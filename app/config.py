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

# Logfire 里的服务名（决定控制台按 service 过滤时归到哪一堆）。
# ⚠️ 2026-09-16 血案：本项目与 hewapi-bff **都部署在同一台机器上**，且两边的
#    .env 一度用的是**同一个 LOGFIRE_TOKEN + 同一个 environment=local**；而
#    hewapi 的 observability.py 把 service_name 写死成 "newapi-bff"。若这边照抄，
#    两套服务的 trace / 日志会在 Logfire 控制台里混成一坨 —— 按服务名过滤不出
#    任何一条边界，排查线上问题时无法判断某条报错到底出自哪套 BFF。
#    故此处**必须从环境变量读，绝不在代码里写死**。
# 更彻底的做法是给本项目**单开一个 Logfire 项目、换独立 token**：只改 service_name
#    仍与对端共用同一个额度池与数据留存策略，隔离不完整。
SERVICE_NAME: str = os.getenv("BFF_SERVICE_NAME", "flovart-bff").strip() or "flovart-bff"

# ---------- 图片任务（BFF 同时兼容同步 / 异步，见 IMAGE-ASYNC-TASKS-CONTRACT.md）----------
# 异步：真正执行方是网关侧（new-api 兼容）。BFF 透传「提交→轮询→取消」，不存任务状态。
# 同步：网关部分模型更适合同步直出（转异步成本高），BFF 阻塞调用网关同步接口、
#       拿到结果后立即把产物落 BFF 云盘并回写请求日志。前端走同一套「提交→轮询」。
#
# ⭐⭐ 2026-09-15 关键修复：**所有 GATEWAY_*_PATH 必须带 `v1/` 前缀**。
#   网关（new-api）的 OpenAI 兼容端点全部挂在 **`/v1/*`** 下，`base_url` 是
#   `NEWAPI_BASE_URL`（不带 /v1），故相对路径必须自带 `v1/`。
#   ⛔ 漏掉 `/v1` 的后果（飞哥 2026-09-15 实报）：请求打到 `{base}/images/generations`
#   —— 该路径在 nginx 上**不存在** → 被兜底给 new-api 的**前端 SPA** →
#   返回 `200 text/html`（`<title>New API</title>`）而非 JSON →
#   BFF 解析失败 → 前端看到 **502 Bad Gateway** + 「网关未实现该图片端点」。
#   📌 免责提醒：报错文案里的「网关未实现该端点」是**误判**，实际是路径缺 /v1。
#   实测对照（2026-09-15）：
#     POST /v1/images/generations  → 401 application/json  ✅ 端点存在
#     POST /images/generations     → 200 text/html         ❌ 前端页面兜底
#   同族正确用法参考：newapi_client 里 "/v1/models"、chat.py 里 "/v1/chat/completions"
#   都显式带 `v1/` —— 各 env 仍可覆盖（填值时**也别忘了 v1/**）。
# ⭐⭐ 2026-09-15 二次修复：**异步任务端点是 `v1/video/generations`，不是
#   `v1/contents/generations/tasks`**。原默认值系早期按契约文档（未实测）填写，
#   实测 `POST /v1/contents/generations/tasks` → **404 application/json**（路径不存在）。
#   依据 new-api 源码 `router/video-router.go`（SetVideoRouter）：
#     POST   /v1/video/generations             ← 提交异步任务（controller.RelayTask）
#     GET    /v1/video/generations/:task_id    ← 轮询任务（controller.RelayTaskFetch）
#     POST   /v1/videos                        ← OpenAI 兼容别名（同样走 RelayTask）
#     GET    /v1/videos/:video_id              ← OpenAI 兼容别名轮询
#   四个端点 2026-09-15 实测均返回 `401 application/json`（= 端点存在，仅缺有效 token）✅
#   note：new-api 的「图片/视频异步任务」**共用同一个 video 路由组**，靠请求体里的
#   模型/type 分流，BFF 无需为图片另开端点 —— 故 image / video 两个 env 默认同值。
GATEWAY_IMAGE_TASKS_PATH: str = os.getenv(
    "GATEWAY_IMAGE_TASKS_PATH", "v1/video/generations"
).strip().strip("/")  # 异步 tasks 端点（相对 NEWAPI_BASE_URL，含 v1 前缀）
# 视频异步：与图片共用 new-api 的统一 video 任务端点（BFF 不感知具体分流）。
# 若网关后续把视频拆到独立端点，用此 env 覆盖。
GATEWAY_VIDEO_TASKS_PATH: str = os.getenv(
    "GATEWAY_VIDEO_TASKS_PATH", "v1/video/generations"
).strip().strip("/")
# 同步端点（相对 NEWAPI_BASE_URL，含 v1 前缀）。
#
# ⭐ 2026-09-14 收敛（飞哥拍板）：**所有图片能力统一走 `v1/images/generations`**。
#   理由：上游网关（如 api.chatfire.cn）**只实现 `/images/generations`，
#   不实现 `/images/edits` 等语义化端点**（打过去必 404）。文生图 / 图生图 / 编辑
#   在上游本就是同一个端点，靠入参（`image` 数组 / `mask` / `variant` / `task`）区分。
#   因此这里不再为每个能力分配独立路径 —— 具体能力由请求体里的字段表达，
#   网关侧按字段翻译到上游。各 env 仍保留（便于个别能力后续若真拆出独立端点时覆盖）。
GATEWAY_SYNC_IMAGE_PATH: str = os.getenv("GATEWAY_SYNC_IMAGE_PATH", "v1/images/generations").strip().strip("/")
GATEWAY_SYNC_UPSCALE_PATH: str = os.getenv("GATEWAY_SYNC_UPSCALE_PATH", "v1/images/generations").strip().strip("/")
GATEWAY_SYNC_REMOVE_BG_PATH: str = os.getenv("GATEWAY_SYNC_REMOVE_BG_PATH", "v1/images/generations").strip().strip("/")
GATEWAY_SYNC_SPLIT_PATH: str = os.getenv("GATEWAY_SYNC_SPLIT_PATH", "v1/images/generations").strip().strip("/")
# 图片编辑类（原纯 BYOK 直连，现走 BFF→网关，用户免 Key）：
# 扩展画面 / 编辑蒙版 / 标注涂鸦 / 打光面板 / 通用编辑（换装预处理等）。
# 同上：统一打 v1/images/generations，靠 body 里的 variant 字段区分具体编辑能力。
GATEWAY_SYNC_OUTPAINT_PATH: str = os.getenv("GATEWAY_SYNC_OUTPAINT_PATH", "v1/images/generations").strip().strip("/")
GATEWAY_SYNC_MASK_PATH: str = os.getenv("GATEWAY_SYNC_MASK_PATH", "v1/images/generations").strip().strip("/")
GATEWAY_SYNC_ANNOTATE_PATH: str = os.getenv("GATEWAY_SYNC_ANNOTATE_PATH", "v1/images/generations").strip().strip("/")
GATEWAY_SYNC_RELIGHT_PATH: str = os.getenv("GATEWAY_SYNC_RELIGHT_PATH", "v1/images/generations").strip().strip("/")
GATEWAY_SYNC_EDIT_PATH: str = os.getenv("GATEWAY_SYNC_EDIT_PATH", "v1/images/generations").strip().strip("/")
# image-gen 的提交模式。
#
# ⭐⭐ 2026-09-15 实测改为默认 **sync**（此前默认 async，导致用户生图必失败）：
#   本网关（new-api 兼容层）的**图片模型只支持同步端点 `v1/images/generations`**。
#   而异步端点 `v1/video/generations` 是**视频任务语义**，网关会按 video 方式拼上游
#   URL → 图片模型打过去必然 `404 fail_to_fetch_task`（上游 Not Found）。
#   实测（用户 sk-，同一模型 gpt-image-2.5-flare）：
#     POST /v1/images/generations  → **200**，63.5s 真实出图（返回 b64_json）✅
#     POST /v1/video/generations   → 404 fail_to_fetch_task（上游 Not Found）❌
#   ⚠️ 除非网关为图片实现真正的异步任务端点，否则**不要改回 async**。
#   sync 模式下前端体验不变（仍是「提交 → 轮询 /api/tasks/{id}」），
#   只是 BFF 在后台阻塞等待网关出图（`_run_sync` + 后台任务，不占用前端连接）。
GATEWAY_IMAGE_GEN_MODE: str = _choice("GATEWAY_IMAGE_GEN_MODE", "sync", ("async", "sync"))
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
