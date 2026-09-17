"""聊天补全代理（走 new-api 网关 /v1/chat/completions，SSE 流式透传）。

设计要点：
- 鉴权：BFF 会话 Cookie（require_session）→ 拿 uid + 用户 PAT。前端不需要、也不应该持有任何模型 Key。
- 凭证：new-api 的 /v1 只认 API Key(sk-)，不认 PAT（PAT 仅用于管理类 /api/*）。
  BFF 为每个用户【按需发放并持久化】一把归属该用户的 sk-（user_keys.py，AES 加密落盘）；
  首次聊天/密钥失效时自动 mint（优先用用户自己的 PAT，失败回退管理员代建），之后复用。
  计费按 sk- 归属落到该用户配额，与管理员账隔离。用户全程免 Key、不可见 Key。
- 这是创作站「写文案」聊天的后端：用户登录 BFF 即可免 Key 调用模型，统一走 new-api 配额。

请求体：前端按 OpenAI 格式构造（model / messages / stream / temperature ...），除 model 缺省
       走 BFF_CHAT_DEFAULT_MODEL 外其余字段原样透传网关。
响应体：网关 SSE 字节流原样透传；网关若返回非 2xx，转成一条 SSE error 事件让前端优雅失败。
"""
import json
import logging

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import config, newapi_client as na
from ..newapi_client import NewApiError
from ..security import require_session
from .. import platform_catalog, user_keys

logger = logging.getLogger("bff.chat")
router = APIRouter()

_CHAT_CLIENT: "httpx.AsyncClient | None" = None


def _chat_client() -> httpx.AsyncClient:
    global _CHAT_CLIENT
    if _CHAT_CLIENT is None:
        _CHAT_CLIENT = httpx.AsyncClient(
            base_url=config.NEWAPI_BASE_URL,
            timeout=httpx.Timeout(config.CHAT_STREAM_TIMEOUT, connect=10.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
        )
    return _CHAT_CLIENT


async def close() -> None:
    """归还聊天代理 httpx 连接池（lifespan 关闭时调用）。"""
    global _CHAT_CLIENT
    if _CHAT_CLIENT is not None:
        await _CHAT_CLIENT.aclose()
        _CHAT_CLIENT = None


async def resolve_user_key(uid: int, user_pat: str) -> str:
    """拿到该用户用于 /v1 的 sk-：优先复用已存密钥，否则按需发放并持久化。

    发放顺序：
      1) 用户态自建房（用会话里的用户 PAT，最贴近「该用户自己持有的 Key」）；
      2) 管理员代建兜底（用户 PAT 已失效时，用管理员凭证为该 uid 生成）。
    任一成功即写入 user_keys 存储，供后续请求复用。
    """
    key = user_keys.get_key(uid)
    if key:
        return key
    try:
        key = await na.mint_user_api_key(user_pat, uid)
        user_keys.set_key(uid, key)
        return key
    except NewApiError as e:
        logger.warning("用户态发放 sk- 失败 uid=%s: %s，尝试管理员代建", uid, e.message)
    key = await na.admin_mint_user_api_key(uid)  # 失败则抛 NewApiError 由 handler 转 502
    user_keys.set_key(uid, key)
    return key


@router.post("/api/chat/completions")
async def chat_completions(request: Request, session: dict = Depends(require_session)):
    """OpenAI 兼容的聊天补全（SSE 流式）。用户免 Key，计费走 new-api 配额。"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "message": "请求体不是合法 JSON"})
    if not isinstance(payload, dict) or not payload.get("messages"):
        return JSONResponse(status_code=400, content={"success": False, "message": "缺少 messages 字段"})
    model = payload.get("model") or config.CHAT_DEFAULT_MODEL
    # 引用图片时强制走视觉模型：纯文本模型（如 deepseek-v4-flash）无法处理 image_url，
    # 必须由网关视觉模型（gpt-4.1-mini 等）处理。仅当配置了视觉模型且消息确实含图才覆盖。
    if config.CHAT_VISION_MODEL and _messages_have_image(payload.get("messages")):
        model = config.CHAT_VISION_MODEL
    if not model:
        return JSONResponse(
            status_code=400,
            content={"success": False,
                     "message": "未指定模型且服务端未配置默认模型（请在 BFF 设 BFF_CHAT_DEFAULT_MODEL 或前端传 model）"},
        )
    payload["model"] = model
    payload["stream"] = True

    # ⭐ 平台下架闸门：模型被管理员下架/删除后，即便调用方本地还缓存着平台服务
    #    影子条目（没刷新页面），也必须在此拦掉并给出明确原因。
    #    抛 ModelSuspendedError，由 main.py 统一转成 409 + {success:false,message}。
    await platform_catalog.assert_model_available(model)

    uid = session["uid"]
    user_pat = session["pat"]
    try:
        key = await resolve_user_key(uid, user_pat)
    except NewApiError as e:
        return JSONResponse(status_code=502, content={"success": False, "message": e.message})

    async def forward(initial_key: str):
        key = initial_key
        tries = 0
        while True:
            headers = {"Authorization": f"Bearer {key}", "Accept": "text/event-stream"}
            try:
                async with _chat_client().stream(
                    "POST", "/v1/chat/completions", headers=headers, json=payload
                ) as upstream:
                    if upstream.status_code >= 400:
                        # 401 且是首次：sk- 可能已被用户在前端撤销/轮换，删旧密钥重试一次
                        if upstream.status_code == 401 and tries == 0:
                            body = await upstream.aread()
                            logger.warning("聊天 sk- 失效 uid=%s，轮换重试", uid)
                            user_keys.delete_key(uid)
                            try:
                                key = await resolve_user_key(uid, user_pat)
                            except NewApiError as e:
                                yield _sse_error(401, e.message)
                                return
                            tries += 1
                            continue
                        body = await upstream.aread()
                        yield _sse_error(upstream.status_code, _decode(body))
                        return
                    async for chunk in upstream.aiter_raw():
                        yield chunk
                return
            except httpx.HTTPError as e:
                logger.warning("chat upstream error uid=%s: %s", uid, e)
                yield _sse_error(502, "上游服务暂时不可用，请稍后重试")
                return

    return StreamingResponse(
        forward(key),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _messages_have_image(messages) -> bool:
    """聊天请求是否携带图像（多模态）。兼容 OpenAI 两种写法：
    content 为数组含 {type:"image_url"} / {type:"image"}，或 content 为对象。
    纯文本模型（如 deepseek-v4-flash）吃不了 image，需切视觉模型。"""
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            continue
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("image", "image_url"):
                    return True
        elif isinstance(content, dict):
            if content.get("type") in ("image", "image_url"):
                return True
    return False


def _decode(body: bytes) -> str:
    try:
        return body.decode("utf-8", "replace")
    except Exception:
        return repr(body)


def _sse_error(status: int, message: str) -> bytes:
    data = json.dumps({
        "error": {
            "message": f"上游返回 {status}：{message[:200]}",
            "type": "bff_proxy_error",
        }
    })
    return f"data: {data}\n\n".encode("utf-8")
