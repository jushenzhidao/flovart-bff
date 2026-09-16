# 部署切换手册：宝塔裸跑 → Docker 容器

> 目标：在**不影响线上用户**的前提下，把 flovart-bff 从「宝塔 Python 项目管理器
> 裸跑 uvicorn」切到「Docker 容器」，失败可一键回滚。
>
> 适用前提：单机、单副本、local SQLite + `data/media`（当前测试环境形态）。
>
> ⚠️ 全程与 hewapi-bff **同机并存**。凡带固定名的资源（容器名 / 镜像名 / 卷名 /
> 宿主端口）本手册已全部改名隔离，执行时**不要退回同名**，理由见
> `docker-compose.yml` 顶部注释。

---

## 0. 变量速填表

动手前先把这张表填满，后面所有命令都引用它。**留空的一格就是一次事故**。

| 变量 | 值 | 从哪来 |
|---|---|---|
| `<REPO>` | 服务器上仓库路径，如 `/www/wwwroot/flovart-bff` | `git clone` 的位置 |
| `<OLD_DIR>` | 旧 BFF 的项目目录（宝塔 Python 项目管理器里显示） | 宝塔面板 |
| `<NEWAPI_BASE_URL>` | 网关地址，如 `https://newapi.example.com`（**不带 `/v1`**） | 现网 `.env` |
| `BFF_SECRET_KEY` | 32+ 字符 | 现网 `.env`，**必须原值照抄** |
| `NEWAPI_ADMIN_UID` | 本业务专属管理员 uid | 现网 `.env` |
| `NEWAPI_ADMIN_USERNAME` / `PASSWORD` | 同上账号 | 现网 `.env` |
| `<DOCKER0_IP>` | 一般是 `172.17.0.1` | 第 1.5 步实测 |
| `APP_VERSION` | 如 `1.0.0` | 你定 |
| `<GHCR_IMAGE>` | `ghcr.io/<owner>/flovart-bff:sha-xxxxxxxx` | 走路线 B 才填，见第 3 节；变量名 `FLOVART_BFF_IMAGE` |

**最容易搞错的两项**，先说清楚：

1. **`BFF_SECRET_KEY` 必须与现网完全一致。** 它经 HKDF 派生出会话 Cookie 的
   AES-256-GCM 密钥。换了值的唯一后果 = **所有在线用户被登出，重新登录即可**，
   不丢账号也不丢数据（注册时 new-api 影子账号用的是用户自己填的密码，
   `app/routers/auth.py:112`；BFF 不存密码，全仓无任何 HMAC 派生）。
   > 📌 更正：本节此前写的「兑换码账号口令由它 HMAC 派生、更换即全部失联」**是错的**，
   > 已核实无代码依据；邀请/兑换码在 `ARCHITECTURE.md:195` 仍是 M4 未实现的规划。
   > 当时把它写进来是凭印象，属于典型的「错误注释比没有更危险」。
   切流时这条保证用户无感。
   ⚠️ 另外注意：本值目前与 hewapi **逐字符相同**，即两套服务的会话可互相解开
   （同名 Cookie + 同 HKDF info + 同载荷 + 同 new-api uid 语义）。Cookie 是
   host-only，浏览器不会跨主机名自动携带，故非紧急漏洞；作为**后续加固项**处理，
   别在切流当天换。
2. **`NEWAPI_ADMIN_UID` / 账号密码——现状是错的，见第 1.4 步。**
   实测确认本项目与 hewapi **共用同一个 new-api 管理员账号**（uid=1 / `newapi-bff`），
   而 PAT 是账号级的、每次重签作废旧值 → 两边会互踢。
   **上线前应给 flovart 单开一个管理员账号**，而不是照抄现网这份。

---

## 1. 前置检查（每一项都要过，不许跳）

### 1.1 Docker 与 compose

```bash
docker --version            # 需要 >= 20.10
docker compose version      # 需要 v2（`docker-compose` 也行，命令自行替换）
docker info >/dev/null && echo "daemon OK"
```

宝塔面板没装 Docker 的话：软件商店搜「Docker管理器」安装，或
`curl -fsSL https://get.docker.com | sh`。

### 1.2 仓库就位

```bash
cd <REPO>
git log --oneline -1        # 记下这个 commit，出问题要回溯
ls Dockerfile docker-compose.yml    # 两个都要在
```

### 1.3 `.env` 必填项自检

compose 里 7 个 `${VAR:?}` 是**硬校验**，缺一个就直接报错退出。先干跑一次：

```bash
cd <REPO>
docker compose config >/dev/null && echo "配置解析通过"
```

报 `xxx 必须设置` 就是缺项，按第 2 步补。

⚠️ **解析通过 ≠ 配置正确**。`BFF_SECRET_KEY` 最容易在这里翻车：它不报错，
只会在切流那一刻把所有在线用户踢下线（重新登录即可恢复，不丢数据）。
上传 `.env` 之前先比指纹，别肉眼比字符串（容易漏掉尾部空格）：

