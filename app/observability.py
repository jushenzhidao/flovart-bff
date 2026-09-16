"""Logfire 接线（可选组件，未配 token 即完全关闭）。

⚠️ 2026-09-16：本项目与 hewapi-bff 部署在**同一台机器**上，两边的 `.env` 一度
共用同一个 `LOGFIRE_TOKEN` 且 `LOGFIRE_ENVIRONMENT` 都是 `local`。若 service_name
也与对端相同（hewapi 把 "newapi-bff" 写死在代码里），两套服务的 trace 会在
Logfire 控制台里彻底混成一坨 —— 按服务过滤不出任何边界，排查时无法判断某条
报错出自哪套 BFF。因此本文件的 service_name **一律从 `config.SERVICE_NAME` 读**，
且生产部署要求**单开 Logfire 项目 + 独立 token**。

设计约束（同 hewapi，逐条都是踩过的）：
1. 未配 token 时不导入 SDK、不注册任何 hook —— 可观测性是运维增强，
   不该成为本地开发与 CI 的前置依赖，也不该让没配 token 的环境有行为差异。
2. 任何一步失败都只告警、不抛异常。让上报组件把主应用带崩，是用可观测性
   换掉可用性；这里的失败上限是「看不到 trace」，不能是「服务起不来」。
3. 敏感字段在 scrub 阶段就地清除，不依赖后端配置 —— PAT / Cookie / 密码
   一旦离开进程就无法召回，必须在发送前拦掉。

另外：本服务的运行日志**不落文件**。生产容器根文件系统 `read_only`，往容器内写
日志文件会直接抛 `OSError: [Errno 30] Read-only file system`。日志一律走两条腿：
stdout（由 docker 的 json-file driver 收集并轮转）+ 本模块的 Logfire 上报。
"""
import logging
from typing import Any

from . import config

logger = logging.getLogger("bff")

# 需要从 span 与日志中清除的字段名片段（小写匹配）。
# 会话 Cookie 里装着用户绑定的 new-api 凭证，密码走注册/绑定链路，
# 二者进了 trace 就等于在第三方留了一份长期有效的凭证。
#
# 成本说明：`key` 会连带命中 `mediaKey` 这类**非敏感**键（BFF 自己生成的产物 ID）。
# 这是刻意接受的过杀 —— 宁可排查时少看一个产物 ID，也不能漏出一个凭证。
_SENSITIVE_HINTS = (
    "authorization", "cookie", "set-cookie", "token", "pat",
    "password", "secret", "session", "access_token", "api_key", "key",
    # 连字符 / 无分隔变体：单靠 `api_key` 匹配不到 `api-key` 与 `apikey`
    "api-key", "apikey", "x-api-",
)

_configured = False


def _scrub(match: Any) -> Any:
    """Logfire scrubbing 回调：命中敏感字段名则替换为占位符。

    只判断字段路径而不看值，避免「值看起来不像密钥就放过」的漏判。
    """
    path = "/".join(str(p) for p in getattr(match, "path", ())).lower()
    pattern = str(getattr(match, "pattern_match", "") or "").lower()
    if any(hint in path or hint in pattern for hint in _SENSITIVE_HINTS):
        return "[scrubbed]"
    return None


def setup(app: Any) -> bool:
    """按需初始化 Logfire 并挂载 FastAPI 埋点。

    返回是否真正启用，便于启动日志如实反映状态（而不是假定成功）。
    """
    global _configured
    if not config.LOGFIRE_ENABLED:
        logger.info("logfire 未配置 LOGFIRE_TOKEN，可观测性上报已关闭")
        return False
    if _configured:
        # 幂等：重复 setup（如测试反复建 app）会让 instrument_fastapi 抛
        # 「already been instrumented」，误报成埋点失败。
        return True

    try:
        import logfire
    except ImportError:
        # 配了 token 但没装依赖，属于部署疏漏，值得 warning 而非静默。
        logger.warning("已配置 LOGFIRE_TOKEN 但未安装 logfire，上报关闭；执行 pip install logfire")
        return False

    try:
        logfire.configure(
            token=config.LOGFIRE_TOKEN,
            environment=config.LOGFIRE_ENVIRONMENT,
            # 从 config 读，不写死：见模块头 2026-09-16 说明。
            service_name=config.SERVICE_NAME,
            service_version=config.APP_VERSION,
            scrubbing=logfire.ScrubbingOptions(callback=_scrub),
            console=False,  # stdout 已有 logging 输出，重复打印只会淹没真实日志
        )
    # 宽泛捕获是刻意的：见模块头约束 2 —— 可观测性故障绝不能拖垮业务启动。
    except Exception as exc:
        logger.warning("logfire 初始化失败，上报关闭，服务继续运行: %s", exc)
        return False

    # configure 成功即可上报日志与手动 span，故先置位；
    # 下面的自动埋点是增量能力，任一缺失不应否定已经可用的部分。
    _configured = True

    # 接管标准 logging：现有 logger.info/error 调用无需改动即可进 Logfire
    logging.getLogger().addHandler(logfire.LogfireLoggingHandler())

    # 逐项独立容错：instrument_* 在缺少对应 extras 时抛 RuntimeError，
    # 若与上面共用一个 try，一个埋点缺包会连带跳过日志接管。
    fastapi_ok = _try_instrument("fastapi", logfire.instrument_fastapi, app, capture_headers=False)

    # 如实汇报实际挂上的埋点，避免「日志说已启用、实际什么都没埋」——
    # 可观测性静默失效比没有可观测性更危险，因为它让人误以为有覆盖。
    logger.info(
        "logfire 已启用 service=%s environment=%s version=%s fastapi_traces=%s",
        config.SERVICE_NAME, config.LOGFIRE_ENVIRONMENT, config.APP_VERSION,
        "on" if fastapi_ok else "off",
    )
    return True


def _try_instrument(name: str, fn: Any, *args: Any, **kwargs: Any) -> bool:
    """执行单个 instrument_* 调用，失败只告警。

    缺少 logfire extras 时 logfire 抛的是 RuntimeError 而非 ImportError，
    提示语里带上安装命令，免得只看到一句「失败」无从下手。
    """
    try:
        fn(*args, **kwargs)
        return True
    # 同上：逐项独立容错，单个埋点失败不连带跳过其余埋点。
    except Exception as exc:
        logger.warning(
            "logfire %s 埋点未挂载（该维度不上报，服务不受影响）: %s；"
            "缺依赖时执行 pip install 'logfire[fastapi,httpx]'",
            name, exc,
        )
        return False


def instrument_httpx(client: Any) -> None:
    """为 new-api 出站调用挂 httpx 埋点。

    单独成函数是因为 client 是懒加载单例（newapi_client 首次请求时才建），
    创建时机晚于应用启动，无法在 setup 里一并处理。
    """
    if not _configured:
        return
    try:
        import logfire
    except ImportError:
        return
    _try_instrument("httpx", logfire.instrument_httpx, client, capture_headers=False)
