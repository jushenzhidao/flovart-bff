# deployment（部署 / 容器化 / 与 hewapi 的同机隔离 · 2026-09-16 建立）

> MEMORY.md 只留摘要，本文件是完整清单与踩坑记录。

## 一、两套 BFF 的隔离契约（flovart-bff vs hewapi-bff）

**前提**：两者**部署在同一台机器**上（飞哥 2026-09-16 确认），且同源
（`D:/code/hewapi-bff/newapi-bff` 是参考实现）。凡有固定名的资源必须改名。

| 资源 | hewapi 线上 | flovart-bff | 不改的后果 |
|---|---|---|---|
| compose project | 目录名推导 | `name: flovart-bff`（显式） | 卷/网络名撞车 |
| `container_name` | `newapi-bff` | `flovart-bff` | **同名冲突，第二个容器起不来** |
| `image` | `newapi-bff:${APP_VERSION}` | `flovart-bff:${APP_VERSION}` | 镜像互相覆盖 |
| volume | `bff-data` | `flovart-bff-data`（带绝对 `name:`） | 数据串味（PAT / 用户密钥混） |
| 宿主端口 | `${BFF_PORT:-8000}` | `${FLOVART_BFF_PORT:-8300}` | 端口占用 |
| Logfire token | `pylf_v1_us_2tpyfKl…` | **必须换独立项目** | trace 混成一坨 |
| Logfire environment | `local`（线上也是，属疏忽） | `flovart-prod` | 同上 |
| Logfire service_name | `newapi-bff`（**代码硬编码**） | `config.SERVICE_NAME`（可覆盖） | 同上 |

**目录层级的影响已被消除**：compose 顶层写了 `name: flovart-bff`，卷写了
`name: flovart-bff-data`（绝对名，不带 project 前缀）→ 项目放哪个目录、几层深，
项目名与卷名都不变。这样将来从「不同机器」合并到「同机」时零改动，也不会
因为改目录名导致「数据卷凭空消失」。

**数据面天然不撞**：hewapi **完全不用 PG / OSS**（纯 JSON 文件 + `/data` 卷，
`grep -cE "POSTGRES|OSS_ENABLED" app/config.py` = 0）。flovart 用 PG/OSS 时，
只要 database 名（`POSTGRES_DB=flovart`）与对象前缀（`OSS_PREFIX=flovart`）保持
专属即可。

**同机还需注意**：Nginx 反代要分别指向 8000（hewapi）与 8300（flovart）；
new-api 管理员账号必须各自独立（PAT 是账号级的，共用会互踢 401）。

## 二、日志落地方式（2026-09-16 已接入 Logfire）

**刻意不写文件日志**，两条腿：

1. **stdout** → compose `logging: json-file, max-size 10m, max-file 5`
   → 宿主机 `/var/lib/docker/containers/<id>/<id>-json.log`
   → 查看 `docker compose logs -f --tail=200 bff`
2. **Logfire** → `app/observability.py`（116 行真实现）

### 为什么不能用 FileHandler
compose 里 `read_only: true`（根文件系统只读）。往容器内写日志文件直接抛
`OSError: [Errno 30] Read-only file system`。要写就写 `/data` 卷 —— 但设计上
选择不写，交给 docker 轮转 + Logfire。

### observability.py 的降级契约（改动时别破坏）
- 未配 `LOGFIRE_TOKEN` → **不 import SDK**、`setup()` 返回 `False`、零副作用
- 任何一步失败只 `logger.warning`，绝不抛 —— 可观测性不能拖垮业务启动
- `_scrub` 回调：字段路径命中 authorization/cookie/token/pat/password/secret/
  session/access_key/api_key/key/api-key/apikey/x-api- → `[scrubbed]`
  （`key` 会连带命中 `mediaKey` 这类非敏感键，是刻意接受的过杀）