```bash
# 服务器上，现网宝塔那份：
grep '^BFF_SECRET_KEY=' <OLD_DIR>/.env | sha256sum
# 你准备上传的那份：
grep '^BFF_SECRET_KEY=' <REPO>/.env     | sha256sum
```

两个哈希一致才继续。不一致就以**现网那份**为准。

### 1.4 🔴 PAT 校验（**这一步决定后面能不能并行起容器**）

`app/newapi_client.py` 的 `_admin_login()` 会 `POST /api/user/login` 后紧接着
`GET /api/user/token` —— **重新生成 PAT 并作废旧值**。

推论：如果新旧两套 BFF 共用同一个 `NEWAPI_ADMIN_UID`，**任一边走到登录流程，
就会把对方的 PAT 踢失效**，表现为两边轮流 401、互相触发重登、逼近 new-api 的
50 会话上限，最终 409/503。

#### ⚠️ 关键更正：共用账号下**不存在稳定态**

一个账号的 `AccessToken` 是**用户记录上的单个字段**（不是「一账号多 token」）。
所以两边不可能各持一把有效 PAT —— 想同时工作，就只能是**两边握着完全相同的那一串**。

推论：**「先验 PAT，200 就并行」只能说明「此刻不会立刻开踢」，不能说明安全。**
任何一次轮换（任何一边的 401 自愈、管理员在面板点「系统访问令牌」、new-api 重启）
都会打破这个状态，然后两边互相触发重登、互相作废 —— **乒乓循环必然发生**。

因此：**PAT 校验只用来判断「现在起容器会不会当场踢」，不作为「可以共用账号」的依据。**
唯一稳定解是独立管理员账号（见本节末）。

顺带更正一条我先前给错的建议：**「把 `NEWAPI_ADMIN_PAT` 留空」并不能避开互踢。**
留空只是把频次从「每次重启一次」降到「首次冷启一次」；只要两套都在线，
乒乓照旧，只是节奏慢一点。

先测一把（判断是不是「立刻开踢」）：

```bash
source <REPO>/.env    # 或手动 export 下面三个
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $NEWAPI_ADMIN_PAT" \
  -H "New-Api-User $NEWAPI_ADMIN_UID" \
  "$NEWAPI_BASE_URL/api/user/self"
```

| 返回 | 含义 | 怎么做 |
|---|---|---|
| `200` | 此刻两边都不走登录 | 可以并行起容器（5.2 步可跳过）—— 但**共用账号仍是随时会爆的雷** |
| `401` | PAT 已失效 | **不要并行**。必须先停旧的，再起新的（第 6 步提到第 5 步之前） |
| `其它` | 网络/地址不对 | 先修 `NEWAPI_BASE_URL`，别往下走 |

> **对照项**：假 token 也会返回 401，公开端点 `/api/status` 返回 200 ——
> 用它排除「端点本来就不吃 PAT」的误判。三个端点建议都打一遍
> （`/api/channel/models_enabled`、`/api/user/?p=0&page_size=1`、`/api/user/self`）。
>
> **零成本自查轮换是否正在发生**（比打网关更有信息量，且无副作用）：
> ```bash
> docker compose logs bff | grep -c "admin PAT rejected, re-login to rotate"
> ```
> 非 0 就说明 hewapi 一直在自愈轮换 —— 那「填一把好的 PAT」这条路就不通。

#### 🔴 实测结论（2026-09-16）：本编排与 hewapi **共用同一个管理员账号**

逐项比对 flovart 与 `D:\code\hewapi-bff\newapi-bff\.env`：

| 配置项 | hewapi 线上 | flovart | 判定 |
|---|---|---|---|
| `NEWAPI_ADMIN_UID` | `1` | `1` | **同一账号** |
| `NEWAPI_ADMIN_USERNAME` | `newapi-bff` | `newapi-bff` | **完全相同** |
| `NEWAPI_ADMIN_PASSWORD` | `newapi-bffne…` | `newapi-bffne…` | **完全相同** |
| `NEWAPI_ADMIN_PAT` | `UqLFohUym+7JV+…` | `UqLFohUym+7JV+…` | **同一串**（sha256 比对确认） |

**并且那把 PAT 已经失效**（2026-09-16 实测）：

```
GET /api/channel/models_enabled   → 401
GET /api/user/?p=0&page_size=1    → 401
GET /api/user/self                → 401
响应体：{"code":"AUTH_UNAUTHORIZED","message":"Unauthorized, invalid access token"}
HTTP 头四种写法均 401：Bearer+New-Api-User / 仅 Bearer / 裸 token / X-Api-Key
对照：GET /api/status（公开）→ 200（网关可达）；伪造 PAT → 401
```

**这说明互踢已经发生了，不是推测。** hewapi 线上之所以还能跑，是因为它每次 401 后
会走 `_admin_login()` 自动换一把新的（且因配了 PAT 而不落盘，只存内存）——
**它现在跑在一把运行时自愈出来的 PAT 上**。而 flovart 一旦用同一个账号起容器，
就会和它轮流踢。

#### ✅ 上线前必须做：给 flovart 开独立管理员账号

