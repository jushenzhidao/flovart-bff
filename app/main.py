"""flovart-bff FastAPI 应用入口。

结构：组装（lifespan / 异常处理 / 路由挂载 / 探针）。业务路由分布在
app/routers/{auth,keys,usage,console}.py，公共件在 app/{config,security,
newapi_client,store,promo}.py —— 参考 hewapi-bff 的单文件分节骨架，按域拆开。
"""
import contextlib
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import cloudstore, config, newapi_client as na, observability, store, tasks, oss, db
from .newapi_client import NewApiError
from .platform_catalog import ModelSuspendedError
from .resp import ok
from .routers import (auth, billing, cloud, console, convert, keys, platform_services,
                      tasks as tasks_router, usage, chat, shares)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bff")


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    # 启动：数据目录就绪 + PG 连接池 + 管理员凭证预检（只读日志，不阻塞启动）
    store.ensure_data_dir()
    # ⚠️ 必须在首个数据访问之前：USE_PG=True 时 cloudstore 全靠 db.pool()，
    # 不 init 就是 None → 云端文档/媒体/请求日志接口全 500，表也不会建。
    # （2026-09-20 实锤：此前 lifespan 漏调，生产一直跑 SQLite 才没暴露。）
    await db.init_pool()
    if config.NEWAPI_BASE_URL_IS_DEFAULT:
        logger.warning("NEWAPI_BASE_URL 未配置，使用默认 %s（请确认这是目标网关）",
                       config.NEWAPI_BASE_URL)
    if not (config.NEWAPI_ADMIN_PAT and config.NEWAPI_ADMIN_UID) and not (
        config.NEWAPI_ADMIN_USERNAME and config.NEWAPI_ADMIN_PASSWORD
    ):
        logger.error("管理员凭证未配置：注册（影子建号）/ 加额度 / 管理台不可用。")
    # 启动兜底：同步出图是进程内后台协程，进程重启会丢在途任务 → 对应行永远停在
    # submitted（僵尸行，干扰排障）。启动时把超过 6 小时仍未终态的 submitted 标 failed。
    try:
        swept = await cloudstore.request_log_fail_stale(hours=6)
        if swept:
            logger.warning("启动清扫：%s 条超时 submitted 请求标记为 failed（服务重启丢任务）", swept)
    except Exception:  # noqa: BLE001 —— 清扫失败不阻塞启动
        logger.exception("启动清扫 stale submitted 失败")
    # 图片任务（同步/异步双模式）：BFF 不跑 worker；异步透传网关、同步阻塞直出后落盘，
    # 每次提交写一条请求日志（落 PG，见 app/tasks.py）。只需归还 httpx 连接池。
    yield
    await tasks.close()  # 归还网关代理 httpx 连接池
    await chat.close()  # 归还聊天代理 httpx 连接池
    await na.close()  # 归还 httpx 连接池
    try:
        from .thirdparty import wavespeed as ws

        await ws.close()  # 归还 WaveSpeed 直连 httpx 连接池
    except Exception:  # noqa: BLE001
        pass
    await db.close_pool()  # 归还 PG 连接池（docs/storage-architecture.md 语义）


app = FastAPI(title=config.SERVICE_NAME, docs_url=None, redoc_url=None, lifespan=_lifespan)
observability.setup(app)

# 统一响应壳 {success, message, data}（同 hewapi）。注意：FastAPI 自动 422 校验
# 失败返回的是 pydantic 默认形状，前端需兼容两种（参考 hewapi 前端处理）。
for r in (auth.router, keys.router, usage.router, console.router, convert.router, cloud.router, tasks_router.router, billing.router, chat.router, shares.router, platform_services.router):
    app.include_router(r)


# ---------- 异常处理：业务错误透传 message，内部错误不泄露 ----------
@app.exception_handler(NewApiError)
async def newapi_error_handler(request: Request, exc: NewApiError):
    return JSONResponse(status_code=exc.status_code,
                        content={"success": False, "message": exc.message})


