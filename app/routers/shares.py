"""素材点对点共享 API（A 方案：指定 new-api 用户）。

设计（2026-09-09 拍板，飞哥）：
- 范围模型：A 点对点（指定 new-api 用户名，BFF 解析成 uid）。不做 workspace / 公开链接。
- 共享粒度：单素材 + 文件夹（文件夹 = 前端把该 folder 下所有 media_key 批量提交）。
- 权限：view（查看·引用·拖入画布） / download（额外允许"保存到我的素材库"）。
- 撤销：DELETE /api/shares/{id}（owner 自己撤销）。

字节不复制：cloud_media 字节仍归 owner 的 OSS key 空间，共享只存授权视图
cloud_media_shares(owner_uid, media_key, target_uid, perm, name)。读取不变量：
viewer==owner OR 存在 shares(target_uid=viewer, media_key) → 代理读 OSS 字节。

鉴权统一 require_session（uid 可信）。创建共享时校验 media_key 属于 owner
（查 cloud_media.uid），防越权共享他人素材；目标用户名经 admin_resolve_uid_by_username
解析（管理员凭证，不依赖用户自身 PAT）。
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from .. import cloudstore, newapi_client as na
from ..resp import fail, ok
from ..security import require_session

router = APIRouter()

PERMS = {"view", "download"}


def _uid(session: dict) -> int:
    return int(session["uid"])


class ShareCreate(BaseModel):
    media_keys: list  # 要共享的 media_key 列表（单素材或整文件夹展开）
    names: dict = {}  # media_key -> 显示名（来自前端 AssetItem.name）
    target_username: str | None = None  # 单个目标（兼容旧前端）
    target_usernames: list = []  # 多个目标：优先使用此项
    perm: str = "view"  # view | download


@router.post("/api/shares")
async def create_share(body: ShareCreate, session: dict = Depends(require_session)):
    owner = _uid(session)
    if not body.media_keys:
        return fail("media_keys 不能为空")
    if body.perm not in PERMS:
        return fail("perm 非法（应为 view / download）")
    # 收集目标用户名列表（兼容单字符串 + 多字符串）
    raw_targets = []
    if body.target_usernames:
        raw_targets.extend(body.target_usernames)
    if body.target_username:
        raw_targets.append(body.target_username)
    raw_targets = [str(u).strip() for u in raw_targets if str(u).strip()]
    if not raw_targets:
        return fail("请至少输入一个目标用户名")
    # 解析用户名 -> uid（管理员凭证，不依赖用户 PAT）
    targets: dict[str, int] = {}
    for username in raw_targets:
        try:
            uid = await na.admin_resolve_uid_by_username(username)
        except na.NewApiError as e:
            return fail(f"解析用户 {username} 失败：{e.message}", e.status_code)
        if uid == owner:
            return fail(f"不能共享给自己（{username}）")
        targets[username] = uid
    if not targets:
        return fail("没有有效的目标用户")
    # 校验每个 key 属于 owner（防越权共享他人素材），只校验一次
    valid = [k for k in body.media_keys
             if (idx := await cloudstore.media_index_get_by_key(k)) and idx.get("uid") == owner]
    if not valid:
        return fail("没有可共享的素材（素材不存在或非您所有）")
    # 批量写入：每个目标并发写授权行
    await asyncio.gather(
        *(cloudstore.share_put_batch(owner, uid, body.perm, body.names or {}, valid)
          for uid in targets.values())
    )
    return ok({
        "shared": len(valid),
        "targets": [{"username": u, "uid": uid} for u, uid in targets.items()],
        "target_count": len(targets),
    })


@router.delete("/api/shares/{share_id}")
async def revoke_share(share_id: str, session: dict = Depends(require_session)):
    owner = _uid(session)
    removed = await cloudstore.share_delete(share_id, owner)
    return ok({"removed": removed})


@router.get("/api/shares")
async def my_shares(session: dict = Depends(require_session)):
    """我发出的共享列表（按目标分组，含用户名展示）。"""
    owner = _uid(session)
    rows = await cloudstore.share_list_by_owner(owner)
    out = [{
        "id": r["id"], "media_key": r["media_key"], "name": r.get("name") or "",
        "target_uid": r["target_uid"], "perm": r["perm"], "created_at": r.get("created_at"),
        "mimeType": r.get("mime") or "application/octet-stream",
        "kind": r.get("kind"), "size": r.get("size"),
    } for r in rows]
    uids = {r["target_uid"] for r in rows}
    uname = {}
    for uid in uids:
        try:
            u = await na.admin_get_user(uid)
            uname[uid] = u.get("username") or str(uid)
        except Exception:
            uname[uid] = str(uid)
    for r in out:
        r["target_username"] = uname.get(r["target_uid"], str(r["target_uid"]))
    return ok(out)


@router.get("/api/shared/media")
async def shared_to_me(session: dict = Depends(require_session)):
    """共享给我的素材索引（合并：指定我的共享）。返回索引 + 来自谁。"""
    viewer = _uid(session)
    rows = await cloudstore.share_list_for_viewer(viewer)
    out = [{
        "id": r["id"], "media_key": r["media_key"], "owner_uid": r["owner_uid"],
        "name": r.get("name") or "", "perm": r["perm"],
        "kind": r.get("kind"), "mime": r.get("mime"), "size": r.get("size"),
        "mimeType": r.get("mime") or "application/octet-stream",
        "created_at": r.get("created_at"),
    } for r in rows]
    uids = {r["owner_uid"] for r in rows}
    uname = {}
    for uid in uids:
        try:
            u = await na.admin_get_user(uid)
            uname[uid] = u.get("username") or str(uid)
        except Exception:
            uname[uid] = str(uid)
    for r in out:
        r["owner_username"] = uname.get(r["owner_uid"], str(r["owner_uid"]))
    return ok(out)


@router.get("/api/shared/media/{media_key}")
async def shared_media_bytes(media_key: str, session: dict = Depends(require_session)):
    """取共享素材字节：按不变量校验权限 → 代理读 OSS（307 重定向 / 本地文件流）。"""
    viewer = _uid(session)
    info = await cloudstore.media_get_shared(media_key, viewer)
    if not info:
        raise HTTPException(status_code=404, detail="素材不存在或无访问权限")
    if info.get("url"):
        return RedirectResponse(url=info["url"], status_code=307)
    if info.get("path"):
        return FileResponse(
            info["path"], media_type=info["mime"],
            headers={"Cache-Control": "private, max-age=31536000, immutable"})
    raise HTTPException(status_code=404, detail="素材字节缺失")