1. 用 root 登录 new-api 面板 → 用户管理 → 新建用户（建议 `flovart-bff`，角色管理员）
2. 记下新账号 UID，`.env` 里 `NEWAPI_ADMIN_UID` / `USERNAME` / `PASSWORD` 换成它的
3. `NEWAPI_ADMIN_PAT` **留空** —— 首次启动登录一次自动签发并落盘
   `/data/admin_cred.json`，之后冷启复用，不再消耗会话

换账号**不影响已有用户数据**：用户是各自独立的 new-api 用户，管理员账号只是 BFF
建号/加额度的操作通道。但新账号得有对应权限。

> ⚠️ 若暂时不换账号：PAT 必须填**服务器现网那把有效的**（不是仓库里这份），
> 并接受「任何一边重登都会踢死另一边」。取现网那把的两种途径：
> `cat <现网数据目录>/admin_cred.json`，或 `grep '^NEWAPI_ADMIN_PAT=' <现网>.env`。

> ⚠️ 无论哪种方案，**都不要配一个已知失效的 PAT**。
> `_save_admin_cred()` 开头是 `if config.NEWAPI_ADMIN_PAT: return` —— 配了 PAT
> 就永不落盘自愈缓存。于是「配失效 PAT」的后果是：首个业务请求触发登录 →
> 踢死对方 → 新 PAT 只存内存 → **每次容器重启再踢一次**。
> 这比留空（留空至少还会先读 `/data/admin_cred.json` 缓存）更危险。

### 1.5 确认 docker0 网段（切流后限流是否误伤，全靠这个）

反代在宿主机、请求经 docker 网桥进容器时，容器看到的源 IP 是 **网桥网关 IP**，
不是 `127.0.0.1`。若 `FORWARDED_ALLOW_IPS` 保持默认 `127.0.0.1`，uvicorn 会**丢弃
`X-Forwarded-For`**，BFF 拿不到真实客户端 IP → 按 IP 限流会把所有用户当成同一个人。

```bash
ip -4 addr show docker0 | grep inet     # 例：inet 172.17.0.1/16
```

把这个 IP 记成 `<DOCKER0_IP>`，第 2 步写进 `.env`。

**绝不能填 `*`** —— 等于允许任意客户端自带伪造 XFF 绕过限流。

### 1.6 宝塔侧信息核对

- Python 项目管理器里旧项目的**启动命令**与**监听端口**（确认是 8300）
- 旧项目的 **data 目录绝对路径**（本手册记作 `<OLD_DIR>/data`）
  - 若不在这里，翻旧项目启动脚本里的 `BFF_DATA_DIR`
- PHP 站点的**反向代理目标**（确认是 `127.0.0.1:8300`）
- 旧项目**是否开了自启**（第 6 步必须关掉）

---

## 2. 准备 `.env`

`.env` 被 `.gitignore` 与 `.dockerignore` **双重忽略，不会随代码走**。
服务器上必须手动放一份。

> **现成底稿**：仓库根目录有一份 `.env.server`（同样被忽略，需手动上传），
> 已按 `docker-compose.yml` 的实际读取口径整理好，并逐项标注了「必须改」的地方。
> 比从 `.env.example` 起手快，也少漏项。**它里面含真实密钥，走 scp/面板上传，
> 不要提交。**
>
> ```bash
> scp .env.server root@<SERVER>:<REPO>/.env     # 上传后重命名为 .env
> ```
>
> ⚠️ **别直接拿开发机那份 `.env` 去部署** —— 它至少有三处会出事：PAT 已失效、
> Logfire token 是 hewapi 的、管理员账号与 hewapi 共用。

```bash
cd <REPO>
cp .env.example .env
vi .env
```

至少填这些（**容器不会读宿主 `.env` 里其它变量** —— compose 刻意不用
`env_file`，只注入显式列出的项）：

```dotenv
# ---- 必填 ----
APP_VERSION=1.0.0
VCS_REF=<git rev-parse --short HEAD 的输出>
BFF_SECRET_KEY=<现网原值，逐字符照抄>

NEWAPI_BASE_URL=<现网值>
# ⚠️ 下面三项应换成 flovart 专属管理员账号（见 1.4），不是照抄现网
NEWAPI_ADMIN_UID=<新账号 UID>
NEWAPI_ADMIN_USERNAME=<新账号用户名>
NEWAPI_ADMIN_PASSWORD=<新账号密码>
# ⚠️ 留空 = 首次启动登录一次自动签发并落盘。绝不能填已知失效的 PAT（见 1.4）
NEWAPI_ADMIN_PAT=

# ---- 本机适配 ----
FLOVART_BFF_PORT=8310          # 蓝绿用的临时端口，切流后才换成 8300
FORWARDED_ALLOW_IPS=<DOCKER0_IP>
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple   # 仅路线 A（本地构建）需要

# ---- 镜像来源（二选一，见第 3 节）----
# 删掉/注释掉下面这行 = 路线 A（服务器本地构建）
# ⚠️ 变量名是 FLOVART_BFF_IMAGE，不是 BFF_IMAGE（后者是 hewapi 的变量名，
#    照抄它的 .env 会劫持成它的镜像地址 → not found。详见第 3 节路线 B ③）
FLOVART_BFF_IMAGE=ghcr.io/<owner>/flovart-bff:sha-xxxxxxxx   # 路线 B（CI 打镜像）

# ---- 可观测性（可后补）----
LOGFIRE_TOKEN=
LOGFIRE_ENVIRONMENT=flovart-prod

# ---- 品牌（按现网 .env 对齐）----
BFF_BRAND_NAME=<现网值>
BFF_BRAND_ICP=<现网值>
BFF_BRAND_CONTACT=<现网值>
BFF_API_BASE_URL=<现网值>

# ---- 聊天补全 ----
BFF_CHAT_DEFAULT_MODEL=<现网值>
BFF_CHAT_VISION_MODEL=<现网值>

# ---- WaveSpeed ----
WAVESPEED_API_KEY=<现网值>

# ---- 媒体与元数据：按测试环境形态留空 = 容器内 /data ----
POSTGRES_DSN=
OSS_ENABLED=0
```