- 幂等（`_configured`）：重复 setup 不抛 `already instrumented`
- `console=False`：stdout 已有 logging，避免重复打印淹没真实日志
- `service_name` **从 `config.SERVICE_NAME` 读，绝不写死**（hewapi 写死
  `"newapi-bff"` 就是这个坑的来源）

### 探针 span 排除（`excluded_urls`，2026-09-16 加）
`instrument_fastapi(..., excluded_urls="/healthz,/readyz")` —— 容器探针每 30s
各打一次 `/healthz`（镜像 HEALTHCHECK）与 `/readyz`（compose healthcheck），
单副本一天约 5760 条恒定噪音 span，排除后这两条路径完全不产生 span。

#### ⚠️ 关于 `excluded_urls` 的两个反直觉点（差点写错）
1. **匹配目标是「完整 URL」而不是 path**：OTEL 传进来的是
   `get_host_port_url_tuple(scope)` 的第三项 = `scheme://host:port` + `scope["path"]`
   （见 `opentelemetry/instrumentation/asgi/__init__.py:477`）。原文还有句
   误导性注释 "using the scope path is enough"。
2. **判定用 `re.search` 而非 `re.match`**：`ExcludeList.url_disabled()` 是
   `bool(search(self._regex, url))`（见 `opentelemetry/util/http/__init__.py:82`）
   → **子串匹配，任意位置**。
   - 所以写 `/healthz` **能**命中 `http://127.0.0.1:8000/healthz` ✅
   - 但**绝不能写 `^/healthz`** —— 锚定行首后永远匹配不上完整 URL ❌
   - 副作用：`/healthz/deep` 这类子路径会一并被排除（对探针正是想要的）
   两者的组合很容易推出相反结论，改动前先跑下面的验证。

#### 验证手法（可复用，比读源码可靠）
自定义 `SpanProcessor` 收集 span 名，真发请求看哪些被捕获：
```python
from opentelemetry.sdk.trace import SpanProcessor
class Collector(SpanProcessor):
    def __init__(self): self.names = []
    def on_start(self, span, parent_context=None): self.names.append(span.name)
    def on_end(self, span): pass
    def shutdown(self): pass
    def force_flush(self, timeout_millis=None): return True

col = Collector()
logfire.configure(send_to_logfire=False, additional_span_processors=[col], console=False)
logfire.instrument_fastapi(app, capture_headers=False, excluded_urls="/healthz,/readyz")
# TestClient 依次 GET /healthz /readyz /api/config
# 期望结果（已实测）：只捕获到 "GET /api/config"
```
> 注意 `logfire.testing.TestExporter` **不能**直接塞进 `additional_span_processors`
> （它不是 SpanProcessor，会报 `AttributeError: no attribute 'on_start'`），
> 上面这个自定义收集器更省事。

## 三、容器化踩坑（`read_only: true` 相关）

### ⚠️ 最坑的一条：这两个路径**不跟随** `BFF_DATA_DIR`
`app/config.py` 里：
- `ADMIN_CRED_FILE` 默认 = `仓库根/data/admin_cred.json`（硬编码，非 DATA_DIR）
- `SIGNUP_STATE_FILE` 默认 = `仓库根/data/signup_bonus.json`（同上）

容器里就是 `/app/data/…`，而 `/app` 只读 → 写管理员 PAT 缓存或首次注册赠送时
**直接抛 Errno 30**。故 Dockerfile 与 compose **都必须显式覆盖**：
```
BFF_ADMIN_CRED_FILE=/data/admin_cred.json
BFF_SIGNUP_STATE_FILE=/data/signup_bonus.json
```

### 其余写盘点（审计结论：全部落在 /data，read_only 安全）
- `cloudstore.py:350` `makedirs(DATA_DIR)`
- `cloudstore.py:710/716` `DATA_DIR/media/<uid>/<xx>/`（本地 blob 回落）
- `newapi_client.py:131/133` `dirname(ADMIN_CRED_FILE)` → 靠上面的显式覆盖
- `store.py:22/39/41/53` `DATA_DIR` 及其父目录
- `user_keys.py:34/36` `DATA_DIR/user_keys.json`（硬编码跟随 DATA_DIR，**没有独立 env**）
- `store.py:53` 的 `makedirs(dirname(path) or ".")`：`.` 已存在时 `exist_ok=True`
  不会尝试创建，故不报错 —— 但前提是 path 都带目录（flovart 的调用点确实都带）

