"""BFF 配置（解析约定与 hewapi-bff 一致）。

生产环境务必用环境变量注入 BFF_SECRET_KEY / NEWAPI_ADMIN_*。
弱值（默认 SECRET_KEY、空管理员凭证）由 /readyz 语义校验拦截。
"""
import os
from pathlib import Path

# 加载项目根 .env。必须在本模块任何 os.getenv 之前执行。
# override=False 是刻意的：真实环境变量（compose environment、CI secrets）的
# 优先级必须高于 .env。BFF_SKIP_DOTENV=1 用于测试：测试结论只由夹具决定，
# 不能随开发者本机 .env 的内容而变。
if os.getenv("BFF_SKIP_DOTENV") != "1":
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
    except ImportError:
        pass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    """解析布尔环境变量；未设置或留空时回落到 default（空值不能当 False）。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    raw = (os.getenv(name) or "").strip().lower()
    return raw if raw in allowed else default


# ---------- new-api 网关 ----------
# 本地默认 127.0.0.1:3000（new-api 常用端口）。部署时不注入即为默认，
# 显式打标供启动日志与 /readyz 告警，别让默认值悄悄生效。
NEWAPI_BASE_URL: str = os.getenv("NEWAPI_BASE_URL", "http://127.0.0.1:3000")
NEWAPI_BASE_URL_IS_DEFAULT: bool = "NEWAPI_BASE_URL" not in os.environ

# 管理员凭证：PAT（推荐，不碰会话系统）> 落盘缓存 > 账密 login 兜底。
# 不留默认值：源码里的默认账密会随仓库分发。未配置时由
# newapi_client._admin_login() 在真正需要管理员权限的调用上报错。
#
# ⚠️ 多业务隔离（踩坑结论 2026-09-07）：每个业务应用必须使用【独立】的 new-api
# 管理员账号（独立 uid），严禁多个 BFF / 业务应用共用同一管理员账号！原因：
#   1) PAT 是账号级、每次 /api/user/token 调用都会重新生成并【作废旧 PAT】。
#      共用账号时，任一业务换 PAT 会瞬间踢掉其他所有业务持有的 PAT，引发
#      401→重登→再互踢的雪崩（flovart-bff 与 hewapi-bff 曾写死同一 PAT 即此坑）。
#   2) new-api 会话上限 50 且【硬拒绝、不淘汰最旧】，共用账号会互相挤占配额，
#      峰值打满直接 409 AUTH_SESSION_LIMIT，建号/加额度/管理台全瘫痪。
# 正确姿势：在 new-api 用 root 后台为 flovart 另建专属管理员账号（role>=10、独立强密码），
# 本仓库 .env 只填该账号的 NEWAPI_ADMIN_PAT/UID（或专属账密）。独立账号后
# request_as_user 代用户发请求的隔离逻辑完全不变（New-Api-User 仍指向目标用户 uid）。
NEWAPI_ADMIN_USERNAME: str = os.getenv("NEWAPI_ADMIN_USERNAME", "").strip()
NEWAPI_ADMIN_PASSWORD: str = os.getenv("NEWAPI_ADMIN_PASSWORD", "")
NEWAPI_ADMIN_PAT: str = os.getenv("NEWAPI_ADMIN_PAT", "").strip()
NEWAPI_ADMIN_UID: int = _int("NEWAPI_ADMIN_UID", 0)

# 管理员 PAT 落盘缓存：进程重启后直接复用，避免每次冷启都 login 消耗一个会话。
ADMIN_CRED_FILE: str = os.getenv(
    "BFF_ADMIN_CRED_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "admin_cred.json"),
)

# BFF 管理员的静态名单兜底（普通判定走上游 user.role >= 10）。
ADMIN_USERNAMES: frozenset = frozenset(
    u.strip() for u in os.getenv("BFF_ADMIN_USERNAMES", "").split(",") if u.strip()
)

# ---------- 会话 Cookie ----------
SECRET_KEY_DEFAULT = "dev-only-secret-change-me"  # noqa: S105 —— /readyz 黑名单哨兵值
SECRET_KEY: str = os.getenv("BFF_SECRET_KEY", SECRET_KEY_DEFAULT)

COOKIE_NAME = "bff_session"
COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 天；PAT 长期有效，不受 15min access_token 限制

# 明文 uid 镜像 Cookie：前端首屏同步读取，用于给本地存储挑命名空间（多账号隔离）。
# 非凭据 —— 服务端一律以会话 Cookie 内的 uid 为准，不读这个值。
UID_COOKIE_NAME = "bff_uid"

# Secure 默认 True：Cookie 载荷含用户 PAT，明文 HTTP 下会被泄露。
# 唯一该置 0 的场景是本地开发 http://127.0.0.1。
COOKIE_SECURE: bool = _bool("BFF_COOKIE_SECURE", True)

# SameSite 默认 lax：写操作全是 POST，防护足够；详见 hewapi config 注释。
COOKIE_SAMESITE: str = _choice("BFF_COOKIE_SAMESITE", "lax", ("lax", "strict", "none"))

# ---------- 积分体系（对外唯一计价单位）----------
# new-api 内部余额单位是 quota，1 元 = QUOTA_PER_CNY（其 QuotaPerUnit 默认值）。
# 对外统一「积分」：1 元 = POINTS_PER_CNY 积分。
QUOTA_PER_CNY: int = _int("BFF_QUOTA_PER_CNY", 500000)
POINTS_PER_CNY: int = _int("BFF_POINTS_PER_CNY", 10000)
POINTS_UNIT_NAME: str = os.getenv("BFF_POINTS_UNIT_NAME", "积分")


def quota_to_points(quota) -> int:
    """内部 quota → 对外积分（向下取整，绝不虚报余额）。用于余额、聚合。"""
    if not QUOTA_PER_CNY:
        return 0
    return int(float(quota or 0) / QUOTA_PER_CNY * POINTS_PER_CNY)


def quota_to_points_exact(quota) -> float:
    """quota → 积分（保留 4 位小数）。单条日志明细必用：1 积分 = 50 quota，
    小请求的 quota 常在 10~49 之间，整数取整后恒为 0，明细会全是「-」。
    """
    if not QUOTA_PER_CNY:
        return 0.0
    return round(float(quota or 0) / QUOTA_PER_CNY * POINTS_PER_CNY, 4)


def points_to_quota(points) -> int:
    """对外积分 → 内部 quota（四舍五入，赠送场景宁可多给一点点）。"""
    if not POINTS_PER_CNY:
        return 0
    return int(round(float(points or 0) * QUOTA_PER_CNY / POINTS_PER_CNY))


# ---------- 注册赠送 ----------
PROMO_SIGNUP_ENABLED: bool = _bool("BFF_PROMO_SIGNUP_ENABLED", True)
PROMO_SIGNUP_POINTS: int = _int("BFF_PROMO_SIGNUP_POINTS", 20000)  # 2 万积分（=¥2）

# 注册赠送幂等账本（BFF 无 DB，用本地 JSON 保证 add_quota 不重复发放）
SIGNUP_STATE_FILE: str = os.getenv(
    "BFF_SIGNUP_STATE_FILE",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "signup_bonus.json",
    ),
)

# ---------- 可写数据目录 ----------
# 运行期写入的状态文件都落这里。容器以 read_only 根文件系统运行时用
# BFF_DATA_DIR 指向挂载卷（默认是仓库下 data/）。
DATA_DIR: str = os.getenv("BFF_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)

# ---------- 外部对象存储（OSS / COS / S3 兼容）—— 媒体字节层 ----------
# 创作产物（图片/视频/音频二进制）统一落外部对象存储，BFF 联网读写，不落本地磁盘。
# 用同一套 boto3 S3 协议接入三大云，差异靠以下 env 消除：
#   腾讯云 COS：OSS_ENDPOINT=https://cos.<region>.myqcloud.com  OSS_ADDRESSING_STYLE=virtual
#   阿里云 OSS：OSS_ENDPOINT=https://oss-<region>.aliyuncs.com   OSS_ADDRESSING_STYLE=path
#    AWS S3   ：不填 OSS_ENDPOINT，OSS_REGION=ap-xxx            OSS_ADDRESSING_STYLE=auto
OSS_ENABLED: bool = _bool("OSS_ENABLED", False)          # 关闭则回落本地文件（开发用）
OSS_PROVIDER: str = os.getenv("OSS_PROVIDER", "cos").strip()   # cos / oss / s3（仅文档/默认值提示）
OSS_ENDPOINT: str = os.getenv("OSS_ENDPOINT", "").strip()
OSS_REGION: str = os.getenv("OSS_REGION", "ap-guangzhou").strip()
OSS_BUCKET: str = os.getenv("OSS_BUCKET", "").strip()
OSS_ACCESS_KEY: str = os.getenv("OSS_ACCESS_KEY", "").strip()    # AK/SK 访问密钥
OSS_SECRET_KEY: str = os.getenv("OSS_SECRET_KEY", "").strip()
OSS_PREFIX: str = os.getenv("OSS_PREFIX", "flovart").strip()     # 对象键前缀（同桶多环境隔离）
OSS_ADDRESSING_STYLE: str = os.getenv("OSS_ADDRESSING_STYLE", "auto").strip()
OSS_PRESIGN_TTL: int = _int("OSS_PRESIGN_TTL", 3600)             # 前端读取 presigned URL 有效期（秒）
OSS_ENFORCE_QUOTA: bool = _bool("OSS_ENFORCE_QUOTA", True)
OSS_QUOTA_BYTES: int = _int("OSS_QUOTA_BYTES", 2 * 1024 * 1024 * 1024)  # 每用户配额（0=不限）

# ---------- PostgreSQL（元数据/索引层）----------
# 结构化元数据（KV 文档 + 媒体索引 + 配额）落 PostgreSQL，替代本地 SQLite（多副本可共享）。
# 优先用 POSTGRES_DSN；缺省时由分项拼装。二者皆空 → 回落本地 SQLite（开发用）。
POSTGRES_DSN: str = os.getenv("POSTGRES_DSN", "").strip()
POSTGRES_HOST: str = os.getenv("POSTGRES_HOST", "localhost").strip()
POSTGRES_PORT: int = _int("POSTGRES_PORT", 5432)
POSTGRES_USER: str = os.getenv("POSTGRES_USER", "").strip()
POSTGRES_PASSWORD: str = os.getenv("POSTGRES_PASSWORD", "").strip()
POSTGRES_DB: str = os.getenv("POSTGRES_DB", "flovart").strip()


def _postgres_dsn() -> str:
    if POSTGRES_DSN:
        return POSTGRES_DSN
    if POSTGRES_USER and POSTGRES_HOST:
        return f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
    return ""


POSTGRES_DSN_RESOLVED: str = _postgres_dsn()
USE_PG: bool = bool(POSTGRES_DSN_RESOLVED)

# ---------- 品牌与站点 ----------
BRAND_NAME: str = os.getenv("BFF_BRAND_NAME", "Flovart")
BRAND_TAGLINE: str = os.getenv("BFF_BRAND_TAGLINE", "Workflow · 在线创作站")
BRAND_ICP: str = os.getenv("BFF_BRAND_ICP", "").strip()          # 备案号，留空不显示
BRAND_CONTACT: str = os.getenv("BFF_BRAND_CONTACT", "").strip()  # 客服联系方式

# 创作站前端直连 new-api 时展示/注入的 API 地址（{base}/v1 OpenAI 兼容端点）
API_BASE_URL: str = os.getenv("BFF_API_BASE_URL", NEWAPI_BASE_URL.rstrip("/") + "/v1")

APP_VERSION: str = os.getenv("BFF_VERSION", "dev")

# ---------- 可观测性（Logfire，留空即关闭）----------
LOGFIRE_TOKEN: str = os.getenv("LOGFIRE_TOKEN", "").strip()
LOGFIRE_ENVIRONMENT: str = os.getenv("LOGFIRE_ENVIRONMENT", "local").strip()
LOGFIRE_ENABLED: bool = bool(LOGFIRE_TOKEN)

# ---------- 图片任务（BFF 同时兼容同步 / 异步，见 IMAGE-ASYNC-TASKS-CONTRACT.md）----------
# 异步：真正执行方是网关侧（new-api 兼容）。BFF 透传「提交→轮询→取消」，不存任务状态。
# 同步：网关部分模型更适合同步直出（转异步成本高），BFF 阻塞调用网关同步接口、
#       拿到结果后立即把产物落 BFF 云盘并回写请求日志。前端走同一套「提交→轮询」。
# 各 task type 的 mode（sync/async）+ 网关端点见 app/tasks.py:TASK_TYPES（默认值，可 env 覆盖）。
GATEWAY_IMAGE_TASKS_PATH: str = os.getenv(
    "GATEWAY_IMAGE_TASKS_PATH", "contents/generations/tasks"
).strip().strip("/")  # 异步 tasks 端点（相对 NEWAPI_BASE_URL）
# 视频异步：new-api 用「统一 tasks 端点」按 type 分流（图片/视频同端点，BFF 不感知具体端点）。
# 若网关后续把视频拆到独立端点，用此 env 覆盖（默认与图片 tasks 端点一致）。
GATEWAY_VIDEO_TASKS_PATH: str = os.getenv(
    "GATEWAY_VIDEO_TASKS_PATH", "contents/generations/tasks"
).strip().strip("/")
# 同步端点（相对 NEWAPI_BASE_URL）。默认值待网关团队确认，可用同名 env 覆盖。
GATEWAY_SYNC_IMAGE_PATH: str = os.getenv("GATEWAY_SYNC_IMAGE_PATH", "images/generations").strip().strip("/")
GATEWAY_SYNC_UPSCALE_PATH: str = os.getenv("GATEWAY_SYNC_UPSCALE_PATH", "images/upscale").strip().strip("/")
GATEWAY_SYNC_REMOVE_BG_PATH: str = os.getenv("GATEWAY_SYNC_REMOVE_BG_PATH", "images/remove-bg").strip().strip("/")
GATEWAY_SYNC_SPLIT_PATH: str = os.getenv("GATEWAY_SYNC_SPLIT_PATH", "images/split-layers").strip().strip("/")
# 图片编辑类（原纯 BYOK 直连，现走 BFF→网关，用户免 Key）：
# 扩展画面 / 编辑蒙版 / 标注涂鸦 / 打光面板 / 通用编辑（换装预处理等）。
# 端点路径为约定占位，待网关侧接入对应能力后端点对点联调。
GATEWAY_SYNC_OUTPAINT_PATH: str = os.getenv("GATEWAY_SYNC_OUTPAINT_PATH", "images/outpaint").strip().strip("/")
GATEWAY_SYNC_MASK_PATH: str = os.getenv("GATEWAY_SYNC_MASK_PATH", "images/mask").strip().strip("/")
GATEWAY_SYNC_ANNOTATE_PATH: str = os.getenv("GATEWAY_SYNC_ANNOTATE_PATH", "images/annotate").strip().strip("/")
GATEWAY_SYNC_RELIGHT_PATH: str = os.getenv("GATEWAY_SYNC_RELIGHT_PATH", "images/relight").strip().strip("/")
GATEWAY_SYNC_EDIT_PATH: str = os.getenv("GATEWAY_SYNC_EDIT_PATH", "images/edits").strip().strip("/")
# image-gen 默认走异步；若网关该模型同步更好用，置 GATEWAY_IMAGE_GEN_MODE=sync 切换为同步直出。
GATEWAY_IMAGE_GEN_MODE: str = _choice("GATEWAY_IMAGE_GEN_MODE", "async", ("async", "sync"))
# 同步调用网关的阻塞超时（秒）：同步生成可能较长，给足余量（前端仍走轮询，BFF 内部 await）。
# 单 worker 下用异步 httpx 长超时不会阻塞事件循环，其他请求仍可并发处理。
GATEWAY_SYNC_TIMEOUT: int = _int("GATEWAY_SYNC_TIMEOUT", 300)
# 异步代理（提交/查询/取消）的 HTTP 超时（秒）。网关应快速 ACK（异步立即返回 task_id），
# 默认 60s 已留足排队校验余量。
GATEWAY_PROXY_TIMEOUT: int = _int("BFF_GATEWAY_PROXY_TIMEOUT", 60)

# ---------- 聊天补全代理（/api/chat/completions，走 new-api 网关，用户免 Key）----------
# 文本聊天 SSE 流式代理的 HTTP 超时（秒）。聊天可能较长，给足余量。
CHAT_STREAM_TIMEOUT: int = _int("BFF_CHAT_STREAM_TIMEOUT", 300)
# 前端未指定 model 时使用的默认聊天模型（需是网关已接入的模型名）。
# 留空则要求前端每次显式传 model；两者皆空 → 400。
CHAT_DEFAULT_MODEL: str = os.getenv("BFF_CHAT_DEFAULT_MODEL", "").strip()
# 引用图片（多模态）时使用的视觉模型：当聊天请求 messages 含 image 内容，
# 自动回退到此模型（即使前端传的是纯文本默认模型），避免纯文本模型吃图 400。
# 需是网关已接入的视觉模型名（如 gpt-4.1-mini / claude-sonnet-4-5）。
# 留空则不做自动切换，由前端显式传 vision 模型名。
CHAT_VISION_MODEL: str = os.getenv("BFF_CHAT_VISION_MODEL", "").strip()

# ---------- 第三方直连 Provider（绕过 new-api 网关，用于网关未接入的能力）----------
# 用途：new-api 网关未接入多视角 / 3D / 视频特效等能力时，BFF 直连第三方 API。
# 当前落地：wavespeed.ai（多角度 multi-angle）。
# ⚠️ Key 仅存 env，BFF 注入 Authorization header，绝不进代码、绝不暴露给前端。
WAVESPEED_API_KEY: str = os.getenv("WAVESPEED_API_KEY", "").strip()
WAVESPEED_BASE_URL: str = os.getenv("WAVESPEED_BASE_URL", "https://api.wavespeed.ai/api").rstrip("/")
# 多角度默认模型：FLUX Kontext Max Multi（多参考图上下文、主体一致性最强，$0.08/run）。
# 备选：wavespeed-ai/uno（角色/商品一致性 $0.05）、wavespeed-ai/flux-kontext-dev/multi。
WAVESPEED_MULTIANGLE_MODEL: str = os.getenv(
    "WAVESPEED_MULTIANGLE_MODEL", "wavespeed-ai/flux-kontext-max/multi").strip()
WAVESPEED_SPLIT_MODEL: str = os.getenv(
    "WAVESPEED_SPLIT_MODEL", "wavespeed-ai/qwen-image/layered").strip()
WAVESPEED_TIMEOUT: int = _int("WAVESPEED_TIMEOUT", 300)            # 单次提交/轮询 HTTP 超时（秒）
WAVESPEED_POLL_INTERVAL: int = _int("WAVESPEED_POLL_INTERVAL", 2)  # 轮询间隔（秒）
WAVESPEED_POLL_MAX: int = _int("WAVESPEED_POLL_MAX", 120)          # 最大轮询次数（≈ MAX*INTERVAL 秒）
WAVESPEED_ENABLED: bool = bool(WAVESPEED_API_KEY)                  # 无 key 时 /readyz 提示但不崩