> **`PIP_INDEX_URL` 不是可选项。** 本项目有 fastapi / asyncpg / boto3 / Pillow /
> psd-tools / logfire 几十个依赖，直连 `pypi.org` 常见「下载极慢 → Read timed
> out → 整次构建白跑」。国内服务器务必填镜像源。

> **Logfire 可以先留空。** 配置全部是运行期注入（镜像里不带 `.env`），所以
> 事后拿到 token 只要改这一行 + `docker compose up -d --force-recreate`，
> **不需要重新打镜像**。留空时上报完全关闭，业务零影响。

---

## 3. 拿到镜像（两条路线，二选一）

**两条路线都可用。** CI 已实测推成功（见路线 B ①），所以走 B 更快 —— 服务器
不用装十分钟依赖。A 的价值只剩「本机没网/CI 挂了」时的兜底。

### 路线 A · 服务器本地构建

```bash
cd <REPO>
export APP_VERSION=1.0.0
export VCS_REF=$(git rev-parse --short HEAD)
export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

docker compose build
```

> ⚠️ **这是本编排第一次真机构建**（开发机上 Docker daemon 没起来过，只验到
> 配置层）。首次构建请预留调试时间，常见失败：
>
> | 报错 | 处理 |
> |---|---|
> | `Read timed out` / 卡在 pip | `PIP_INDEX_URL` 没填或镜像源不可达 |
> | `Bad Gateway` 拉不到 `python:3.13-slim` | 降级：`.env` 加 `PYTHON_IMAGE=python:3.12-slim`（代码 `requires-python >= 3.12`，官方支持）；或在 `daemon.json` 配 `registry-mirrors` 后 `systemctl restart docker` |
> | `no space left on device` | `docker system prune -f` 清旧镜像层 |

确认镜像出来了：

```bash
docker images flovart-bff
```

### 路线 B · CI 打镜像，服务器只拉（GHCR）

镜像由 `.github/workflows/build-image.yml` 在 push `main` 或打 `v*` tag 时
自动构建并推到 `ghcr.io/<owner>/flovart-bff`。

**① CI 已经跑绿了，先确认有哪些 tag 可用**

实测（2026-09-16）`ghcr.io/jushenzhidao/flovart-bff` 上已有：

```
latest  main  sha-1d8fb3e  sha-6abf3fa
```

> 所以「CI 能不能建出镜像」这个悬了很久的缺口**已经补上了** ——
> `Dockerfile` 真机构建通过，pytest 44 项通过，镜像可拉。
> 以后每次 push `main` 都会自动重跑；想手动重跑：
> GitHub 仓库 → **Actions** → **Build & Push Image** → **Run workflow**。

想确认某个 tag 真的存在（**上传 `.env` 前先跑这条**，1 秒，远好过起容器才 404）：

```bash
docker manifest inspect ghcr.io/<owner>/flovart-bff:<tag> >/dev/null && echo OK
```

**② 认证：当前不需要**

实测该 GHCR 包**匿名可拉**（无凭证直接取 manifest 返回 200），所以跳过 `docker login`。
若日后把包可见性改成 private，则要认证一次：

```bash
echo <GitHub_PAT> | docker login ghcr.io -u <GitHub用户名> --password-stdin
```

> PAT 需勾选 `read:packages`。用户名是 GitHub 账号名，**不是邮箱**。
> 凭证存在 `~/.docker/config.json`，之后不用重复登录。

**③ `.env` 指定镜像**

```dotenv
APP_VERSION=sha-1d8fb3e      # 与下面的 tag 保持一致
FLOVART_BFF_IMAGE=ghcr.io/<owner>/flovart-bff:sha-1d8fb3e
```

