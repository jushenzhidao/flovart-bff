"""云端持久化 API（v2）—— 用户个人数据的云存储。

后端：元数据 PostgreSQL（app/db）/ 字节对象存储（app/oss），未配置云时回落本地。
鉴权：全部 require_session（uid 取自登录会话）。媒体归属校验在 cloudstore.media_get 内完成。

契约：
  GET    /api/me/docs/{scope}/{doc_key}          → {payload, revision, updated_at} | 404
  PUT    /api/me/docs/{scope}/{doc_key}          body {payload, base_revision?}
                                                 → {revision} | 409 {current_revision}
  DELETE /api/me/docs/{scope}/{doc_key}
  GET    /api/me/docs/{scope}                    → [{doc_key, revision, updated_at}]（列表）
  GET    /api/me/history/records                 → {items, total}（生成历史，源 cloud_request_log）
  POST   /api/me/media                           multipart {file, kind?}
                                                 → {media_key, size, mime, url} | 413 | 507 配额
  GET    /api/me/media/{media_key}               → 307 重定向到 OSS presigned URL（或本地文件流）
  DELETE /api/me/media/{media_key}
  GET    /api/me/storage/overview                → {doc_count, media_count, bytes_used, quota_bytes}
"""
import re
from urllib.parse import unquote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from .. import cloudstore
from ..resp import ok
from ..security import require_session
from ..timeutil import iso_to_cn

router = APIRouter()

# scope 白名单：只允许已知业务域，防止把任意路径当 scope（防御性约束）。
ALLOWED_SCOPES = {"projects", "history", "assets", "kv", "settings"}

_SCOPE_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _uid(session: dict) -> int:
    return int(session["uid"])


def _validate_scope_key(scope: str, doc_key: str) -> None:
    if scope not in ALLOWED_SCOPES:
        raise HTTPException(status_code=400, detail=f"不支持的 scope: {scope}")
    if not _SCOPE_KEY_RE.match(doc_key or ""):
        raise HTTPException(status_code=400, detail="doc_key 格式不合法")


@router.get("/api/me/docs/{scope}")
async def doc_list(scope: str, session: dict = Depends(require_session)):
    if scope not in ALLOWED_SCOPES:
        raise HTTPException(status_code=400, detail=f"不支持的 scope: {scope}")
    return ok(await cloudstore.doc_list(_uid(session), scope))


@router.get("/api/me/docs/{scope}/{doc_key:path}")
async def doc_get(scope: str, doc_key: str, session: dict = Depends(require_session)):
    doc_key = unquote(doc_key)
    _validate_scope_key(scope, doc_key)
    doc = await cloudstore.doc_get(_uid(session), scope, doc_key)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    return ok(doc)


@router.put("/api/me/docs/{scope}/{doc_key:path}")
async def doc_put(scope: str, doc_key: str, body: dict, session: dict = Depends(require_session)):
    doc_key = unquote(doc_key)
    _validate_scope_key(scope, doc_key)
    uid = _uid(session)
    if "payload" not in body:
        raise HTTPException(status_code=400, detail="缺少 payload 字段")
    base_revision = body.get("base_revision")
    if base_revision is not None:
        current = await cloudstore.doc_get(uid, scope, doc_key)
        current_revision = current["revision"] if current else 0
        if int(base_revision) != current_revision:
            return JSONConflict(current_revision)
    try:
        result = await cloudstore.doc_put(uid, scope, doc_key, body["payload"])
    except ValueError as e:
        raise HTTPException(status_code=413, detail=str(e)) from e
    return ok(result)


@router.delete("/api/me/docs/{scope}/{doc_key:path}")
async def doc_delete(scope: str, doc_key: str, session: dict = Depends(require_session)):
    doc_key = unquote(doc_key)
    _validate_scope_key(scope, doc_key)
    await cloudstore.doc_delete(_uid(session), scope, doc_key)
    return ok({"deleted": True})


@router.get("/api/me/history/records")
async def history_records(
    limit: int = 20,
    offset: int = 0,
    kind: str = "",
    session: dict = Depends(require_session),
):
    """生成历史（数据源 cloud_request_log，一笔一行）。

    返回 {items, total}；item 含完整 params(result 前的提交参数) + result，
    b64/data-uri 已剥离为占位符。前端「一键同款」直接拿 params 原样重提交
    （b64 引用类参数除外）。旧 history JSON 文档路径不受影响，可并行迁移。
    """
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    data = await cloudstore.request_log_history(_uid(session), limit, offset, kind or "")
    # 补东八区可读时间（原 UTC ISO 字段保留不动）
    for it in (data.get("items") or []):
        it["created_at_cn"] = iso_to_cn(it.get("created_at"))
        it["updated_at_cn"] = iso_to_cn(it.get("updated_at"))
    return ok(data)


@router.post("/api/me/media")
async def media_upload(
    file: UploadFile = File(..., description="媒体二进制（PNG/JPG/WebP/MP4/WAV…）"),
    kind: str = Form("media", description="语义类型：image/video/audio/asset…"),
    session: dict = Depends(require_session),
):
    blob = await file.read()
    if not blob:
        raise HTTPException(status_code=400, detail="空文件")
    try:
        info = await cloudstore.media_put(_uid(session), kind, file.content_type or "", blob,
                                          source_kind="upload")
    except ValueError as e:
        message = str(e)
        status = 507 if "配额" in message else 413
        raise HTTPException(status_code=status, detail=message) from e
    return ok(info)


@router.get("/api/me/media/{media_key}")
async def media_download(media_key: str, session: dict = Depends(require_session)):
    media = await cloudstore.media_get(_uid(session), media_key)
    if not media:
        raise HTTPException(status_code=404, detail="媒体不存在")
    # OSS 后端返回 presigned url → 307 重定向（前端直连 OSS，需 bucket CORS）。
    if media.get("url"):
        return RedirectResponse(url=media["url"], status_code=307)
    # 本地兜底：文件流。
    return FileResponse(
        media["path"],
        media_type=media["mime"],
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.delete("/api/me/media/{media_key}")
async def media_remove(media_key: str, session: dict = Depends(require_session)):
    deleted = await cloudstore.media_delete(_uid(session), media_key)
    return ok({"deleted": deleted})


@router.get("/api/me/storage/overview")
async def storage_overview(session: dict = Depends(require_session)):
    return ok(await cloudstore.storage_overview(_uid(session)))


def JSONConflict(current_revision: int):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=409,
        content={"success": False, "message": "云端已有更新", "data": {"current_revision": current_revision}},
    )