@app.exception_handler(ModelSuspendedError)
async def model_suspended_handler(request: Request, exc: ModelSuspendedError):
    """平台已下架/已删除的模型被调用 —— 409 + 明确文案。

    ⚠️ 必须返回**统一响应壳**（`{success, message, code}`）而不是 FastAPI 默认的
    `{"detail": ...}`：前端 `hostedClient.api()` 只读 `body.message`，
    走默认形状会退化成「请求失败(409)」—— 那就等于把「模型已下架」这个
    关键信息又吞掉了（正是本次要修的毛病）。
    """
    logger.info("模型已下架被拒 %s %s model=%s code=%s",
                request.method, request.url.path, exc.model, exc.code)
    return JSONResponse(
        status_code=409,
        content={"success": False, "message": exc.message,
                 "code": exc.code, "data": {"model": exc.model, "reason": exc.reason}},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"success": False, "message": "服务器内部错误"})


# ---------- 探针 ----------
@app.get("/healthz")
async def healthz():
    """存活探针：进程能响应即可，不做语义判断。"""
    # 服务名与 Logfire 上报用的是同一个来源（config.SERVICE_NAME）：两处各写
    # 一份的话，将来改名只改一处，探针与可观测性会报出不同的名字。
    return ok({"service": config.SERVICE_NAME, "version": config.APP_VERSION})


_WEAK_SECRETS = {config.SECRET_KEY_DEFAULT, "changeme", "secret", "test", ""}


def _check_secret_key() -> tuple[bool, str]:
    key = config.SECRET_KEY
    if key in _WEAK_SECRETS:
        return False, "SECRET_KEY 未配置或为已知弱值"
    if len(key) < 32:
        return False, "SECRET_KEY 长度不足 32 字符"
    return True, "ok"


def _check_admin_cred() -> tuple[bool, str]:
    if (config.NEWAPI_ADMIN_PAT and config.NEWAPI_ADMIN_UID) or (
        config.NEWAPI_ADMIN_USERNAME and config.NEWAPI_ADMIN_PASSWORD
    ):
        return True, "ok"
    return False, "管理员凭证未配置（注册/管理台不可用）"


def _check_state_dir() -> tuple[bool, str]:
    # ⚠️ 不要做真实写探针：本机 D:\code 下（OneDrive/Defender 等文件过滤）每次写入
    # 会被实时扫描卡 ~5s，会让 /readyz 稳定超时、探针误判服务挂掉。就绪检查用 stat 即可。
    try:
        if not os.path.isdir(config.DATA_DIR):
            return False, f"数据目录不存在: {config.DATA_DIR}"
        if not os.access(config.DATA_DIR, os.W_OK):
            return False, f"数据目录不可写: {config.DATA_DIR}"
        return True, "ok"
    except OSError as e:
        return False, f"数据目录检查失败: {e}"


@app.get("/readyz")
async def readyz():
    """就绪探针：语义校验，不通过返回 503（容器编排据此重启/摘流）。"""
    checks = {
        "secret_key_configured": _check_secret_key(),
        "admin_cred_configured": _check_admin_cred(),
        "state_dir_writable": _check_state_dir(),
    }
    passed = all(v[0] for v in checks.values())
    body = {
        "success": passed,
        "checks": {k: (v[0], v[1]) for k, v in checks.items()},
        "version": config.APP_VERSION,
    }
    return JSONResponse(status_code=200 if passed else 503, content=body)


@app.get("/")
async def root():
    """BFF 根路径：创作站前端由另一个仓库构建部署（同域反代），此处仅探活提示。"""
    # 同上：服务名统一取 config.SERVICE_NAME，不要在这里写死字面量。
    return ok({"service": config.SERVICE_NAME, "message": "API is at /api/*",
               "docs": "见 ARCHITECTURE.md"})