> ⚠️ **变量名是 `FLOVART_BFF_IMAGE`，不是 `BFF_IMAGE`。**
> hewapi 的 `.env` 里也有个 `BFF_IMAGE`（值是它的镜像地址）。两套配置项大面积
> 相同，「照抄 hewapi 的 .env」是很自然的动作 —— 而这行会**静默劫持**本服务，
> 报错长这样（2026-09-16 实际发生）：
>
> ```
> flovart-bff  Image ghcr.io/<owner>/newapi-bff:sha-1d8fb3e Pulling
> Image ghcr.io/<owner>/newapi-bff:sha-1d8fb3e Error
>   failed to resolve reference ... not found
> ```
>
> 注意**容器名是自己的、仓库名是对方的**。compose 不会报任何配置错误，
> 因为语法完全合法，只是拉了个不存在的仓库。变量已改名（刻意不与 hewapi 同名），
> 残留在 `.env` 里的 `BFF_IMAGE` 行现在会被忽略。

> 生产请钉 `sha-xxxxxxx`，别用 `latest` —— `latest` 随每次 push `main` 漂移，
> 一旦线上行为与预期不符，你无法判断跑的是哪一版。
> `APP_VERSION` 在路线 B 下只用于解析 compose（镜像里的版本是 CI 构建时烧死的），
> 所以让它与镜像 tag 一致，`.env` 才是自洽的。
> 反查镜像对应的 commit：
> `docker inspect flovart-bff --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'`

**④ 拉取**（`up` 在第 4 节）

```bash
cd <REPO>
docker compose pull
git rev-parse --short HEAD    # 记下来，切流后核对用
```

> ⚠️ **顺序不能反**。本服务同时声明了 `build` 段：若镜像在本地不存在又没先
> `pull`，compose 会回退去执行 build —— 表现为「配了 GHCR 地址，服务器却
> 吭哧吭哧装了十分钟依赖」。保险起见第 4 节可用 `docker compose up -d --no-build`。

### 两条路线怎么选

| | A 本地构建 | B CI 拉取 |
|---|---|---|
| 首次成本 | 低 | 要等 CI 跑绿 + 配一次 docker login |
| 发版速度 | 每次在服务器编译几分钟 | 秒级 pull |
| 服务器要求 | 能拉 Docker Hub + pypi | 只需能拉 GHCR |
| 可回溯性 | 取决于 `APP_VERSION` 填得准不准 | tag 自带 commit sha |

---

## 4. 起容器（**8310，先不碰生产端口**）

```bash
cd <REPO>
docker compose up -d
# 走路线 B（镜像来自 GHCR）时建议加 --no-build，杜绝 compose 回退去本地构建：
#   docker compose up -d --no-build
docker compose ps
```

期望 `State` 是 `running`，`Health` 在 ~40s 内变 `healthy`。

> 容器挂了会自动重启（`restart: unless-stopped`）。若反复重启，直接看日志
> （第 8 步）。

---

## 5. 验证（三项全过才允许往下）

### 5.1 `/readyz` —— 配置自检

```bash
curl -s http://127.0.0.1:8310/readyz | python3 -m json.tool
```

要通过必须三项全 `true`：

| 检查项 | 对应配置 | 不过怎么办 |
|---|---|---|
| `secret_key_configured` | `BFF_SECRET_KEY` 长度 >= 32 且非弱值 | 改 `.env` |
| `admin_cred_configured` | 有 `UID+PAT` 或 `账密` 任一组 | 补 `NEWAPI_ADMIN_*` |
| `state_dir_writable` | `/data` 可写 | 见 5.4 |

> `/readyz` **不碰 new-api**（只查本地配置），所以这一步**不会触发 PAT 互踢**，
> 可以放心在旧服务还在跑的时候执行。这是刻意设计。

顺手确认服务名与版本对得上：

```bash
curl -s http://127.0.0.1:8310/healthz
```

### 5.2 业务冒烟

用仓库里的脚本，**不要手搓 curl** —— 它会按「是否触碰管理员凭证通道」分层，
把会踢 hewapi 的用例默认挡掉：

```bash
cd <REPO>
chmod +x scripts/smoke_test.sh
BASE_URL=http://127.0.0.1:8310 ./scripts/smoke_test.sh
```

默认只跑 **L0**（配置体检 + 探针 + 站点配置 + 未登录鉴权闸门），**完全不接触
new-api**，可以在旧服务还开着的时候放心跑。全绿说明「容器起来了、配置对了、
鉴权没漏」。

```bash
# 加一层：用户通道（只影响该测试账号自己的 PAT）
./scripts/smoke_test.sh --user 某个普通用户:它的密码
```

> 脚本会拒绝用管理员账号登录 —— 那会重新签发它的 PAT 并当场踢掉 hewapi。

#### ⚠️ 为什么业务冒烟默认测不完整（这是设计，不是脚本偷懒）

这个 BFF 里**大量「用户级接口」在兜底时会走管理员凭证通道**。按 `app/routers/*`
实际调用逐个核过：

