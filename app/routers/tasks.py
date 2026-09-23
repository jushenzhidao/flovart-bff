"""图片任务 REST 端点 + 用户请求日志（契约见 IMAGE-ASYNC-TASKS-CONTRACT.md）。

BFF 同时兼容同步 / 异步（见 app/tasks.TASK_TYPES）：
- POST   /api/tasks                 提交任务：按 type 的 mode 分流
                                     → 返回 {id, taskId, kind, mode, status, result?, gatewayTask}
- GET    /api/tasks/{id}            轮询状态：id = 提交返回的 request_id
                                     → async 转发网关；sync 直接返回存储结果
- DELETE /api/tasks/{id}            取消：async 转网关 DELETE；sync 即时完成不可取消

生图「镜像路由」（方案 B，2026-09-23；路径即语义，响应体同上任务协议）：
- POST   /api/v1/images/generations            同步提交（路径强制 mode=sync）
- POST   /api/async/v1/images/generations      异步提交（路径强制 mode=async）
- GET    /api/v1/images/generations/{req_id}   同步轮询（BFF 扩展语义：上游同步端点无 GET，
                                                读 BFF 请求日志存储结果）
- GET    /api/async/v1/images/generations/{req_id}  异步轮询（等价 GET /api/tasks/{req_id}）
仅支持 image-gen；旧 /api/tasks 完全保留。详见 _create_task_common。

用户请求日志（调用模型时的请求结构+参数，按 request_id 落库，供拉日志对齐）：
- GET    /api/me/requests?limit=&offset=   当前用户请求记录列表（最新优先）
- GET    /api/me/requests/{request_id}      单条详情（含完整 payload + result）

所有端点 require_session（uid 取自登录会话，透传给网关做归属/计费隔离与越权拦截）。
网关 4xx/5xx 由 newapi_client 抛 NewApiError，经 main 异常处理器原样透传前端
（如 404=任务不存在/越权，429=网关限流，502=网关错误）。
"""
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import cloudstore, config, platform_catalog, tasks
from ..platform_catalog import ModelSuspendedError
from ..resp import ok
from ..security import is_admin, require_session

logger = logging.getLogger("bff.tasks_router")

router = APIRouter()


class SubmitBody(BaseModel):
    type: str = Field(..., description="image-gen | upscale | remove-background | split-layers | outpaint | mask | annotate | relight | edit | video-gen")
    params: dict[str, Any] = Field(default_factory=dict, description="各 type 请求体，原样透传网关")


@router.post("/api/tasks")
async def create_task(body: SubmitBody, session: dict = Depends(require_session)):
    return await _create_task_common(body, session)


# ---------------------------------------------------------------------------
# 方案 B：生图请求「镜像路由」（飞哥 2026-09-23 拍板）
# ---------------------------------------------------------------------------
# 目标：浏览器 devtools 里的请求 URL 与上游真实调用路径一致（区分同步/异步）。
#   同步：POST /api/v1/images/generations            ↔ 上游 v1/images/generations
#   异步：POST /api/async/v1/images/generations      ↔ 上游 async/v1/images/generations
#   轮询：GET  /api/{async/}v1/images/generations/{request_id}
#
# 路径即语义（D1）：sync/async 由 URL 显式决定，优先级高于 params.mode；
# 两者冲突时以路径为准并打 warning 日志（方便发现前端服务配置与路径不一致）。
# 响应体完全复用现有任务协议（D2）；同步执行机制不变（D3）：BFF 依旧后台
# 完成同步上游调用，镜像 POST 只是入口别名 —— 严禁复制业务逻辑（D5）。
# ⚠️ 仅 image-gen（D4）；旧 /api/tasks 完全保留、行为不变。
_MIRROR_IMAGE_KIND = "image-gen"


