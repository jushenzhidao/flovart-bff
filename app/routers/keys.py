"""API Key 管理（用户态代理 new-api）。

创作站前端把 Provider 地址指向 new-api `{base}/v1`、用这里创建的 Key 注入
keyVault，即完成「平台供 Key」改造 —— 模型统一走运营方渠道，用户不自带 Key。
"""
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import config, newapi_client as na
from ..resp import fail, ok
from ..security import require_session

logger = logging.getLogger("bff.keys")

router = APIRouter()

P = config.quota_to_points


class TokenCreateBody(BaseModel):
    name: str


def _token_payload(t: dict, key: str | None = None) -> dict:
    return {
        "id": t["id"], "name": t["name"], "key": key if key is not None else t["key"],
        "status": t["status"], "created_time": t["created_time"],
        "unlimited_quota": t.get("unlimited_quota", False),
        "used_points": P(t.get("used_quota", 0)),
    }


@router.get("/api/token")
async def list_tokens(session: dict = Depends(require_session)):
    d = await na.list_tokens(session["pat"], session["uid"])
    return ok([_token_payload(t) for t in d.get("items", [])])


@router.post("/api/token")
async def create_token(body: TokenCreateBody, session: dict = Depends(require_session)):
    name = body.name.strip() or "未命名 Key"
    pat, uid = session["pat"], session["uid"]
    await na.create_token(pat, uid, name)
    # 创建接口不返回 key，取列表第一条（按创建时间倒序）拿 id，再取明文
    d = await na.list_tokens(pat, uid, page=1, size=1)
    items = d.get("items", [])
    if not items:
        return ok(None, "创建成功")
    t = items[0]
    plain = await na.get_token_key(pat, uid, t["id"])
    return ok(_token_payload(t, plain), "创建成功")


@router.post("/api/token/{token_id}/key")
async def token_plain_key(token_id: int, session: dict = Depends(require_session)):
    plain = await na.get_token_key(session["pat"], session["uid"], token_id)
    return ok({"key": plain})


@router.delete("/api/token/{token_id}")
async def delete_token(token_id: int, session: dict = Depends(require_session)):
    await na.delete_token(session["pat"], session["uid"], token_id)
    return ok(None, "已删除")


# ---------- 平台供 Key（hosted 用户端）----------
async def _ensure_token_plain(session: dict) -> dict:
    """用户态确保有一把默认平台 Key（幂等）。

    用户 PAT 有效时走用户自己的 token 列表（「平台默认 Key」）；PAT 失效
    （new-api 官方前端作废旧值）时降级到管理员代用户通道，避免 401 跳登录。
    两条通道 token name 不同（后者含 uid），但都是 unlimited_quota 平台 Key，
    前端只注入其一，功能等价。
    """
    pat, uid = session["pat"], session["uid"]
    try:
        return await _ensure_token_user(pat, uid)
    except na.NewApiError as e:
        if e.status_code != 401:
            raise
        logger.warning("user PAT 失效，降级 admin_ensure_user_api_key uid=%s", uid)
        return await na.admin_ensure_user_api_key(uid)


async def _ensure_token_user(pat: str, uid: int) -> dict:
    d = await na.list_tokens(pat, uid, page=1, size=100)
    items = d.get("items", [])
    if not items:
        await na.create_token(pat, uid, "平台默认 Key")
        d = await na.list_tokens(pat, uid, page=1, size=1)
        items = d.get("items", [])
    if not items:
        raise na.NewApiError("创建平台 Key 失败", 500)
    t = items[0]
    plain = await na.get_token_key(pat, uid, t["id"])
    return {"id": t["id"], "name": t.get("name") or "平台默认 Key", "key": plain,
            "status": t.get("status", 1)}


@router.post("/api/me/ensure-key")
async def ensure_platform_key(session: dict = Depends(require_session)):
    """登录用户幂等获取「平台默认 Key」明文（自动创建/复用第一个 token）。

    前端 hosted 模式启动后调一次，把返回的 key + api_base_url 注入运行时，
    之后所有模型调用都走这个 Key（管理员在网关侧配置渠道决定能调哪些模型）。
    """
    info = await _ensure_token_plain(session)
    return ok({"id": info["id"], "name": info["name"], "key": info["key"],
               "api_base_url": config.API_BASE_URL}, "OK")


@router.get("/api/models")
async def user_model_catalog(session: dict = Depends(require_session)):
    """登录用户可读的「平台模型目录」（启用渠道的模型名数组）。

    new-api 的 models_enabled 是管理端点；普通用户无权直接调，由 BFF 用
    管理员凭证代取再下发。创作站 default Key 的模型列表由此驱动。
    """
    models = await na.admin_enabled_models()
    return ok({"items": models, "total": len(models)})