| 接口 | 走的通道 | 跑它的后果 |
|---|---|---|
| `/healthz` `/readyz` `/` `/api/config` | 纯本地 | 无 |
| `/api/user/login` `/logout` `/self` `/api/token` `/api/log/self` | 用户自己 | 仅该账号 PAT 被重签 |
| `/api/me/points` | **恒走 admin** `admin_get_user` | 🔴 触发轮换 |
| `/api/console/*`（全部） | **恒走 admin** | 🔴 触发轮换 |
| `/api/user/register` | **恒走 admin** `admin_create_user` | 🔴 触发轮换 |
| `/api/shares/*` | **恒走 admin** | 🔴 触发轮换 |
| `/api/models` | 用户 sk-，**失败回落 admin** | ⚠️ 正常时不碰，异常时碰 |
| `/api/chat/completions`、`/api/tasks`（生图/生视频） | 复用本地缓存 sk-，**缓存未命中走 admin 代建** | ⚠️ 新卷首次请求必碰 |

所以：**只要管理员账号仍与 hewapi 共用，「注册新用户」「看积分」「进管理台」
「新卷首次生图」这些动作都会踢 hewapi**。`admin_cred_configured` 在 `/readyz`
里只是「配置存在」的意思，不代表凭证真的可用 —— 别被那个绿勾骗了。

确认 `.env` 已换成独立管理员账号后，才加 `--admin-channel` 把 L2 跑完：

```bash
./scripts/smoke_test.sh --user 某普通用户:密码 --admin-channel
```

#### 站点配置单独看一眼

```bash
curl -s http://127.0.0.1:8310/api/config | python3 -m json.tool | head -20
```

重点核对 `brand.name`（应为 `oneArt`，若是「Workbuddy积分」说明 `.env` 抄错了
hewapi 那份）和 `version`（应与 `.env` 的 `APP_VERSION`、镜像 tag 三者一致）。

#### 想看真实页面

端口只绑回环，`http://<服务器IP>:8310` 打不通，走 SSH 隧道：

```bash
# 在你自己电脑上执行
ssh -L 8310:127.0.0.1:8310 root@<服务器IP>
# 然后本地浏览器开 http://127.0.0.1:8310
```

**真正完整的 UX 验证放在切流之后**（第 7 步），那时反代才通。

### 5.3 日志

```bash
docker compose logs -f --tail=200 bff
```

启动期应看到 `service=flovart-bff`，若配了 token 还有
`environment=flovart-prod fastapi_traces=on`。

> ⚠️ **应用刻意不写文件日志**：根文件系统 `read_only`，写容器内路径会抛
> `Errno 30`。所有日志走 stdout → docker json-file（10m × 5 轮转）。
> 所以排查**只有这一条路**，别去容器里找 `.log` 文件。
> 业务请求日志是另一回事，已落库：`GET /api/tasks`（`cloudstore.request_log`）。

### 5.4 `/data` 卷检查

```bash
docker compose exec bff ls -la /data
docker compose exec bff sh -c 'touch /data/.probe && rm /data/.probe && echo "可写"'
```

---

## 6. 数据迁移

**顺序很重要：先停旧进程，再拷数据。** 反过来会在拷贝途中被 SQLite 写入，
拷出一份不一致的库。

### 6.1 停旧 BFF（宝塔 Python 项目管理器）

面板 → 网站 / Python 项目管理器 → 找到旧项目：

1. 点 **停止**
2. **⚠️ 再关掉「开机自启」或直接删除项目**

> **只点「停止」是不够的。** 宝塔的守护会把它拉起来，抢回 8300 端口，
> 切流后表现为「一部分请求打旧服务、一部分打新服务」，而且两套 BFF 同时
> 在跑会让 PAT 互踢的概率陡增。
>
> 若第 1.4 步测出 PAT 已失效（401），**这一步必须提到第 4 步之前**。

验证真的停了：

```bash
ss -lntp | grep 8300
# 期望：无输出
pgrep -af uvicorn
# 期望：无 flovart-bff 相关进程
```

### 6.2 拷数据进卷

⚠️ **不要用 `docker cp`**：它保留宿主 uid（宝塔下通常是 root = `0:0`），
而容器以 `bff`（uid **10001**）运行，拷进去会权限拒绝。
也 **不要用 `docker compose exec -u root chown`** —— compose 里 `cap_drop: ALL`
连 root 的 `chown` 能力也一起丢了，会静默失败。

正解：另起一个一次性容器挂同一个卷来拷（不受 `cap_drop` 约束）：

```bash
docker compose stop bff

docker run --rm \
  -v flovart-bff-data:/data \
  -v "<OLD_DIR>/data:/src:ro" \
  python:3.13-slim \
  sh -c "cp -a /src/. /data/ && chown -R 10001:10001 /data && echo 迁移完成"

docker compose start bff
```

拷完核对：

```bash
docker compose exec bff ls -la /data
```

应能看到（按旧环境实际有的）：

| 文件/目录 | 丢了会怎样 |
|---|---|
| `admin_cred.json` | 管理员 PAT 缓存没了，下次冷启动走账密重登（可接受） |
| `user_keys.json` | **所有用户的 Key 密文丢失**，用户需重新填 |
| `signup_bonus.json` | 注册赠送账本重置 → **可重复领赠送**（真金白银） |
| `flovart_cloud.db` | **全部云端数据与媒体不可见** |
| `media/` | **用户媒体文件不可见** |

