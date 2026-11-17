"""外部接入 Key 管理（用户态，2026-11-03）。

用途：让外部 agent（WorkBuddy MCP、脚本等）以用户身份调用 flovart 开放能力
（生图/任务查询/余额），替代浏览器 Cookie 会话 —— Cookie 是 httponly 的，
agent 拿不到，也不该把浏览器会话交给机器。

设计要点：
- Key 形如 fvk_<43位urlsafe>；库里只存 SHA-256 哈希（cloud_api_keys.key_hash），
  明文**仅在生成响应里出现一次**，之后任何接口都拿不回来（与 GitHub token 同款语义）。
- 每用户上限 MAX_KEYS_PER_USER（默认 5）把，防脚本批量生成。
- 列表/删除均需浏览器会话（require_session）—— 外部 Key 本身不能管理 Key，
  泄露的 Key 最坏只能生图烧积分，动不了账号。
- 调用侧鉴权见 security.require_identity（双轨：Cookie 优先，X-API-Key 其次），
  限频 60 次/分钟/Key。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import cloudstore
from ..resp import fail, ok
from ..security import generate_api_key, require_session

logger = logging.getLogger("bff.apikeys")

router = APIRouter()

MAX_KEYS_PER_USER = 5


class ApiKeyCreateBody(BaseModel):
    name: str = Field(default="", max_length=40, description="备注名，如「我的 WorkBuddy」")


def _payload(rec: dict, plain: "str | None" = None) -> dict:
    return {
        "id": rec["id"], "name": rec["name"], "prefix": rec["prefix"],
        "status": rec["status"], "created_at": rec["created_at"],
        "last_used_at": rec["last_used_at"], "use_count": rec["use_count"],
        **({"key": plain} if plain else {}),
    }


@router.get("/api/me/apikeys")
async def list_api_keys(session: dict = Depends(require_session)):
    items = await cloudstore.api_key_list(int(session["uid"]))
    return ok({"items": [_payload(r) for r in items],
               "max": MAX_KEYS_PER_USER})


@router.post("/api/me/apikeys")
async def create_api_key(body: ApiKeyCreateBody, session: dict = Depends(require_session)):
    uid = int(session["uid"])
    existing = await cloudstore.api_key_list(uid)
    if len(existing) >= MAX_KEYS_PER_USER:
        return fail(f"每账号最多 {MAX_KEYS_PER_USER} 把外部接入 Key，请先删除不用的")
    plain, key_hash = generate_api_key()
    # prefix = 明文前 12 位（fvk_ + 8 字符），用于列表识别，不含可逆信息
    rec = await cloudstore.api_key_create(uid, key_hash, plain[:12], body.name.strip() or "外部接入 Key")
    logger.info("外部接入 Key 生成 uid=%s key_id=%s prefix=%s", uid, rec["id"], rec["prefix"])
    # 明文仅此一次返回
    return ok(_payload(rec, plain), "生成成功：Key 仅显示这一次，请立即复制保存")


@router.delete("/api/me/apikeys/{key_id}")
async def delete_api_key(key_id: int, session: dict = Depends(require_session)):
    deleted = await cloudstore.api_key_delete(int(session["uid"]), key_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Key 不存在或已删除")
    return ok(None, "已删除")