async def _create_task_common(
    body: SubmitBody, session: dict, path_mode: "str | None" = None
) -> Any:
    """提交公共流程（旧 /api/tasks 与镜像路由共用）。

    path_mode=None → 旧路由（mode 照旧由 params.mode / 模型表 / 全局默认解析）；
    path_mode ∈ {"sync","async"} → 镜像路由，路径强制 mode，冲突时告警。
    """
    if body.type not in tasks.TASK_TYPES:
        raise HTTPException(status_code=400, detail=f"不支持的任务类型: {body.type}")
    if not isinstance(body.params, dict):
        raise HTTPException(status_code=400, detail="params 必须是对象")
    if path_mode is not None:
        # D4：镜像路由只做图片。
        if body.type != _MIRROR_IMAGE_KIND:
            raise HTTPException(
                status_code=400,
                detail=f"镜像路由仅支持 {_MIRROR_IMAGE_KIND}，当前: {body.type}")
        # D1：路径即语义 —— 冲突时以路径为准 + warning。
        pmode = body.params.get("mode")
        if pmode in ("sync", "async") and pmode != path_mode:
            logger.warning(
                "镜像路由路径与 params.mode 冲突（以路径为准）path=%s params.mode=%s "
                "model=%s uid=%s", path_mode, pmode,
                body.params.get("model") or body.params.get("model_name"),
                session.get("uid"))
        params = {**body.params, "mode": path_mode}
    else:
        params = body.params
    # ⭐ Pro 分层管理员闸门（飞哥 2026-09-21）：Seedream V5.0 Pro Layer Decomposition
    #    单价 $0.765+，约为 qwen 分层的 8~15 倍，先只对管理员开放内测。
    #    前端只是不显示（治「看见」），准入判定必须放服务端（治「能不能调」）。
    #    语义值 model:'pro' 由 tasks.resolve_split_model 统一映射，勿在此重复判断模型名。
    if body.type == "split-layers" and not is_admin(session):
        if tasks.resolve_split_model(params) == config.WAVESPEED_SPLIT_MODEL_PRO:
            raise HTTPException(status_code=403, detail="Pro 分层模型仅管理员可用")
    # ⭐ 平台下架闸门（飞哥 2026-09-16）：模型被管理员下架/删除后，**即使调用方
    #    本地还缓存着那条平台服务影子条目**（没刷新页面），也必须在这里被拦掉。
    #    前端拉取只能治「显示」，治不了「能不能调」—— 准入判定只能放服务端。
    # ⚠️ 带 `_gateway` 的请求是**用户 BYOK 直连自己的端点**（与平台供给无关），
    #    绝不能拦：那些模型名从来没进过平台目录。见 app/platform_catalog.py 模块头。
    if not params.get("_gateway"):
        model_name = params.get("model") or params.get("model_name") or ""
        try:
            await platform_catalog.assert_model_available(model_name)
        except ModelSuspendedError as e:
            # 409 拦截也必须留痕（此前直接 raise，请求日志里查无此调用 ——
            # 飞哥 2026-09-18：「失败但日志全空」盲区之一）。
            # status=failed + mode=blocked：管理台 /api/console/requests 可筛。
            req_id = uuid.uuid4().hex
            try:
                await cloudstore.request_log_put(
                    int(session["uid"]), req_id, body.type, "gateway",
                    model_name, cloudstore.strip_b64({**params}),
                    status="failed", mode="blocked")
                await cloudstore.request_log_update(
                    req_id, result={"code": e.code, "reason": e.reason,
                                    "message": e.message})
            except Exception as log_err:  # noqa: BLE001 —— 记日志失败绝不能盖掉 409 本身
                logger.warning("409 拦截日志落库失败 uid=%s model=%s: %s",
                               session.get("uid"), model_name, log_err)
            raise
    # BFF 按 type 的 mode 分流（async 透传网关 tasks / sync 阻塞直出），并记录请求日志。
    task = await tasks.submit(int(session["uid"]), body.type, params)
    return ok(task)


# ---------- 镜像路由：提交（路径即语义，D1） ----------
@router.post("/api/v1/images/generations",
             summary="生图同步提交（镜像路由，等价 POST /api/tasks image-gen mode=sync）")
async def create_task_mirror_sync(body: SubmitBody, session: dict = Depends(require_session)):
    return await _create_task_common(body, session, path_mode="sync")


@router.post("/api/async/v1/images/generations",
             summary="生图异步提交（镜像路由，等价 POST /api/tasks image-gen mode=async）")
async def create_task_mirror_async(body: SubmitBody, session: dict = Depends(require_session)):
    return await _create_task_common(body, session, path_mode="async")


# ---------- 镜像路由：轮询 ----------
# ⚠️ BFF 扩展语义：上游同步端点 v1/images/generations **没有 GET**（文档只有 POST）。
#    这两条 GET 是 BFF 自有轮询入口：sync 读请求日志存储结果 / async 转发网关任务
#    （与 GET /api/tasks/{request_id} 完全等价），仅为了让 devtools 请求路径与
#    上游对齐。request_id = 提交返回视图里的 id（BFF request_id，非网关 task_id）。
@router.get("/api/v1/images/generations/{request_id}",
            summary="生图同步轮询（BFF 扩展语义，读存储结果；上游同步端点无 GET）")
@router.get("/api/async/v1/images/generations/{request_id}",
            summary="生图异步轮询（镜像路由，等价 GET /api/tasks/{request_id}）")
async def get_task_mirror(request_id: str, session: dict = Depends(require_session)):
    # id 即提交时返回的 request_id；网关按 New-Api-User 隔离，越权/不存在返回 404。
    task = await tasks.get_task(request_id, int(session["uid"]))
    return ok(task)


@router.get("/api/tasks/{request_id}")
async def get_task(request_id: str, session: dict = Depends(require_session)):
    # id 即提交时返回的 request_id；网关按 New-Api-User 隔离，越权/不存在返回 404。
    task = await tasks.get_task(request_id, int(session["uid"]))
    return ok(task)


@router.delete("/api/tasks/{request_id}")
async def cancel_task(request_id: str, session: dict = Depends(require_session)):
    result = await tasks.cancel_task(request_id, int(session["uid"]))
    return ok(result)


# ---------- 用户请求日志（拉日志对齐）----------
@router.get("/api/me/requests")
async def list_requests(
    limit: int = Query(50, ge=1, le=200, description="每页条数"),
    offset: int = Query(0, ge=0, description="偏移"),
    session: dict = Depends(require_session),
):
    items = await cloudstore.request_log_list(int(session["uid"]), limit, offset)
    return ok({"items": items, "limit": limit, "offset": offset})


@router.get("/api/me/requests/{request_id}")
async def get_request(request_id: str, session: dict = Depends(require_session)):
    row = await cloudstore.request_log_get(request_id)
    if not row or row["uid"] != int(session["uid"]):
        raise HTTPException(status_code=404, detail="请求记录不存在")
    return ok(row)
