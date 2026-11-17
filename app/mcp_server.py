"""flovart 远程 MCP server（挂载在 BFF 进程 /mcp，2026-11-03）。

目的：让外部 agent（WorkBuddy / Claude Code / 任何 MCP 客户端）以 flovart 用户
身份调用平台生图能力。选「远程 HTTP MCP」而非本地 stdio：小白用户**零安装**——
网页生成接入 Key → 一键复制配置 → 粘进客户端即可，server 升级统一在服务端。

架构：
- FastMCP streamable-http（stateless），ASGI 包一层 X-API-Key 鉴权后 mount 到
  主 FastAPI 的 /mcp 前缀（最终 URL = https://<bff>/mcp）。
- 鉴权中间件：X-API-Key → SHA-256 → cloud_api_keys 查表 + 限频（60/min，见
  security._check_api_key_rate），通过的 identity 存 ContextVar 供 tools 读取。
  （不用 fastmcp 自带 auth 依赖 —— 我们的鉴权就是一张表 + 一个请求头，简单直给。）
- 计费归属：identity.pat=None，tasks.submit → _user_api_key 走管理员代建通道，
  sk- token 建在用户名下（new-api user_id=uid），积分扣用户自己的。

对外契约（tools）：
- generate_image(prompt, model?, size?, wait=True)   提交生图；sync 模式默认等待出图
- get_task(request_id)                                查询任务状态/结果
- get_balance()                                       剩余积分
- list_models()                                       平台可用模型目录
"""
import asyncio
import contextlib
import contextvars
import logging

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

from . import cloudstore, config, newapi_client as na, platform_catalog, tasks as task_core
from .routers.tasks import SubmitBody, _create_task_common
from .security import _check_api_key_rate, _hash_api_key

logger = logging.getLogger("bff.mcp")

mcp = FastMCP(
    "flovart",
    instructions=(
        "flovart（oneArt）AI 创作站的 MCP 接入。"
        "用 generate_image 生成图片（默认等待出图并返回图片 URL）；"
        "不确定模型名时先调 list_models；长任务用 get_task 轮询；"
        "get_balance 查剩余积分。"
    ),
)

# 鉴权中间件 → tools 传递身份（同一 ASGI 请求内可见）
_identity_cv: contextvars.ContextVar = contextvars.ContextVar("flovart_mcp_identity", default=None)

_SYNC_WAIT_TIMEOUT = 150.0   # sync 出图等待上限（秒）
_SYNC_POLL_INTERVAL = 3.0
_TERMINAL = ("succeeded", "failed", "cancelled")


# ---------------------------------------------------------------------------
# ASGI 鉴权中间件
# ---------------------------------------------------------------------------
async def _mcp_auth(scope, receive, send, app):
    if scope["type"] != "http":
        await app(scope, receive, send)
        return
    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in (scope.get("headers") or [])}
    raw = (headers.get("x-api-key") or "").strip()
    if not raw:
        await _send_json_error(send, 401, "缺少 X-API-Key 请求头（在 flovart 网页「外部接入」生成）")
        return
    key_hash = _hash_api_key(raw)
    try:
        _check_api_key_rate(key_hash)  # 限频（429）
        rec = await cloudstore.api_key_lookup(key_hash)
    except Exception:  # noqa: BLE001 —— 中间件内异常一律以 500 形式返回，不能挂死协议层
        logger.exception("MCP 鉴权异常")
        await _send_json_error(send, 500, "鉴权服务异常")
        return
    if rec is None:
        await _send_json_error(send, 401, "无效的外部接入 Key")
        return
    await cloudstore.api_key_touch(key_hash)
    token = _identity_cv.set({
        "uid": rec["uid"], "username": "", "pat": None, "role": 1,
        "via": "api-key", "api_key_id": rec["id"],
    })
    try:
        await app(scope, receive, send)
    finally:
        _identity_cv.reset(token)


async def _send_json_error(send, status: int, message: str) -> None:
    import json
    body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def _identity() -> dict:
    ident = _identity_cv.get()
    if not ident:
        raise RuntimeError("MCP 身份缺失（鉴权中间件未运行）")
    return ident


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
def _extract_urls(task_view: dict) -> list:
    """从任务视图抽图片 URL（网关 outputs 优先，回落 BFF 落盘 bffMedia）。

    相对路径（/api/me/media/...）按当前请求的 Host 绝对化 —— agent 在外部环境，
    拿到必须能直接 GET 的完整 URL（媒体下载路由已双轨鉴权，X-API-Key 可用）。
    """
    urls: list = []
    result = task_view.get("result") or {}
    for out in (result.get("outputs") or []):
        if isinstance(out, dict):
            u = out.get("url")
            if u:
                urls.append(u)
        elif isinstance(out, str):
            urls.append(out)
    if not urls:
        for m in (result.get("bffMedia") or []):
            if m.get("url"):
                urls.append(m["url"])
    base = _request_base_url()
    return [u if u.startswith(("http://", "https://", "data:")) else f"{base}{u}"
            for u in urls]


def _request_base_url() -> str:
    """当前 MCP 请求的对外 base（scheme://host），拿不到则空串（返回相对路径）。"""
    try:
        from fastmcp.server.dependencies import get_http_request
        req = get_http_request()
        return str(req.base_url).rstrip("/")
    except Exception:  # noqa: BLE001 —— 测试/非 HTTP 上下文
        return ""


