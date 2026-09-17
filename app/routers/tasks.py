"""图片任务 REST 端点 + 用户请求日志（契约见 IMAGE-ASYNC-TASKS-CONTRACT.md）。

BFF 同时兼容同步 / 异步（见 app/tasks.TASK_TYPES）：
- POST   /api/tasks                 提交任务：按 type 的 mode 分流
                                     → 返回 {id, taskId, kind, mode, status, result?, gatewayTask}
- GET    /api/tasks/{id}            轮询状态：id = 提交返回的 request_id
                                     → async 转发网关；sync 直接返回存储结果
- DELETE /api/tasks/{id}            取消：async 转网关 DELETE；sync 即时完成不可取消

用户请求日志（调用模型时的请求结构+参数，按 request_id 落库，供拉日志对齐）：
- GET    /api/me/requests?limit=&offset=   当前用户请求记录列表（最新优先）
- GET    /api/me/requests/{request_id}      单条详情（含完整 payload + result）

所有端点 require_session（uid 取自登录会话，透传给网关做归属/计费隔离与越权拦截）。
网关 4xx/5xx 由 newapi_client 抛 NewApiError，经 main 异常处理器原样透传前端
（如 404=任务不存在/越权，429=网关限流，502=网关错误）。
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import cloudstore, platform_catalog, tasks
from ..resp import ok
from ..security import require_session

router = APIRouter()


class SubmitBody(BaseModel):
    type: str = Field(..., description="image-gen | upscale | remove-background | split-layers | outpaint | mask | annotate | relight | edit | video-gen")
    params: dict[str, Any] = Field(default_factory=dict, description="各 type 请求体，原样透传网关")


@router.post("/api/tasks")
async def create_task(body: SubmitBody, session: dict = Depends(require_session)):
    if body.type not in tasks.TASK_TYPES:
        raise HTTPException(status_code=400, detail=f"不支持的任务类型: {body.type}")
    if not isinstance(body.params, dict):
        raise HTTPException(status_code=400, detail="params 必须是对象")
    # ⭐ 平台下架闸门（飞哥 2026-09-16）：模型被管理员下架/删除后，**即使调用方
    #    本地还缓存着那条平台服务影子条目**（没刷新页面），也必须在这里被拦掉。
    #    前端拉取只能治「显示」，治不了「能不能调」—— 准入判定只能放服务端。
    # ⚠️ 带 `_gateway` 的请求是**用户 BYOK 直连自己的端点**（与平台供给无关），
    #    绝不能拦：那些模型名从来没进过平台目录。见 app/platform_catalog.py 模块头。
    if not body.params.get("_gateway"):
        await platform_catalog.assert_model_available(
            body.params.get("model") or body.params.get("model_name"))
    # BFF 按 type 的 mode 分流（async 透传网关 tasks / sync 阻塞直出），并记录请求日志。
    task = await tasks.submit(int(session["uid"]), body.type, body.params)
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
