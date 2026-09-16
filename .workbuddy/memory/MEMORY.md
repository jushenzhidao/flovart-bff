# flovart-bff 长期记忆

> **细节卷**（血案全过程 / 完整表格 / 代码片段）在 `.workbuddy/memory/topics/`：
> `gateway.md`（/v1 凭证 · 路径前缀 · 端点选择 · 渠道分组）· `tasks.md`（图片/视频任务）·
> `product-model.md`（产品模型解析 · 条目拆分）· `frontend-storage.md`（keyVault · 多账号隔离）·
> `platform-services.md`（平台共享服务 · 平台 Key 注入 · UI 语义）· `cloudstore.md`（云同步/素材）·
> `pitfalls.md`（其他坑 · 接口速查）· `packaging.md`（打包交付口径）·
> `deployment.md`（部署 / 容器化 / 与 hewapi 的同机隔离 · read_only 踩坑）。
> 每日流水见同目录 `YYYY-MM-DD.md`。原完整版备份：`MEMORY.md.bak20260916`。

## 定位 / 操作约定
Flovart「在线创作站」FastAPI BFF：登录 / 持久化 / new-api 代理。前端独立仓库 `D:\code\flovart-web`（同机并存，直接改）。
**约定**：不执行任何 git commit/push（飞哥自己来，只在末尾提醒）。
⚠️ 两个「打包」别混：**交付给飞哥的产物 = zip 压缩包**（他明确说过「我们之前不是 python 项目吗，压缩包就行」，别主动上 Docker 镜像）；**生产部署形态 = Docker compose**（2026-09-16 起补齐 Dockerfile + docker-compose.yml，见 `topics/deployment.md`）。

## 🔭 日志 / 可观测性（2026-09-16 **已接入 Logfire**）
**仍然没有文件日志，这是刻意的**：生产容器根文件系统 `read_only`，写容器内路径会抛 `OSError: [Errno 30]`。日志走两条腿 —— **stdout（docker json-file，10m×5 轮转）+ Logfire 上报**。
- `app/observability.py` **已从 no-op 桩换成真实现**（2026-09-16）。降级契约：未配 token → 不 import SDK、`setup()` 返回 `False`、零副作用；任何一步失败只 `warning` 不抛。
- ⚠️ **必须给本项目单开一个 Logfire 项目 + 独立 token**：与 hewapi 同机，两边曾共用**同一个 token**、environment 都叫 `local`，而 hewapi 把 `service_name` 写死成 `newapi-bff` → 两套 trace 在控制台混成一坨，按服务过滤不出边界。本项目 `service_name` 从 `config.SERVICE_NAME` 读（`BFF_SERVICE_NAME`，默认 `flovart-bff`），**代码里绝不写死**。
- 查看运行日志：`docker compose logs -f --tail=200 bff`（服务跑不动时只能看 stdout）
- 业务请求日志**是另一回事、已落库**：`GET /api/tasks`、`GET /api/tasks/{id}`（`cloudstore.request_log`）
- ⚠️ 报错 `services.bff.environment.NEWAPI_BASE_URL must be set` **出自 hewapi 的 `docker-compose.yml:66`**（service 名 `bff` / `container_name: newapi-bff` / `ports 127.0.0.1:${BFF_PORT:-8000}:8000`），**不是 flovart-bff 的文件**。飞哥 `.env` 里那段是粘贴的**面板报错备注**，别当成配置内容
- 完整部署隔离清单（容器名/镜像名/卷名/端口/SERVICE_NAME 逐项对照）+ 容器化踩坑见 `topics/deployment.md`
- ▶️ **部署后冒烟用 `scripts/smoke_test.sh`**（默认只跑不碰 new-api 的 L0 层：探针 + `/api/config` + 未登录鉴权闸门）。⚠️ 本 BFF 的**用户级接口大量兜底走管理员通道**（注册/积分/分享/新卷首次生图都会），所以验业务前先看 `topics/deployment.md` 的「凭证通道表」
- 🔴 **跨项目绝不复用环境变量名**（2026-09-16 真实事故）：镜像变量原名 `BFF_IMAGE`，与 hewapi 的 `.env` **同名**。照抄 hewapi 配置时被填成 `ghcr.io/<owner>/newapi-bff:sha-xxx` → `docker compose` 报 `failed to resolve reference ... not found`（**容器名是自己的、仓库名是对方的**，compose 不报配置错误，因为语法合法）。已改名 **`FLOVART_BFF_IMAGE`**（刻意不兼容残留的 `BFF_IMAGE`）。同类已改名项：`FLOVART_BFF_PORT`（原 `BFF_PORT`）。**新增任何带固定名的资源前先问：hewapi 有没有同名变量？**