### 其余取值
- `/tmp` tmpfs 给 **64m**（hewapi 只给 16m）：FastAPI `UploadFile` 超过 1MB 会
  spool 到 `/tmp`，本项目的 PSD / 多图层上传容易越线。tmpfs **计入 memory 限额**。
- memory limit **1G**（hewapi 512M）：有 Pillow / psd-tools 图像转码路径，
  大图解码是内存峰值来源，512M 下大 PSD 易被 OOM Kill。
- 不装 curl：健康检查用 `python -c urllib.request`，避免构建依赖 apt 源。
- 只 `COPY app/`：**没有** static/（前端独立仓库）、**没有** docs/ 运行时依赖。
  ⚠️ 别照抄 hewapi 的 `COPY static/ ./static/` 与 `COPY docs/ ./docs/`。

## 四、文件清单（2026-09-16 新增/改动）

新增：
- `Dockerfile`（多阶段、非 root、read_only 兼容、单 worker）
- `docker-compose.yml`（全套改名隔离 + Logfire 独立项目说明）
- `.dockerignore`（`.env` / `data/` / 缓存 / 文档 / 测试 / 脚本）
- **`DEPLOY-CUTOVER.md`（宝塔裸跑 → Docker 的切换手册，10 节 + 隔离契约速查表）**

改动：
- `app/observability.py`：no-op 桩 → 真实现
- `app/config.py`：新增 `SERVICE_NAME`（读 `BFF_SERVICE_NAME`，默认 `flovart-bff`）
- `app/main.py`：`/healthz` 的服务名从硬编码改为 `config.SERVICE_NAME`
- `requirements.txt`：新增 `logfire[fastapi,httpx]==4.41.0`
- `.env.example`：补 Logfire 独立项目说明 + `BFF_SERVICE_NAME` + 容器部署段；
  修正过时注释（`GATEWAY_IMAGE_GEN_MODE` 早就默认 sync，文件里还写着 async）

## 五、验证记录（2026-09-16）
- `python -m compileall app` → 通过
- `pytest tests/` → **44 passed**
- 无 token：`setup()` 返回 `False`、`instrument_httpx()` 零副作用
- 假 token：`setup()` 返回 `True`，日志 `service=flovart-bff environment=flovart-prod
  fastapi_traces=on`；二次 setup 幂等；`instrument_httpx(object())` 只 warning 不抛
- `docker compose config` → 语法通过，`name/container_name/image/volume/published`
  全部为 flovart-bff 专属值
- **模拟容器环境**（只拷 `app/` 到干净目录启动）→ `/healthz` 200、
  `/readyz` 三项全 ok（secret_key/admin_cred/state_dir_writable）、`/` 200
  → **证实只拷 `app/` 就够**
- 服务名统一（10:30 补）：原先 4 处硬编码 `"flovart-bff"`，现只留
  `app/config.py:213` 一处作为**默认值定义**，其余全部读 `config.SERVICE_NAME`：
  `main.py` 的 `FastAPI(title=)`、`/`、`/healthz`，`routers/auth.py` 的 `/api/config`。
  实起 8399 验证四处返回 `flovart-bff` 一致；`pytest` 44 passed 无回归。
  > 安全性依据：前端 `services/hostedClient.ts:62 detectHosted()` 判定 hosted 模式
  > 用的是 **「`/api/config` 通不通」**（`.then(()=>true).catch(()=>false)`），
  > **不读返回里的 `service` 字段值** → 改取值来源对前端零影响（且默认值不变）。

## 六、CI 打镜像（GitHub Actions → GHCR · 2026-09-16 新增）

