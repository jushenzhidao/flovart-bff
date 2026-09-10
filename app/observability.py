"""可观测性接缝（预留）。

当前为 no-op 桩：不引入任何外部依赖、不注册 hook —— 本地开发与 CI 零副作用。
后续接入 Logfire（或自建 OTel）时，把 hewapi-bff 的 app/observability.py 实现
搬过来即可（接口保持一致）：

    setup(app)             # FastAPI 应用埋点 + 日志接管
    instrument_httpx(c)    # 出站 new-api 调用埋点（newapi_client 单例创建后调用）

设计约束（同 hewapi）：
1. 未配 token 即完全关闭，不导入 SDK；
2. 任何一步失败只告警不抛异常 —— 可观测性不能把主应用带崩；
3. 会话 Cookie 里装着用户 new-api PAT，埋点必须对敏感字段（cookie/pat/token/
   password/api_key）做 scrub，凭证离开进程前就地清除。
"""
import logging
from typing import Any

logger = logging.getLogger("bff")


def setup(_app: Any) -> bool:
    """启用可观测性。未配 LOGFIRE_TOKEN 时返回 False（零副作用）。"""
    try:
        from . import config
    except ImportError:
        return False
    if not config.LOGFIRE_ENABLED:
        logger.info("observability disabled (no LOGFIRE_TOKEN)")
        return False
    # TODO(M4): 接入 Logfire —— 抄 hewapi-bff app/observability.py 实现。
    logger.warning("LOGFIRE_TOKEN 已配置但 observability 尚未接入（M4 TODO）")
    return False


def instrument_httpx(_client: Any) -> None:
    """挂 httpx 出站埋点。当前 no-op，保持接口稳定。"""
