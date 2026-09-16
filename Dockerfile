# flovart-bff 生产镜像：多阶段构建，运行层不含编译工具链与 pip 缓存。
#
# ⚠️ 与 hewapi-bff 的隔离说明（2026-09-16）：
#   两者**部署在同一台机器**上，且 hewapi-bff 是本项目的参考实现（同源骨架）。
#   凡是有固定名的资源都必须显式改名，否则会互相覆盖 —— 最致命的是
#   container_name（hewapi 写死 newapi-bff），同名会让第二个容器直接起不来。
#   本文件与 docker-compose.yml 中的镜像名一律为 flovart-bff。
#
# 刻意不写 `# syntax=` 指令：本文件只用标准 Dockerfile 语法，
# 省掉每次构建都要联网拉 frontend 镜像这一步。
#
# 阶段划分的意义：builder 里装依赖（可能拉编译器），runtime 只复制装好的
# site-packages，最终镜像里没有 gcc、没有 pip cache、没有 .git。

# 基础镜像版本参数化：便于在拉不到 3.13 的环境（内网/镜像站滞后）降到 3.12，
# 代码本身兼容 >=3.12（见 pyproject.toml）
ARG PYTHON_IMAGE=python:3.13-slim

# ---------- builder ----------
FROM ${PYTHON_IMAGE} AS builder

# pip 源参数化。这不是洁癖，是国内服务器的必需品：本项目依赖含 fastapi /
# asyncpg / boto3 / Pillow / psd-tools / logfire 等几十个包，直连 pypi.org
# 常见「下载极慢 → Read timed out → 整次构建白跑」。构建失败在这一层时，
# 加下面这个 build arg 重试即可，无需改文件：
#   PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple docker compose build
# 默认留官方源，保证境外/CI 环境行为不变。
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# 依赖单独一层：只要 requirements.txt 不变，改业务代码时这层命中缓存
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir \
      --index-url "${PIP_INDEX_URL}" \
      -r requirements.txt

# ---------- runtime ----------
# 重复声明：ARG 在 FROM 之前定义的作用域到此失效，这是 Dockerfile 的既定行为
ARG PYTHON_IMAGE=python:3.13-slim
FROM ${PYTHON_IMAGE} AS runtime

# 镜像元信息，便于回溯线上跑的到底是哪个 commit
ARG APP_VERSION=dev
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="flovart-bff" \
      org.opencontainers.image.description="Flovart 在线创作站 BFF（FastAPI）" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    BFF_VERSION="${APP_VERSION}" \
    # Logfire 上报时的服务名。**必须显式声明**：本项目与 hewapi-bff 同机，
    # 两边曾共用同一个 LOGFIRE_TOKEN + environment=local，若服务名也相同，
    # 两套 trace 会在控制台里混成一坨。理由详见 app/observability.py 模块头。
    BFF_SERVICE_NAME=flovart-bff \
    # 可写数据目录。根文件系统以 read_only 运行，/app 不可写，所有落盘状态
    # 必须在挂载卷里，否则写入抛 OSError: [Errno 30] Read-only file system。
    # 下面逐项显式声明而非只靠 BFF_DATA_DIR：显式路径在 docker inspect 里
    # 一眼可见，运维排查「文件到底写哪了」不用去翻代码的默认值。
    BFF_DATA_DIR=/data \
    # ⚠️ 这两项**必须覆盖**，不能依赖 config.py 的默认值：
    #   默认值是「仓库根目录/data/」，即容器内的 /app/data —— read_only 下不可写。
    #   写管理员 PAT 缓存（文件不存在时先建）或首次注册赠送时，会直接抛 Errno 30。
    #   config.py 的默认值是为「单机裸跑」设计的，容器里必须显式改指 /data。
    BFF_ADMIN_CRED_FILE=/data/admin_cred.json \
    BFF_SIGNUP_STATE_FILE=/data/signup_bonus.json \
    # 只信任本机来的 X-Forwarded-For。app 里取 XFF 首段转给 new-api 做按 IP 限流，
    # 若信任任意来源，客户端自带一个伪造 XFF 就能绕过限流。反代不在同一
    # network namespace 时，改为反代的实际来源 IP。
    FORWARDED_ALLOW_IPS=127.0.0.1

# 刻意不 apt install curl：健康检查改用 Python 标准库（见下方 HEALTHCHECK）。
# 理由有两条，都不是洁癖：
#   1) 构建不再依赖 apt 源可达 —— 内网/受限网络下 apt-get update 失败会直接
#      让整个镜像构建挂掉，而它换来的只是一个探针用的 curl。
#   2) 少装一个带 TLS 栈的二进制，就少一条要跟 CVE 的依赖。

# 非 root 运行：容器逃逸时攻击者拿到的是无特权账号
RUN groupadd --system --gid 10001 bff \
 && useradd --system --uid 10001 --gid bff --no-create-home bff

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# 只拷运行必需的东西。scripts/ 是运维探针脚本、tests/ 是测试、根目录的
# demo_* 是本地演示素材，都不该进生产镜像（少一个文件少一处攻击面）。
# 注意：本项目没有 static/（前端独立仓库）、docs/ 只是设计文档、运行时不读，
# 故都不拷 —— 与 hewapi 不同，别照抄它的 COPY 行。
COPY --chown=bff:bff app/ ./app/

# /data 存放 admin_cred.json（含管理员 PAT）、user_keys.json（用户 sk- 的
# AES-GCM 密文）、signup_bonus.json、flovart_cloud.db 与 media/ 回落目录。
# 目录权限收到 700 —— admin_cred 与 user_keys 都等同凭证。
RUN mkdir -p /data && chown bff:bff /data && chmod 700 /data
VOLUME ["/data"]

USER bff
EXPOSE 8000

# 探针打 /healthz（不触碰上游），上游抖动不会导致容器被判定不健康而重启。
# 用 python -c 而非 curl：镜像里没装 curl（理由见上），Python 一定在。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

# 单 worker：注册赠送的幂等靠 app/promo.py 的 asyncio.Lock（只在进程内有效）+
# signup_bonus.json 账本，多 worker 会让同一用户重复领到赠送。
# 要扩容就横向加副本 + 共享存储，或先把幂等状态迁到 PG。
#
# 用 exec 形式的 shell 包装，只为让 FORWARDED_ALLOW_IPS 可被 compose 覆盖；
# exec 保证 uvicorn 是 PID 1，能直接收到 SIGTERM 优雅退出（触发 lifespan 里的
# shutdown 钩子，归还 httpx / asyncpg / boto3 连接池）。
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips \"$FORWARDED_ALLOW_IPS\""]