### 为什么之前 Actions 页面是空的
仓库**从来没有 `.github/workflows/` 目录** → 一个 workflow 都没有。
GH Actions 不是「提交就自动做事」，必须有个 YAML 定义步骤。飞哥看到的是
GitHub 的「从模板建一个」引导页，与提交内容无关。

### 新增 `.github/workflows/build-image.yml`
- 触发：push `main` / push tag `v*` / `workflow_dispatch`
- **job `test`**（先跑门禁）：`pip install -r requirements.txt pytest` → `pytest -q`
  - `tests/conftest.py` 自带 `BFF_SKIP_DOTENV=1` + 临时目录 → CI 里不需要 .env
  - pytest 不在 requirements.txt（那是运行依赖），单独装；httpx 已在其中
- **job `build`**（`needs: test`，测试不过不推镜像）：setup-buildx → login GHCR →
  metadata-action → build-push-action，`cache-from/to: type=gha`
- ⚠️ **`permissions: packages: write` 必须显式声明**：默认 GITHUB_TOKEN 只读，
  缺了会「build 成功、push 403 denied」，报错很不直观
- ⚠️ **镜像名必须全小写**（GHCR 硬要求），而 GitHub 仓库名保留大小写 →
  用 `repo_lc=${GITHUB_REPOSITORY,,}` 在 step 里转小写再喂给 metadata-action
- ⚠️ **CI 刻意用官方 pip 源**（不传 PIP_INDEX_URL）：runner 在境外，直连
  pypi.org 最快；国内服务器构建才需要镜像源 —— 两者诉求相反，别互相照抄
- `APP_VERSION` build arg：tag 触发用 tag 名，分支触发用 `sha-<7位>`（写进
  `BFF_VERSION` → `/healthz` 的 version，可回溯）
- **刻意不加 ruff**：`ruff check app/` 现有 68 项、`tests/` 61 项（其中 60 项是
  pytest 正常用法的 S101 `assert`，属误报，应配 per-file-ignores）。
  现在加 CI 会一直红，反而让人不看 CI 结果。待独立清理一轮后再作为门禁加入。

### compose 相应改动：镜像名可被 registry 覆盖
```yaml
image: ${FLOVART_BFF_IMAGE:-flovart-bff:${APP_VERSION}}
```
- 不设 `FLOVART_BFF_IMAGE` = 路线 A（服务器本地 build，名字不变）
- 设 `FLOVART_BFF_IMAGE=ghcr.io/<owner>/flovart-bff:<tag>` = 路线 B（只拉不 build）
- ⚠️ **本服务同时有 `build` 段**：若镜像本地不存在又没先 `pull`，compose 会回退
  去 build → 现象是「配了 GHCR 地址却在服务器上装了十分钟依赖」。
  正确顺序永远是先 `pull`，再 `up -d --no-build`

### 🔴 变量名从 `BFF_IMAGE` 改成 `FLOVART_BFF_IMAGE`（2026-09-16 真实事故）
**症状**（飞哥服务器实测）：
```
flovart-bff  Image ghcr.io/jushenzhidao/newapi-bff:sha-1d8fb3e Pulling
Image ... Error failed to resolve reference ...: not found
```
**根因**：`image: ${BFF_IMAGE:-...}` 里的变量名与 hewapi 的 `.env` **完全同名**。
两套配置项大面积相同（`BFF_SECRET_KEY`/`BFF_POINTS_PER_CNY`/`NEWAPI_*` 都一样），
「照抄 hewapi 的 .env」是很自然的动作 → 这一行被静默劫持成对方的镜像地址。
注意报错里 **sha 是对的、仓库名是错的**（`newapi-bff`）。
compose **不会**报任何配置错误 —— 语法完全合法，只是拉了个不存在的仓库。
- 改名后残留的 `BFF_IMAGE` 行**失效且刻意不兼容**（已验证：设了也不生效）
- 本项目已改名项：`BFF_IMAGE`→`FLOVART_BFF_IMAGE`、`BFF_PORT`→`FLOVART_BFF_PORT`
- **原则**：新增任何带固定名的资源/变量前，先问「hewapi 有没有同名的？」