async def _wait_terminal(request_id: str, uid: int) -> dict:
    deadline = asyncio.get_event_loop().time() + _SYNC_WAIT_TIMEOUT
    while True:
        task = await task_core.get_task(request_id, uid, pat=None)
        if (task.get("status") or "").lower() in _TERMINAL:
            return task
        if asyncio.get_event_loop().time() > deadline:
            return {**task, "status": task.get("status") or "processing",
                    "note": f"等待超时（{_SYNC_WAIT_TIMEOUT:.0f}s），请稍后用 get_task 查询 {request_id}"}
        await asyncio.sleep(_SYNC_POLL_INTERVAL)


async def _pick_default_model() -> "str | None":
    """agent 不传 model 时的默认生图模型解析。

    优先 env 配置（BFF_MCP_DEFAULT_IMAGE_MODEL，需在平台目录已发布）；
    配置不可用则从平台目录启发式挑一个名字含 image 的已发布模型。
    都取不到返回 None（让网关报错，错误信息仍会透出）。
    """
    try:
        state = await platform_catalog.read_state()
        published = set((state.get("published") or {}).keys())
    except Exception:  # noqa: BLE001
        published = set()
    if config.MCP_DEFAULT_IMAGE_MODEL and config.MCP_DEFAULT_IMAGE_MODEL.lower() in published:
        return config.MCP_DEFAULT_IMAGE_MODEL
    for m in sorted(published):
        if "image" in m and "preview" not in m:
            return m
    return config.MCP_DEFAULT_IMAGE_MODEL or None


@mcp.tool
async def generate_image(prompt: str, model: str = "", size: str = "",
                         wait: bool = True) -> dict:
    """生成一张图片。

    Args:
        prompt: 画面描述（支持中文，建议具体：主体/风格/构图/光影）。
        model: 模型名（可选，不填自动选平台默认生图模型；list_models 可看可选项）。
        size: 尺寸（可选，如 1024x1024，不填由模型默认）。
        wait: sync 提交时是否等待出图（默认 True，最长等 150 秒；False 立即返回任务号）。
    """
    ident = _identity()
    params: dict = {"prompt": prompt}
    if model:
        params["model"] = model
    else:
        default_model = await _pick_default_model()
        if default_model:
            params["model"] = default_model
    if size:
        params["size"] = size
    params["mode"] = "sync"
    body = SubmitBody(type="image-gen", params=params)
    wrapper = await _create_task_common(body, ident)
    view = (wrapper.get("data") or {}) if isinstance(wrapper, dict) else {}
    request_id = view.get("id")
    if not request_id:
        return {"error": "提交未返回任务 id", "raw": view}
    if wait:
        view = await _wait_terminal(request_id, int(ident["uid"]))
    out = {"id": view.get("id"), "status": view.get("status"),
           "urls": _extract_urls(view), "model": params.get("model")}
    if (view.get("status") or "").lower() == "failed":
        out["error"] = (view.get("result") or {}).get("error") or view.get("result")
    if view.get("note"):
        out["note"] = view["note"]
    return out


@mcp.tool
async def get_task(request_id: str) -> dict:
    """查询任务状态与结果（生图/异步任务通用）。

    Args:
        request_id: 提交时返回的任务 id。
    """
    ident = _identity()
    task = await task_core.get_task(request_id, int(ident["uid"]), pat=None)
    return {"id": request_id, "status": task.get("status"),
            "urls": _extract_urls(task), "task": task}


@mcp.tool
async def get_balance() -> dict:
    """查询当前账号剩余积分与累计已用。"""
    ident = _identity()
    uid = int(ident["uid"])
    # 与 billing.my_points 同口径：团队成员读主池水位，按 quota_to_points 换算
    from . import teams
    owner_uid = await teams.pool_uid_for(uid)
    try:
        data = await na.admin_get_user(owner_uid if owner_uid else uid)
    except na.NewApiError as e:
        # 管理员账号查自己（本地/测试场景）会被网关拒绝「同级不可见」——明确报错而非崩溃
        return {"error": f"余额查询失败：{e.message}（管理员账号请在网关后台查看余额）"}
    quota = data.get("quota")
    if quota is None:
        quota = data.get("remain_quota", 0)
    used = data.get("used_quota", 0)
    return {"points": int(config.quota_to_points(quota)),
            "used_points": int(config.quota_to_points(used)),
            "unit": config.POINTS_UNIT_NAME}


@mcp.tool
async def list_models() -> dict:
    """列出平台当前可用的模型名（生图为主，含编辑类能力）。"""
    models = await na.admin_enabled_models()
    models = await platform_catalog.filter_available(models or [])
    return {"items": models, "total": len(models)}


class _AuthWrappedASGI:
    """pure-ASGI 鉴权包装：转发 lifespan 等属性（main.py lifespan 嵌套要用）。"""

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        await _mcp_auth(scope, receive, send, self._app)

    def __getattr__(self, name):
        return getattr(self._app, name)


# ASGI 子应用：stateless（无会话粘滞），路径 '/'，由主 app mount 到 /mcp
mcp_http_app = _AuthWrappedASGI(
    mcp.http_app(path="/", transport="streamable-http", stateless_http=True))