再打一次 `/readyz` 确认 `state_dir_writable` 仍为 `true`。

---

## 7. 切流

### 7.1 改反代目标

宝塔 → 网站 → 找到前端站点 → 设置 → 反向代理 → 把目标端口从 `8300` 改成 `8310`。

**如果手改 nginx 配置，注意这一条最容易踩的坑：**

```nginx
location /api/ {
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # ✅ 正确：末尾无 `/` → 保留原始 URI，BFF 收到 /api/config
    proxy_pass http://127.0.0.1:8310;

    # ❌ 错误：末尾带 `/` → nginx 把 /api/ 替换掉，BFF 收到 /config → 404
    # proxy_pass http://127.0.0.1:8310/;
}
```

> **为什么：** BFF 的路由**自带 `/api` 前缀**（`app/routers/*.py` 里是
> `@router.get("/api/config")` 这种写法），**不靠 Nginx 剥离前缀**。
> 所以反代必须原样透传，不做任何路径重写。
>
> 反代只需覆盖 `/api/`。`/healthz`、`/readyz` 不带前缀，从宿主机直接 curl 即可。

重载：

```bash
nginx -t && nginx -s reload
```

### 7.2 切流后验证

```bash
# 前端页面正常
curl -sI https://<你的域名>/ | head -3
# 经过反代的接口正常
curl -s https://<你的域名>/api/config | head -c 200
```

浏览器里走一遍真实路径：**打开站点 → 登录已有账号（验证 Cookie 未失效）→
打一张图 → 刷新确认历史记录还在**。

> 登录没被踹出来 = `BFF_SECRET_KEY` 抄对了。
> 历史记录还在 = 数据迁移成功了。

### 7.3 换回生产端口（可选，建议过几天再做）

稳定运行后，把 `.env` 的 `FLOVART_BFF_PORT` 改回 `8300`（或干脆保留 8310，
只要反代指对就行），然后：

```bash
cd <REPO>
docker compose up -d
```

此时旧进程早已停掉，不会抢占。

---

## 8. 回滚

**一句话：把反代目标改回 `127.0.0.1:8300`，启动宝塔旧项目。**

```bash
# 1. 反代改回 8300，重载 nginx
nginx -t && nginx -s reload

# 2. 宝塔面板启动旧项目（并恢复自启）

# 3. 停新容器（先别删，留着排查）
docker compose stop bff
```

旧目录 `<OLD_DIR>/data` **始终没被改动过**（迁移是单向拷贝），所以回滚后
数据完整。

> ⚠️ 若第 1.4 步测出 PAT 已失效，回滚时要留意：新容器如果在运行期间走过登录
> 流程重签过 PAT，旧的裸跑进程的 PAT 也已失效 → 它自己会走账密重登自愈
> （前提是配了账密）。这就是 `NEWAPI_ADMIN_USERNAME/PASSWORD` 必须配的原因。

---

## 9. 排查速查

