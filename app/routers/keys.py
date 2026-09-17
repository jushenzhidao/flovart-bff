"""API Key 管理（用户态代理 new-api）。

创作站前端把 Provider 地址指向 new-api `{base}/v1`、用这里创建的 Key 注入
keyVault，即完成「平台供 Key」改造 —— 模型统一走运营方渠道，用户不自带 Key。
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import cloudstore, config, newapi_client as na, platform_catalog
from ..resp import fail, ok
from ..security import decrypt_secret, encrypt_secret, require_session

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
    """登录用户可读的「平台模型目录」——**该用户按其分组真正可调用的模型**。

    为什么要用用户自己的 sk- 去查（而不是管理员全站列表）：
    new-api 的渠道按【分组】绑定，用户属于某个分组，只能调用该分组渠道上的模型。
    而 `admin_enabled_models()`（GET /api/channel/models_enabled）返回的是**全站**
    启用模型、**不按分组过滤** —— 直接下发会让用户看到一堆自己调不通的模型
    （症状：模型列表里有、点下去报 `No available channel for model X under group ...`）。
    实测：default 分组 /v1/models 返回 31 个（图片类 0 个），而全站列表有 52 个。

    故这里改用【用户自己的 sk-】查网关 /v1/models（该方法按 Key 所属分组过滤），
    与聊天/图片调用的可见范围严格一致 —— 目录里有的，就是真能调的。

    降级：取不到 sk- 或查 /v1/models 失败时，回落管理员全站列表（保证目录非空，
    前端不至于空白；宁可多显示也不要让平台服务整体消失）。

    ⭐ 2026-09-16：出口统一过一遍**下架过滤器**（`platform_catalog.filter_available`）。
    被管理员下架/删除的模型名在这里就被剔掉 —— 这样即使用户本地 keyVault 里
    还留着那条平台服务影子条目（页面没刷新），模型选择器里也不会再列出来。
    本函数只治「显示」，真正的准入拦截在 `/api/tasks` 与 `/api/chat/completions`。
    """
    try:
        info = await _ensure_token_plain(session)
        models = await na.user_available_models(info["key"])
        if models:
            models = await platform_catalog.filter_available(models)
            return ok({"items": models, "total": len(models), "scoped": True})
        logger.warning("用户态 /v1/models 返回空，回落全站列表 uid=%s", session.get("uid"))
    except Exception as e:  # noqa: BLE001 — 任何失败都不应让模型目录整体 500
        logger.warning("用户态模型目录获取失败，回落全站列表 uid=%s: %s", session.get("uid"), e)
    models = await platform_catalog.filter_available(await na.admin_enabled_models())
    return ok({"items": models, "total": len(models), "scoped": False})


# ─── 用户 AI 服务配置云端同步（接口1，2026-09-17 飞哥需求）────────────────────
#
# 需求：管理员/普通用户配置的 AI 服务（BYOK 卡片）不再只存浏览器 localStorage，
# 而是存到服务器 —— 换设备登录同一账号，直接看到之前配置的服务。
#
# 安全：前端把「自己的 AI 服务」数组（**已剔除平台影子条目**，那是服务端下发的
# 派生数据，不能回存）整体 JSON 后交给这里；服务端用 AES-256-GCM（security.
# encrypt_secret，与服务端会话同一把主密钥）加密后再落 cloudstore。
# DB 泄漏拿不到明文 key；传输走 HTTPS + 会话 Cookie。
#
# 同步语义（v1，Last-Write-Wins）：
#   - 登录后前端先 GET：服务器有 → 整体采纳（替换本地 own keys）；
#     服务器没有而本地有 → 推上去。
#   - 之后本地每次改动（防抖）→ PUT 整体覆盖。多设备并发编辑以后到者胜。

_USER_SERVICES_SCOPE = "user_keys"
_USER_SERVICES_DOC = "ai_services"
_USER_SERVICES_MAX_KEYS = 200


class AiServicesBody(BaseModel):
    keys: list[dict]


@router.get("/api/user/ai-services")
async def get_user_ai_services(session: dict = Depends(require_session)):
    """读当前用户的 AI 服务配置（返回解密后的 keys 数组与更新时间）。"""
    doc = await cloudstore.doc_get(session["uid"], _USER_SERVICES_SCOPE, _USER_SERVICES_DOC)
    if not doc:
        return ok({"keys": [], "updated_at": None})
    payload = doc.get("payload") or {}
    blob = payload.get("blob")
    keys: list = []
    if blob:
        try:
            decrypted = json.loads(decrypt_secret(str(blob)))
            if isinstance(decrypted, list):
                keys = decrypted
        except Exception as e:  # noqa: BLE001 — 密钥轮换/损坏时按空处理，不让 GET 500
            logger.warning("用户 AI 服务配置解密失败 uid=%s: %s", session["uid"], e)
    return ok({"keys": keys, "updated_at": doc.get("updated_at")})


@router.put("/api/user/ai-services")
async def put_user_ai_services(body: AiServicesBody, session: dict = Depends(require_session)):
    """覆盖保存当前用户的 AI 服务配置（整体替换，Last-Write-Wins）。"""
    if len(body.keys) > _USER_SERVICES_MAX_KEYS:
        raise HTTPException(status_code=413, detail=f"AI 服务数量超过上限 {_USER_SERVICES_MAX_KEYS}")
    blob = encrypt_secret(json.dumps(body.keys, ensure_ascii=False))
    if len(blob) > 512 * 1024:
        raise HTTPException(status_code=413, detail="AI 服务配置体积过大")
    doc = await cloudstore.doc_put(
        session["uid"], _USER_SERVICES_SCOPE, _USER_SERVICES_DOC, {"blob": blob})
    return ok({"updated_at": doc.get("updated_at"), "count": len(body.keys)})