### 服务器侧（路线 B）
1. **当前免认证**：实测 GHCR 包 `jushenzhidao/flovart-bff` **匿名可拉**
   （无凭证取 manifest 返回 200）。日后若改 private 才需要：
   `echo <PAT> | docker login ghcr.io -u <用户名> --password-stdin`
   （PAT 需 `read:packages`；用户名是账号名不是邮箱；凭证存 ~/.docker/config.json）
2. `.env`：`FLOVART_BFF_IMAGE=ghcr.io/<owner>/flovart-bff:<tag>`
   并让 `APP_VERSION` 与 tag 一致（路线 B 下它只参与 compose 解析，
   镜像里的版本是 CI 构建时烧死的）
3. **先验引用存在**：`docker manifest inspect <完整地址> >/dev/null && echo OK`
4. `docker compose pull && docker compose up -d --no-build`

### ✅ CI 已实测跑绿（2026-09-16）
push `main` 自动触发并成功，GHCR 上已有 4 个 tag：
`latest` / `main` / `sha-1d8fb3e` / `sha-6abf3fa`。
镜像内实测落地值：`BFF_VERSION=sha-1d8fb3e`、`BFF_SERVICE_NAME=flovart-bff`、
`BFF_DATA_DIR=/data`、`BFF_ADMIN_CRED_FILE=/data/admin_cred.json`、
`BFF_SIGNUP_STATE_FILE=/data/signup_bonus.json`；
OCI 标签 `org.opencontainers.image.revision=1d8fb3e6d1fd...` 与本地 HEAD 对得上；
平台 linux/amd64（+ buildx 的 unknown/unknown attestation，正常）。
→ **「Dockerfile 从未真机 build」这个缺口已关闭**。

### `.dockerignore` 补充
加了 `.github/`（CI 配置由 GitHub 直接读取，不经过 dockerignore，无需进上下文）。
注意 `.dockerignore` 里已有 `*.md`，所以 `DEPLOY-CUTOVER.md` 不会进镜像。