| 症状 | 原因 | 处理 |
|---|---|---|
| `docker compose up` 报 `xxx 必须设置` | 缺 `${VAR:?}` 必填项 | 补 `.env`，再 `docker compose config` 验 |
| 容器反复重启 | `/readyz` 不过，或 `/data` 不可写 | `docker compose logs --tail=100 bff` |
| `Errno 30 Read-only file system` | 有代码往 `/app` 写 | 检查 `BFF_DATA_DIR` / `BFF_ADMIN_CRED_FILE` / `BFF_SIGNUP_STATE_FILE` 是否都指到 `/data` |
| `port is already allocated` | 8310 被占，或旧 BFF 没真停 | `ss -lntp \| grep 8310`；按 6.1 确认旧进程已停**且关了自启** |
| 接口 401 但用户说自己已登录 | `BFF_COOKIE_SECURE=1` 要求 HTTPS | 确认访问的是 `https://`；`http://` 下浏览器不发 Secure Cookie |
| 反代后接口全 404 | `proxy_pass` 末尾多写了 `/` | 按 7.1 去掉 |
| 首次冷启后两边轮流 401 | PAT 互踢（见 1.4） | 根治：给 flovart 换独立管理员账号。⚠️ 别用「把 PAT 留空」当解法——共用账号下留空 = 冷启必登录 = 当场踢死 hewapi |
| 限流把所有用户当同一人 | `FORWARDED_ALLOW_IPS` 没改成 `<DOCKER0_IP>` | 见 1.5 与 2 |
| 上传大 PSD 失败 | tmpfs `/tmp` 太小 | compose 里调大 `tmpfs: /tmp:size=` 与 `memory` |
| 容器被 OOM Kill | 大图解码内存峰值 | compose 里调大 `deploy.resources.limits.memory`（现 1G） |
| 看不到任何日志 | 应用不写文件日志，只能看 stdout | `docker compose logs -f bff` |
| Actions 页面只有模板引导页 | workflow 文件不在默认分支上 | 确认 `.github/workflows/build-image.yml` 已推到 `main` |
| CI 能构建但 push 报 403 denied | `GITHUB_TOKEN` 默认只读 | 确认 workflow 顶部有 `permissions: packages: write` |
| `pull access denied for ghcr.io/...` | 服务器没 `docker login ghcr.io`，或 PAT 缺 `read:packages` | 重新登录，PAT 勾上 `read:packages` |
| 配了 `FLOVART_BFF_IMAGE` 却在服务器上装依赖 | 没先 `pull`，compose 回退去 build 了 | `docker compose pull` 后再 `up -d --no-build` |
| pull 报 `not found`，**且仓库名是对方项目**（如 `.../newapi-bff:sha-xxx`） | 照抄 hewapi 的 `.env` 时被同名的 `BFF_IMAGE` 劫持了 | 改用 `FLOVART_BFF_IMAGE`，并核对仓库名是 `flovart-bff`；先 `docker manifest inspect` 验存在 |
| pull 报 `not found`，仓库名正确 | tag 写错或该次 CI 没推成功 | `docker manifest inspect ghcr.io/<owner>/flovart-bff:<tag>` 逐个试；Actions 里看构建记录 |
| 服务器拉到的镜像不是最新 | `latest` 在 CI 侧没更新，或本地有旧层 | 改用具体 sha tag；`docker compose pull` 时看输出确认拉到新 digest |
| `/api/config` 里 `brand.name` 是「Workbuddy积分」 | `.env` 抄了 hewapi 那份 | 改 `BFF_BRAND_NAME=oneArt`，`up -d --force-recreate` |
| `/api/config` 里 `version` 与镜像 tag 不一致 | `.env` 的 `APP_VERSION` 没跟着 tag 刷新 | 两个值对齐（走路线 B 时它们只用于解析 compose） |
| **未登录就能拿到业务数据**（`smoke_test.sh` 第 3 节的闸门项返回 200） | 鉴权依赖被漏挂 | 🔴 严重，先别切流。检查对应路由的 `Depends(require_session)` |
| 业务接口一调，hewapi 那边就开始报错 | 管理员凭证通道被触发（见 5.2 的通道表） | 只有换独立管理员账号能根治 |
| `smoke_test.sh` 的 L2 全红 | 账号仍是共用的、或 `.env` 里 `NEWAPI_ADMIN_*` 没换 | 先做 1.4 的独立账号 |

---

## 10. 收尾与遗留

### 切流完成后

- [ ] `.env` 备份到密码管理器（**丢了就全员登出 + 数据卷找不到**）
- [ ] **切流后再跑一次完整冒烟**（这次带上管理通道）：
      `./scripts/smoke_test.sh --url http://127.0.0.1:8310 --user 普通用户:密码 --admin-channel`
      L2 全绿才说明注册/管理台/积分这些路径真的通
- [ ] 记录本次 `APP_VERSION` 与 `VCS_REF`（`docker inspect flovart-bff`）
- [ ] 确认宝塔旧项目自启已关，或项目已删除
- [ ] 拿到 Logfire token 后补进 `.env` 并重建容器：
      `docker compose up -d --force-recreate`
      控制台应出现 `service=flovart-bff` / `environment=flovart-prod`，
      **与 hewapi 的 `newapi-bff` 完全分开**
- [ ] 观察一周后清理旧目录与旧 venv
- [ ] **仅路线 B**：镜像引用已验证存在（`docker manifest inspect` 通过，第 3 节①）
      ⚠️ `docker login` 用的 PAT 若设了有效期，到期后 `docker compose pull` 会报
      denied。运行中的容器不受影响，但**下次发版会拉不动镜像**。建议这个 PAT
      设成不过期，或记下到期日

### 已知待办（与本次切换无关）

- **video-gen 异步分支 body 形状**：现在是 `{"type": kind, "params": params}`，
  网关会报 `Model name not specified`（需要顶层 `model`）。图片已改 `sync` 绕开，
  视频异步待修。
- **多副本不可用**：注册赠送幂等靠进程内 `asyncio.Lock` + `signup_bonus.json`，
  故强制单 worker。要扩容须先迁 PG。

---

## 附：隔离契约速查

两套 BFF 同机，改配置时对照此表，**不要退回同名**：

| 资源 | hewapi 线上 | 本项目 |
|---|---|---|
| compose project | （目录名推导） | `flovart-bff` |
| `container_name` | `newapi-bff` | `flovart-bff` |
| image | `newapi-bff:${VER}` | `flovart-bff:${VER}` |
| **image 的变量名** | `BFF_IMAGE` | `FLOVART_BFF_IMAGE` |
| volume | `bff-data` | `flovart-bff-data` |
| 宿主端口 | `${BFF_PORT:-8000}` | `${FLOVART_BFF_PORT:-8300}` |
| Logfire service | `newapi-bff`（硬编码） | `flovart-bff` |
| Logfire environment | `local` | `flovart-prod` |

> hewapi **不用 PG / OSS**（纯 JSON + `/data` 卷），所以数据面天然不撞。
> 本项目将来启用 PG / OSS 时，若复用同一实例，务必换 `POSTGRES_DB` 与 `OSS_PREFIX`。
