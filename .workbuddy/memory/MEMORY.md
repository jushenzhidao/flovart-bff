# flovart-bff 长期记忆

## 定位
Flovart「在线创作站」FastAPI BFF：登录/持久化/new-api 代理。前端独立仓库 `D:\code\flovart-web`（同机并存，直接改）。

## 架构铁律
- 登录=BFF 独立注册；注册=管理员影子建 new-api 号+赠送+自动登录（口令只换 PAT，不存密码）
- 会话=AES-256-GCM 加密 Cookie（`app/security.py`，服务端零存储，勿改回仅签名）
- 云端：PostgreSQL(asyncpg)+OSS/COS/S3；本机兜底 `USE_PG=False`→LocalMeta(SQLite)+LocalBlob(本地文件)。**cloudstore 改动须同时实现 Pg/Local 两套**
- new-api 双头 `Authorization:Bearer <PAT>` + `New-Api-User:<uid>`；登录即 DELETE sessions/{sid} 归还（50 上限硬拒绝）
- **多业务隔离**：每业务必须用**独立** new-api 管理员账号(uid)，严禁共用（PAT 账号级、每次重签作废旧值 → 互踢 401 雪崩）
- 出口 quota→points（`config.quota_to_points*`），裸 quota 不外泄；单 worker
- BFF venv：`C:\Users\81068\.workbuddy\binaries\python\envs\flovart-bff`；Windows 下 `pip install -r` 用 `D:\` 路径（Git Bash `/d/` 会被误解析）

## 聊天免 Key
- `/api/chat/completions` 走 new-api `/v1`（**只认 sk-，不认 PAT**）；每用户按需 mint+持久化归属配额的 sk-（`app/user_keys.py`，AES-GCM 落盘 0o600）
- 模型须配 `BFF_CHAT_DEFAULT_MODEL` / `BFF_CHAT_VISION_MODEL`，否则聊天 400

## 图片/视频任务（`app/tasks.py` 同步/异步双模式）
- `TASK_TYPES` 每项含 `mode` + `async_path`/`sync_path` + `provider`(gateway/thirdparty)
- `async`(image-gen/video-gen) 透传网关提交/轮询/取消；`sync` 阻塞直出后落盘
- `submit(uid,kind,params)`：带 `_gateway`→BFF 直连用户指定外部网关（用户 BYOK）；否则 `na.request_as_user` 走 new-api 平台（计费落用户配额，**绝不误用 admin_request**）
- ⭐ **外部网关调用已改非阻塞（2026-09-14）**：`_run_external` 落请求日志后 `asyncio.create_task`（强引用集 `_BACKGROUND_TASKS`，asyncio 只持弱引用）立即返回 `processing`，后台 `_external_call_and_record` 收敛 succeeded/failed。**根因**：原阻塞到底 + nginx 默认 `proxy_read_timeout 60s` < 生图 60~62s → 上游已出图但响应被掐 → 前端裸 `Network Error`。⇒ 部署务必把 nginx 读超时调到 **600s**（`data/release/nginx.conf` 已改）
- 前端轮询键用返回 `id`(=request_id)，非旧 task_id；`processing` 是中间态非终态，轮询继续
- 产物统一落 BFF 对象存储（元数据入 PG `cloud_media`），result 改写为 `/api/me/media/{key}` + `_bffMediaKey`
- 图片归一化 `_normalize_sync_result`：OpenAI `{data}`→`{images}`；前端 `extractImageOutputs` 扫 `image/images/layers/data`
- ⭐ **图片参考图入参契约（curl 实测，勿翻转）**：网关 `image` **必须是 data URL 字符串数组** `["data:image/png;base64,..."]`。对象 `{data,mimeType}` → 422；`[{data,mimeType}]` → 422
- ⭐ **图生图 = 文生图同一端点**（2026-09-11 飞哥拍板）：`POST {baseUrl}/images/generations`（JSON）+ 多一个 `image[]`。实测 `/images/edits`(multipart) → **404 网关未实现**。**勿再让图生图走 `/images/edits`**（除非官方 OpenAI 原生编辑模型，作为兜底）
- 契约文档：`docs/gateway-endpoint-contracts.md`、`docs/gateway-mapping-design.md`、`IMAGE-ASYNC-TASKS-CONTRACT.md`

## 素材共享（点对点）
- `admin_resolve_uid_by_username`；`cloud_media_shares`(owner+target+media_key+perm)；字节不复制，`/api/shared/media/{key}` 代理校验
- 共享本地素材前先 `ensureCloudKey` 上传 BFF 拿 cloudMediaKey

## 工作流素材云端持久化
- 项目元数据走 cloudSync→`cloud_docs`；**素材字节**经 `media.ts` 的 `ensureProjectMediaUploaded` 上传 `/api/me/media`，cloudMediaKey 回写节点 metadata（节点/封面/分层各一份）
- 读取兜底：`loadWorkflowMediaBlob` 本地 miss → `restoreWorkflowMediaFromCloud`
- 索引 `registerWorkflowCloudMediaIndex`（storageKey→cloudMediaKey），在 `setWorkflowMediaCanonicalProjects`/`pullOnce`/projects 订阅时重建
- **铁律：上传失败只 warn 不抛（下次 push 重试）；回写 metadata 不改 `updatedAt`，否则 schedulePush 死循环**

## 🎯 多账号本地存储隔离（已闭环，勿推翻）
`utils/storageNamespace.ts` 的 `ns()` 给 localforage 实例名追加 `__u<uid>`；14 个存储模块在**模块顶层**调用；登录/登出**整页 reload** 重建实例。

**真根因 = ES module import 求值顺序**：`index.tsx` 的 `import { RouterHost }` 会传递求值整条 storage 链，若 `primeStorageNamespace()` 写在其后 → **晚了**。
- **修法（两条必须同时满足）**：① `storageNamespace.ts` **文件末尾裸调用** `primeStorageNamespace();`；② `index.tsx` 首行 `import './utils/storageNamespace';`（副作用 import，置于所有业务 import 之前）
- **`bff_uid` 明文 uid 镜像 Cookie**：`bff_session` 是 httponly，JS 首屏读不到 uid。BFF `security.set_session` 额外种 `httponly=False` 的 `bff_uid`（`config.UID_COOKIE_NAME`），`clear_session` 同属性删除。**它不是凭据**，只用于挑 IndexedDB 库名后缀
- **HashRouter 铁律**：凡依赖整页 reload 重建模块顶层单例者，**禁用 `location.assign(pathname#x)`** → 须 `window.location.hash = x; window.location.reload();`
- reload 防循环：`clearStorageReloadMarker` / `consumeStorageReloadMarker`(读后即焚) / `markStorageReload`
- ⛔ **运行时动态实例方案已回退，勿再引入**（曾致素材读不到/节点丢失/历史消失/竞态）
- **回归测试** `tests/storageNamespaceIsolation.test.ts`（17 例，改存储隔离前必跑）：含**顺序保证静态断言**、`apiKeysLoaded` 健壮性契约

## ⭐⭐⭐ 分支策略（2026-09-14 起，务必先读）

**`main` = 以「公共平台服务」为主的线上版本**；**`dev` = 接入网关模型新需求的开发区**。
- `main` 上「平台共享服务」= **旧语义（共享 Key）**：条目含 `key`+`baseUrl`，用户侧持管理员 key
  直连管理员配的端点，**不经 new-api 计费**（平台自担成本）。`_gateway` **必须带**。
- `dev` 上「平台共享服务」= **新语义（模型清单）**：条目**仅含模型清单**（无 key/baseUrl），
  用户用**自己的默认 Key** + BFF 下发的 `gatewayBaseUrl` 走 new-api，**计费落用户自己配额**。
  `_gateway` **绝不能带**；BFF `_FORBIDDEN_FIELDS=("key","baseUrl")` 强制剔除。
- ⚠️ **动手改造前先打真·基线 tag**：本仓 init commit（web `5ad25eb`/bff `6538bc7`）
  **已包含** dev 改造，tag `dev-baseline-20260914` **不是**改造前快照。回退只能手工反向改写。
- 相关提交：main 回退 = web `f08ff71` / bff `8acc81d`。

## ⭐⭐ 平台共享 AI 服务（管理员发布 → 服务端存储 → 全员拉取）

### 🔑 两个标记、三条链路 —— 本仓最容易踩的坑
| | 平台 Key 池 | 平台共享服务（**main=旧语义**） | 平台共享服务（**dev=新语义**） |
|---|---|---|---|
| 标记 | `extraConfig.flovart_platform='1'` | `extraConfig.platformSource='1'` | 同左（标记不变，语义变） |
| 来源 | `/api/me/ensure-key` 按用户签发 | `/api/platform/services` 管理员发布 | 同左 |
| 密钥 | 每用户一把自己的 new-api token | 管理员那把 key **下发给所有人** | **不含密钥**，用用户自己的 sk- |
| 链路 | BFF 网关代发 → new-api **计费落各自配额** | 用户侧 `params._gateway` 交 BFF `_run_external` 直连外部网关 | 用户自己的 sk- 打 BFF `gatewayBaseUrl` → new-api |
| 计费 | new-api | **不经 new-api**（平台自担成本） | new-api，**落用户自己配额** |

两者**并存不互斥**。**严禁复用同一标记**：共享服务若带上 `flovart_platform`，前端 `isHostedPlatform()` 会误判 → 绕开计费。BFF `_sanitize()` **强制剔除**该字段并有单测守护。
**两者都必须被 `isHostedPlatform()` 认作平台模式**（否则掉 BYOK 直连 → 拿本地 key 直连上游），区别只在「要不要带 `_gateway`」：共享服务带、Key 池不带。注入函数 `platformSharedGatewayParams()`。
⚠️ **新增任何「服务自带 baseUrl+key」的链路，都要同步检查图片+视频两条路径是否都注入了 `_gateway`**（视频早有、图片曾漏）。

### 存储与鉴权
- 复用 `cloud_docs`，**`uid=0` + `scope='platform'` + `doc_key='services'`** 存全局配置（uid=0 不可能是真实用户，用户 uid 全来自服务端加密会话 Cookie，无法伪造 0）
- `GET /api/platform/services` = `require_session`（所有登录用户可读）；`PUT` = `require_admin`，整体覆盖 + `base_revision` 乐观锁（冲突 409）
- `_ALLOWED_FIELDS` 16 个 + `updatedBy` 审计；下发补 `keyPresent` 布尔
- 模块：`app/routers/platform_services.py`（已在 `app/main.py` 注册）

### 前端接入
- `hostedClient.ts`：`fetchPlatformServices()` / `publishPlatformServices()`
- `useHostedStore`：`platformServices` + `loadPlatformServices()`（失败静默降级）；`refresh()` 预取；`logout()` 清空
- `App.tsx` 注入 effect：`sharedEntries` 与平台 Key 池**在同一函数式 setState 内合并**；`withoutShared = prev.filter(platformSource!=='1')` 后**重铺服务端最新一份**。**不要拆成两个 setUserApiKeys**（会互相覆盖）
- ⚠️ **注入尾部必须 `return mergeSuggestedProductRouteMappings(entry)`**（服务端优先、缺失才推导）。否则服务端没带 `routeMappings` → 普通用户解析不出图片路由 → **「文生图/图生图」按钮消失**（管理员本地 keyVault 会自动补全所以有）。**任何往 key 列表注入条目的路径都要走这套补全**
- `SettingsPanel.tsx`：`platformSourceKeys`（只读区）与 `managedApiKeys`（可增删改）**分开过滤**，后者排除两个标记；管理员显示「编辑 / 删除」（内联表单）+「发布到平台」

### 🔧 平台服务去重 / 管理员编辑（2026-09-14 修复）
- **重复条目根因**：发布时去重键用条目**本机随机 id**，而服务端条目的 id 与本地不同 → 每次发布都新增一条。修：`sameService(a,b)` 按**服务身份**（provider + baseUrl 去尾斜杠 + imageGenModel + videoGenModel + defaultModel）判定，`find(s => s.id === entry.id) || find(s => sameService(s, entry))`，并过滤掉其余同身份条目
- **管理员此前只读**：现 `hostedIsAdmin` 时平台卡片显示「编辑 / 删除」，编辑态用独立状态 `editingPlatformId`/`platformDraft`（**与 BYOK 的 `editingKeyId` 分开**，避免串台）；删除 `removePlatformServiceById(serviceId)` 走整体覆盖 PUT
- 回归测试 `tests/platformSharedService.test.tsx`（11 例）+ `tests/platformSharedServiceRouting.test.ts`

### 🔧 前端调试真实运行时的正确姿势
- `App.tsx` 挂在 **`#/app`**（HashRouter），goto `/` 只是 Landing Page，**App 不 mount、effect 不执行**
- **不能「先加载页面再 fetch 登录」**：`status` 只在初始化判定一次。正确：`ctx.addCookies()` 预注入 `bff_session` 再 `goto`；cookie 用 `curl -D - -o /dev/null -X POST .../api/user/login` 取
- agent-browser daemon 跨命令丢页面状态 → 用 **playwright-core + agent-browser 自带 Chrome**：`C:/Users/81068/.agent-browser/browsers/chrome-152.0.7977.64/chrome.exe`
- 页面内 `await import('/services/xxx.ts')` 可直接调前端模块（vite dev 支持），验证「真实运行时数据 → 业务计算」最快

## ⭐ 平台 Key 注入（普通用户唯一可用入口）
- **两个独立存储域，勿混为一谈**：「平台 AI 服务」= 服务端共享（`/api/models` + `/api/me/ensure-key`），**所有登录用户可用、不隔离**；「工作流/素材」= 浏览器 IndexedDB，`ns()` 按 uid 隔离。平台 Key 存**每个用户自己的** keyVault（计费落各自配额），与工作流隔离无关。**修 A 不得影响 B**
- `isHostedPlatform(key)` = **只认条目标记**（`flovart_platform==='1' || platformSource==='1'`），**不得加 `status` 必要条件**（status 是瞬时的，冷启动瞬间仍是 `probing` → 条目被静默降级成 BYOK → 拿平台 key 直打上游 401）。**同时严禁放宽到「无标记也当平台」**（会劫持用户自配 BYOK key）
- **守卫只依赖 `hostedAuthed`，严禁依赖 `apiKeysLoaded`**（**正交条件**；一旦读库失败卡 false，平台 Key 永不注入且**一条日志都不打**）
- **`existing` 判断必须用函数式更新** `setUserApiKeys(prev => prev.find(...))`，不得读 effect 闭包快照（批处理期重复注入）
- ❌ 曾用 `platformInjectRef.current` 一次性守卫 → 首跑 `hostedModels` 未回即置 ref → 目录填充被永久挡掉。**已移除**
- ✅ `apiKeysLoaded` 卡死（已修）：加载链包 try/catch 且**无论成败必置位**；`keyVault.migrateLegacyKeys` 绝不外抛
- **铁律：凡门控后续关键逻辑的 boolean 状态，其置位路径必须有 catch 且必须保证置位**，否则故障完全静默
- 「服务为空」类问题**先查前端门控状态，不是后端**
- 模型展示：`productModelCatalog.getProductModel()` 含 `normalizeLoose` 宽松匹配（`. _`→`-`、去版本后缀）→ 管理员配任何同类模型名都不用补 aliases。`inferProductCapability` 含 `NON_CREATIVE_HINT` 黑名单防误判生图。Key 名固定 `'平台模型'`

## 🔧 BFF 接口速查（易错）
- `/api/models` = **GET**，数据在 `data.items`（**不在顶层**）
- `/api/me/ensure-key` = **POST**（GET 会 405）
- `/api/platform/services` = GET（全员读）/ **PUT**（管理员写，body `{services, base_revision?}`）
- `/api/me/docs/{scope}/{doc_key}` = GET/PUT/DELETE，body 是 **`{payload}`**（不是 `{data}`）
- `/api/tasks` = POST `{type, params}`；任务类型见 `app/tasks.TASK_TYPES`

## ⚠️ 网关渠道分组：模型「列表里有但调不了」（2026-09-11 已治本）
- 症状：`model_not_found: No available channel for model X under group default`
- 根因：new-api **渠道绑定分组**；普通用户都在 `default`，而图片模型只挂在 `group='keypool'` 渠道
- ✅ **已修**：`/api/models` 改用**该用户自己的 sk-** 查 `/v1/models`（**按分组过滤**），失败才回落全站。`newapi_client.user_available_models(sk)`（**不带 New-Api-User**）。契约：**目录里有的就是真能调的，不得硬塞能力**
- 判定：管理员账密登网关（PAT 易失效，别用）→ `GET /api/channel/?p=0&page_size=100` 看 `group` 与 `models`
- **仍需网关侧动手**：①分组未配（volc 渠道 group 应含 `default`）②`contents/generations/tasks`（异步）**未实现** → video-gen 与 image-gen(默认 async) 全 502 ③`images/upscale|remove-bg|outpaint|mask|annotate|relight` **未实现** ④**唯一已实现的图片端点是标准 `images/generations`**（临时可用 `.env` `GATEWAY_IMAGE_GEN_MODE=sync`）
- ⚠️ 网关管理员 PAT 易失效（点控制台即作废）→ 排查**直接用账密通道** `POST /api/user/login` 拿 access_token

## 🖼 PromptBar / 设置页的 AI 服务语义（勿误判为 bug）
- **PromptBar 图片模式**：按「AI 服务（key）」列出，每服务只显示一个代表性图片模型 → **只显示一条「平台模型」是正确设计**
- **设置页两类卡片**：`platformEntry`（平台统一配置）vs `managedApiKeys`（各自浏览器，互不可见）
- ⚠️ 管理员在**设置页**加的 Key **不是平台共享服务**，普通用户看不到是**正确隔离**

## ⭐ 云同步与删除一致性（已修）
- **「删掉的工作流又回来」**：`cloudSync` 的「本地已删→云端删」挂在 800ms 防抖 → 删完立即刷新则云端未删 → 下次 `pullOnce` 拉回。修：`flushWorkflowCloudDeletions()` 绕过防抖立即 DELETE。**铁律：任何「删除」都必须立即落远端**
- 纵深防御：`cloudIdsOwnerUid` 归属戳（`pullOnce` 写入、登出清 null），uid 不匹配时**跳过不发**（宁漏删不错删）
- 云同步门控：`storageAligned` 为 false 时禁启 `startWorkflowCloudSync`

## 后端隔离已验证
- `cloud.py` 全部 `_uid(session)` 隔离无问题：跨账号列表空 / 直读 404 / 跨账号 media 404
- 🔒 **跨账号误删结构上不可能**：`cloud_docs` 主键 `(uid, scope, doc_key)`
- ⚠️ **认知陷阱**：A 对 B 的 doc_key 发 DELETE/PUT/GET 返回 **200 而非 404**（落在 A 自己命名空间）。**判断越权必须看「B 端数据是否变化」**
- **排查隔离问题先分清前后端——服务端已证明干净**

## 其他坑
- ⭐ **点弹框致「弹框+侧边栏一起消失」**：`WorkflowSidebar` 用 `panelRef.contains(target)` 判内外，但 **antd Modal 是 portal 挂 body** → 被判「点外部」。修：handler 先 `target.closest('.ant-modal-root,.ant-modal-wrap,.ant-modal,.ant-dropdown,.ant-select-dropdown,.ant-tooltip,.ant-popover')` 命中即 return
- 🪤 **共享服务 Key 自愈**：用户侧条目是登录时快照；管理员改 key 后 `aiGateway.refreshPlatformServiceEntry()` 在**鉴权类失败**时重拉 `/api/platform/services`、按 `platformServiceId` 取最新、**重试一次**（key+baseUrl 未变则不重试）
- 🔑 `request_log.payload` 里 `_gateway.api_key` 恒为 `***` 是**正确设计**（落库前掩码）。**不能**据此推断前端带了假 key —— 要查 uvicorn 日志里 httpx 实际打到上游的状态码
- 🩺 **失败必须留痕**：统一 catch `Exception` + `_error_record(exc, type, stage, url|path, uid)` 落库。**铁律：任何失败分支都要把原因写进 result**。⚠️ `NewApiError` 属性是 **`.message` / `.status_code`**（不是 `.detail`/`.status`）
- 🩹 **401 文案分场景**：`_parse_response` 按 `target` 是否完整 http(s) URL 区分 —— 打外部网关 = 「AI 服务的 API Key 无效」；打 new-api path = 「凭证已失效，请重新登录」
- 🧯 前端错误兜底 `workflowGeneration.describeGenerationError()`：把裸 `Failed to fetch|Network Error|ERR_*` 等翻译成中文提示（含「让管理员把反代超时调到 600s」）
- 弱 `SECRET_KEY` 占位 `dev-only-secret-change-me` 由 `/readyz` 拦截，勿删逻辑
- M1 待办：console 渠道/模型/用户列表契约对真实 new-api **逐条实测**后回填 `newapi_client.py` 头部注释（现标 ⚠️）

## 代码地图
- 后端：`app/routers/{auth,keys,usage,console,billing,promo,chat,shares,platform_services}.py`；`app/user_keys.py`；`app/tasks.py`+`app/thirdparty/wavespeed.py`；`app/{db,oss,security,store,config,newapi_client,cloudstore}.py`
- 前端：`FlovartAgentPanel.tsx` / `services/{browserAgentKernel,imageTask,aiGateway,hostedClient,historyCloudSync,cloudSync}.ts` / `stores/useHostedStore.ts` / `hooks/useApiKeys.ts` / `utils/{storageNamespace,keyVault}.ts` / `components/ConfigManager/*` / `components/workflow/*`