## 七、待办
- 🔴🔴 **【最高优先级，上线前必须解决】new-api 管理员账号与 hewapi 共用**（2026-09-16 实测）
  - flovart 与 hewapi **完全同一个账号**：uid=1 / 用户名 `newapi-bff` / 密码逐字相同 /
    **连 PAT 都是同一串**（sha256 比对确认，线上值 = `UqLFohUym+7JV+…`）
  - 那把 PAT **已失效**：3 个端点 + 4 种请求头写法全 401，响应体
    `{"code":"AUTH_UNAUTHORIZED","message":"Unauthorized, invalid access token"}`；
    对照 `/api/status`=200（网关可达）、伪造 PAT=401（排除端点不吃 PAT）
  - 推论：hewapi 线上跑在**运行时自愈出来的内存 PAT** 上（配了 PAT 就不落盘），
    每次重启都重登轮换 → **「互踢」不是推测，是正在发生**
  - 正解：给 flovart 单开管理员账号；`.env` 换新账号 UID/账密；`NEWAPI_ADMIN_PAT` 留空
  - ⚠️ **绝不能照抄那串失效 PAT**：配了它，首个管理员请求就 login → 当场踢死 hewapi，
    且新 PAT 只存内存 → 每次容器重启再踢一次。**比留空更危险**
  - ⚠️⚠️ **更正（2026-09-16 晚）：`NEWAPI_ADMIN_PAT` 留空也不是解法**
    —— 留空只把频次从「每次重启」降到「首次冷启一次」，只要两套都在线乒乓照旧。
    **唯一稳定解 = 独立管理员账号**，别指望靠调 PAT 字段绕开。
    另：compose 原注释「留空则每次冷启走账密登录」不准确 —— 实际是首次冷启登录一次
    并落盘 `/data/admin_cred.json`，之后复用（`_load_admin_cred` 先读盘）。
  - 🔑 **「PAT 每次调用都轮换」已从注释断言升级为实证**：上游路由
    `selfRoute.GET("/token", …, controller.GenerateAccessToken)` —— handler 名为
    **Generate**，且挂 `UserCriticalRateLimit("access-token")` + `DisableCache()`；
    观察闭环：账密登录实测 200（role=100）但 .env 那串 401，若只返回现值则不会失效。
    路由存在性对照：`/api/user/token`→401（已注册）、伪造路径→404。
  - 🔴 **「先验 PAT，200 就可并行」不充分**：`AccessToken` 是**用户记录上的单个字段**，
    两边不可能各持一把有效 PAT，同时工作只能是**握着完全相同的同一串** →
    200 仅代表「此刻不会立刻开踢」，任何一次轮换都会打破。
  - 📌 **概念分界（飞哥反复追问的点）**：**能登录 ≠ PAT 有效**。
    `_probe_two_channel.py` 实测：`POST /api/user/login`（账密）**200** ✅ 与
    `GET /api/user/self`（PAT）**401** ❌ **可以同时成立**。登录返回的 `access_token`
    带 `access_expires_at`，是**会话令牌**，与长期 PAT 是两回事。
    且正是「账密有效」让 401 后必然自愈成功 → 必然轮换 → 必然互踢；
    若账密也失效反而只是 503，不会互踢。
  - 影响面已核实（比想象中窄）：主路径 `/api/models` 走**用户自己的 PAT**
    （`keys.py:69 _ensure_token_plain` → `_ensure_token_user`），管理员 PAT 只是 401
    降级兜底（`admin_ensure_user_api_key`）→ 互踢主要打在**管理台 `/api/console/*`**
    （`console.py` 三处直接调 `admin_enabled_models()`）与冷启/降级路径
  - 查 hewapi 是否已在循环：
    `docker compose logs bff | grep -c "admin PAT rejected, re-login to rotate"`
- 🔴 **别直接把开发机 `.env` 拷到服务器**。除账号共用外还有 6 处：
  `BFF_SECRET_KEY` 需与现网一致（否则全员登出；**并更正：不存在「兑换码账号失联」这回事**，
  见下条）、`LOGFIRE_TOKEN` 是 hewapi 的、`LOGFIRE_ENVIRONMENT=local`、
  `APP_VERSION`/`VCS_REF` 过时、缺 `PIP_INDEX_URL`、
  **缺 `FORWARDED_ALLOW_IPS`（默认 127.0.0.1 容器化后是错的，静默劣化限流）**
  已产出服务器底稿 `.env.server`（被忽略，需手动上传）
  - `BFF_MOCK_MODE` 是**死配置**（全仓库无代码读取）；9 个 `GATEWAY_SYNC_*_PATH`
    不传也没问题（config.py 默认就是 `v1/images/generations`）
  - `BFF_COOKIE_SECURE=false` 看似危险实则无害：compose 硬编码 `"1"` 覆盖，
    且不用 `env_file` 所以根本进不去容器
- 🔴 **更正一处凭印象写错的技术结论（2026-09-16 晚）**：此前多处写「兑换码账号的用户名
  密码由 `BFF_SECRET_KEY` HMAC 派生，换 key 等于那批账号全部失联」——**纯属错误**。
  依据：`auth.py:112` 是 `admin_create_user(username, body.password)`，影子账号用
  **用户自己填的密码**；全仓 `grep -rn "hmac\|HMAC" app/` **零命中**；
  `ARCHITECTURE.md:195` 写明邀请/兑换码仍是 **M4 未实现规划**。
  **`BFF_SECRET_KEY` 唯一用途** = `security.py:44-49` HKDF(`bff-session-aead-v2`)
  → 会话 Cookie 的 AES-256-GCM 密钥。**换值后果仅「所有在线用户被登出，重新登录即可」**。
  已在 `.env.server` 与 `DEPLOY-CUTOVER.md`（2 处）更正。
  > 教训：错误注释比没有注释更危险 —— 它会把一个其实安全的操作写成禁地。