## 架构铁律
- 登录=BFF 独立注册；注册=管理员影子建 new-api 号+赠送+自动登录（口令只换 PAT，不存密码）
- 会话=AES-256-GCM 加密 Cookie（`app/security.py`，服务端零存储，勿改回仅签名）
- 云端=PostgreSQL(asyncpg)+OSS/COS/S3；本机兜底 `USE_PG=False`→LocalMeta(SQLite)+LocalBlob。**cloudstore 改动须同时实现 Pg/Local 两套**
- new-api 双头 `Authorization:Bearer <PAT>` + `New-Api-User:<uid>`；登录即 DELETE sessions/{sid} 归还（50 上限硬拒绝）
- **多业务隔离**：每业务必须用**独立** new-api 管理员账号(uid)，严禁共用（PAT 账号级、每次重签作废旧值 → 互踢 401 雪崩）
- ⚠️ **同一业务多实例并行（蓝绿/灰度切换）同样会互踢**：旧进程（如宝塔 Python 项目管理器跑在 8300）+ 新容器（如 8310）**共用同一个 `NEWAPI_ADMIN_UID`** 时，任一边触发 `_admin_login()`（`newapi_client.py:141`，内部 `GET /api/user/token` **重新生成 PAT 并作废旧值**）就会作废对方 → 对方 401 → 也去重登 → 循环。
  缓解：`.env` 配**有效**的 `NEWAPI_ADMIN_PAT`，两边启动时 `_load_admin_cred()` 都直接用它、都不走登录 → 相安无事；**一旦该 PAT 失效即入循环**（日志刷 `admin PAT rejected, re-login to rotate`、高频登录逼近 new-api 50 会话上限 → 409 → BFF 返 503）。
  **根治：切换窗口内先停旧进程，或给新实例配独立管理员账号。** 
- ⚠️ `.env` 的 `NEWAPI_ADMIN_PAT` 是易失效一次性快照（点控制台「系统访问令牌」即作废，预期行为）→ **不要让飞哥维护 PAT**；PAT 401 自动回落账密 `_admin_login` 重签。失效只需改 `NEWAPI_ADMIN_PASSWORD`（同账号）
- 出口 quota→points（`config.quota_to_points*`），裸 quota 不外泄；单 worker
- BFF venv：`C:\Users\81068\.workbuddy\binaries\python\envs\flovart-bff`；Windows 下 `pip install -r` 用 `D:\` 路径（Git Bash `/d/` 会被误解析）

## ⭐⭐⭐ 三条高频踩坑速查（血案见 topics/gateway.md）
1. **`/v1` 只认用户 `sk-`，绝不能拿管理员 PAT 打** → 打 `/v1/*` 一律 401 Invalid token。统一走 `tasks._gw_call`（按需 mint 用户 sk- + 401 自动轮换）；`tasks.py` 里不允许再出现 `request_as_user`
2. **网关路径必须自带 `v1/` 前缀**（`base_url` 不带 `/v1`）；异步提交是 `POST /v1/video/generations`，**不是** `contents/generations/tasks`。返回 `text/html` = 路径不存在；`application/json` = 端点存在
3. **`image-gen` 必须 `mode=sync`**（`GATEWAY_IMAGE_GEN_MODE` 默认 sync，勿改回）：本网关**图片模型只支持 `v1/images/generations`**，走 `v1/video/generations` 是视频任务语义 → 图片必 404 `fail_to_fetch_task`（= 提交阶段上游非 200）。**9 个 `GATEWAY_SYNC_*_PATH` 全部 = `v1/images/generations`**，能力靠入参（`image[]`/`mask`/`variant`）区分，上游**不实现** `/images/edits` 等语义化端点

## ⭐⭐⭐ 分支策略（务必先读）
**`main` = 以「公共平台服务」为主的线上版本**；**`dev` = 接入网关模型新需求的开发区**
- `main` 上「平台共享服务」= **旧语义（共享 Key）**：条目含 `key`+`baseUrl`，用户侧持管理员 key 直连，**不经 new-api 计费**。`_gateway` **必须带**
- `dev` 上 = **新语义（模型清单）**：条目**仅含模型清单**（无 key/baseUrl），用户用**自己的默认 Key** + BFF 下发 `gatewayBaseUrl` 走 new-api，**计费落用户自己配额**。`_gateway` **绝不能带**；BFF `_FORBIDDEN_FIELDS=("key","baseUrl")` 强制剔除
- ⚠️ **基线 tag 不可信**：init commit（web `5ad25eb`/bff `6538bc7`）**已含** dev 改造，`dev-baseline-20260914` 不是改造前快照。main 回退 = web `f08ff71` / bff `8acc81d`
- ⚠️ **飞哥要求发布到 main 给普通用户用**：新增模型必须在 main 语义下也能走通（平台共享服务 + 平台 Key 池）

## 代码地图
- 后端：`app/routers/{auth,keys,usage,console,billing,promo,chat,shares,platform_services,tasks}.py`；`app/user_keys.py`；`app/tasks.py`+`app/thirdparty/wavespeed.py`；`app/{db,oss,security,store,config,newapi_client,cloudstore}.py`
- 前端：`FlovartAgentPanel.tsx` / `services/{browserAgentKernel,imageTask,aiGateway,hostedClient,historyCloudSync,cloudSync,routeMapping,productModelCatalog,promptBarPolicy,providerGenerationAdapter,workflowGeneration,workflowPromptPolicy}.ts` / `stores/useHostedStore.ts` / `hooks/useApiKeys.ts` / `utils/{storageNamespace,keyVault,modelRefs,platformModelGate}.ts` / `components/{SettingsPanel,PromptBar,ConfigManager/*,workflow/*}` / `tools/flovart/product-models.js`