- 🔍 **两侧会话密钥当前是同一把**：flovart 与 hewapi 的 `BFF_SECRET_KEY` 逐字符相同
  （`e97052e9…`）。配合两边同名的 `COOKIE_NAME=bff_session`、同 HKDF info、
  同载荷 `{uid,username,pat,role}`、同一 new-api 实例（uid 语义一致）
  → **两套服务的会话可互相解开**。Cookie 未设 `domain=`（host-only），
  浏览器不会跨主机名自动携带 → **非紧急漏洞**，列为后续加固项，
  **别在切流当天换**（换了 = 全员登出）。
- ✅ **Logfire token 已实测有效并填入 `.env.server`**。判据做了对照实验：
  `force_flush()` 返回 True **不能**单独当判据（假 token 也返回 True）。
  真 token → 零告警；假 token → `UserWarning: … 401 Detail: Invalid token` +
  `LogfireServerWarning` + `ERROR otlp… 401`。
  格式差异辅助辨认：hewapi 的 v1 = **59** 字符，新项目 v2 = **92** 字符。
  `LOGFIRE_ENVIRONMENT` → `production`（本地那份保持 `pre`，同项目内区分本地/生产流量）。
  > ⚠️ 过程中自伤一次：凭前缀+长度**编造**了 token 后半段写进文件，靠长度校验（79≠92）
  > 才发现。**密钥值必须从源头程序化搬运，绝不凭已见片段续写。**
- `.gitignore` 已补 `.env.*` —— 之前只有 `.env`，导致 `.env.server` 这种带真实密钥的
  环境变体会被提交。`.dockerignore` 早就写对了
- **切换执行**：按仓库根 `DEPLOY-CUTOVER.md` 走。顺序要点：
  ① 验 PAT（`/api/user/self` 判 200/401）② 起容器 8310 ③ `/readyz`（不碰 new-api）
  ④ 停宝塔旧项目**并关自启**（只点停止会被拉起抢 8300）⑤ 改反代指向 8310
  ⚠️ PAT 已 401 → 必须把「停宝塔」提到「起容器」之前，否则两套 BFF 同账号互踢
  ⚠️ 但根治仍是①之前的**独立管理员账号**（见本文件开头「多业务隔离」）。

- ~~飞哥：去 Logfire 新建项目取新 token~~ **已完成**（`.env` 与 `.env.server` 均已填
  `pylf_v1_us_…` 92 字符新 token，`LOGFIRE_ENVIRONMENT=production`，实测可上报）。
  ⚠️ 本项目与 hewapi 是**两个 Logfire 项目、两把 token**，别再混用
- `.e2e/{cookies,hdr,user}.txt` **已在 `fcf5f93` 进仓库**（本地 127.0.0.1 的 e2e
  测试账号凭证，非线上真实用户）。`.gitignore` 已补 `.e2e/` / `dist/` / `*.bak*`，
  但已入库的三个文件需出仓：`git rm --cached .e2e/cookies.txt .e2e/hdr.txt .e2e/user.txt`
- ~~Dockerfile 从未真机 build~~ **已关闭**：CI 于 2026-09-16 构建成功并推 GHCR
- ~~workflow 手工 Run workflow 一次~~ **已关闭**：push `main` 自动触发即跑绿
- **ruff 清理（独立一轮）**：`app/` 68 项、`tests/` 61 项。清理顺序建议：
  ① `pyproject.toml` 加 `[tool.ruff.lint.per-file-ignores]` 豁免 `tests/*` 的 S101
  ② `ruff check app/ --fix` 能自动修 53 项
  ③ 剩下 15 项（S110 try-except-pass / E702 / E741 / S608 / ASYNC230 / F811）
     需人工判断
  ④ 全绿后再把 ruff 加进 CI 的 test job 作为门禁
