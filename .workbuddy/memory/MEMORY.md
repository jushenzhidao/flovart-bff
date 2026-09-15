# flovart-bff 长期记忆

## 定位
Flovart「在线创作站」FastAPI BFF：登录/持久化/new-api 代理。前端独立仓库 `D:\code\flovart-web`（同机并存，直接改）。
**操作约定**：不执行任何 git commit/push（飞哥自己来，只在末尾提醒他）。

## 架构铁律
- 登录=BFF 独立注册；注册=管理员影子建 new-api 号+赠送+自动登录（口令只换 PAT，不存密码）
- 会话=AES-256-GCM 加密 Cookie（`app/security.py`，服务端零存储，勿改回仅签名）
- 云端：PostgreSQL(asyncpg)+OSS/COS/S3；本机兜底 `USE_PG=False`→LocalMeta(SQLite)+LocalBlob(本地文件)。**cloudstore 改动须同时实现 Pg/Local 两套**
- new-api 双头 `Authorization:Bearer <PAT>` + `New-Api-User:<uid>`；登录即 DELETE sessions/{sid} 归还（50 上限硬拒绝）
- **多业务隔离**：每业务必须用**独立** new-api 管理员账号(uid)，严禁共用（PAT 账号级、每次重签作废旧值 → 互踢 401 雪崩）
- ⚠️ `.env` 的 `NEWAPI_ADMIN_PAT` 是易失效一次性快照（点控制台「系统访问令牌」即作废，预期行为）→ **不要让飞哥维护 PAT**；PAT 401 时自动回落账密通道 `_admin_login` 重签一次。失效只需改 `NEWAPI_ADMIN_PASSWORD`（同账号）
- 出口 quota→points（`config.quota_to_points*`），裸 quota 不外泄；单 worker
- BFF venv：`C:\Users\81068\.workbuddy\binaries\python\envs\flovart-bff`；Windows 下 `pip install -r` 用 `D:\` 路径（Git Bash `/d/` 会被误解析）

## 聊天免 Key
- `/api/chat/completions` 走 new-api `/v1`（**只认 sk-，不认 PAT**）；每用户按需 mint+持久化归属配额的 sk-（`app/user_keys.py`，AES-GCM 落盘 0o600）
- 模型须配 `BFF_CHAT_DEFAULT_MODEL` / `BFF_CHAT_VISION_MODEL`，否则聊天 400

## 图片/视频任务（`app/tasks.py` 同步/异步双模式）
- `TASK_TYPES` 每项含 `mode` + `async_path`/`sync_path` + `provider`(gateway/thirdparty)；`multi-angle` 走 thirdparty(wavespeed)
- ⭐⭐⭐ **`/v1` 只认用户 `sk-`，绝不能拿管理员 PAT 打（2026-09-15 血案，与 chat.py 同款约束）**
  - `na.request_as_user()`（PAT + `New-Api-User`）**只适用于管理类 `/api/*`**；打 `/v1/*` 一律
    **401 Invalid token** → 普通用户生图必失败（**管理员在控制台直连不经过这条路，所以"管理员能用"**）。
  - ✅ 统一走 `tasks._gw_call(method, path, uid, ...)`：内部取该用户 sk-
    （`user_keys.get_key` → 无则 `admin_mint_user_api_key(uid)` 代建并持久化），**401 自动轮换 sk- 重试一次**。
    ⚠️ **tasks.py 里不允许再出现 `request_as_user`**（4 处已全部替换：异步提交/同步调用/轮询/取消）。
  - 排查判据：`/api/tasks` 返回「凭证已失效」（登录态却正常）→ 就是拿了 PAT 打 /v1。
- ⭐⭐⭐ **`image-gen` 必须 `mode=sync`（`GATEWAY_IMAGE_GEN_MODE` 默认已改 sync，勿改回 async）**
  - 本网关**图片模型只支持同步端点 `v1/images/generations`**。异步端点 `v1/video/generations` 是
    **视频任务语义**，网关按 video 拼上游 URL → 图片模型必 **404 `fail_to_fetch_task`**（上游 Not Found）。
  - 实测对照（同模型 `gpt-image-2.5-flare`、用户 sk-）：
    `POST /v1/images/generations` → **200 / 63.5s 真实出图（b64_json）** ✅ ；
    `POST /v1/video/generations` → 404 fail_to_fetch_task ❌
  - `fail_to_fetch_task` 语义（源码 `relay/relay_task.go`）：**提交阶段上游响应为空或非 200**（≠查询失败）。
  - sync 模式下前端体验不变（仍「提交 → 轮询 `/api/tasks/{id}`」），BFF 后台阻塞等待。
- ⚠️ **async 分支的 body 形态**：`{"type": kind, "params": params}` 会被网关报
  `Model name not specified`（它要**顶层** `model`）。video-gen 仍走 async，网关需在顶层拿到 model。
- `submit(uid,kind,params)`：带 `_gateway`→BFF 直连用户指定外部网关（用户 BYOK）；否则走平台网关（计费落用户配额）
- ⭐ **外部网关调用非阻塞（2026-09-14）**：`_run_external` 落请求日志后 `asyncio.create_task`（强引用集 `_BACKGROUND_TASKS`）立即返回 `processing`。**根因**：原阻塞到底 + nginx `proxy_read_timeout 60s` < 生图 60~62s → 上游已出图但响应被掐 → 前端裸 `Network Error`。⇒ 部署 nginx 读超时调 **600s**（`data/release/nginx.conf` 已改）
- 前端轮询键用返回 `id`(=request_id)，非旧 task_id；`processing` 是中间态非终态
- 产物统一落 BFF 对象存储（元数据入 PG `cloud_media`），result 改写为 `/api/me/media/{key}` + `_bffMediaKey`
- 图片归一化 `_normalize_sync_result`：OpenAI `{data}`→`{images}`；前端 `extractImageOutputs` 扫 `image/images/layers/data`
- ⭐ **图片参考图入参契约（curl 实测，勿翻转）**：网关 `image` **必须是 data URL 字符串数组** `["data:image/png;base64,..."]`。对象 `{data,mimeType}` → 422；`[{data,mimeType}]` → 422
- ⭐ **图片端点已全面收敛（2026-09-14 飞哥拍板）**：`app/config.py` **9 个 `GATEWAY_SYNC_*_PATH` 全部 = `v1/images/generations`**（image/upscale/remove-bg/split/outpaint/mask/annotate/relight/edit）。上游网关只实现 `/images/generations`，**不实现 `/images/edits` 等语义化端点**（必 404），能力靠入参（`image[]`/`mask`/`variant`/`task`）区分。**勿再改回语义化路径**
- ⭐⭐⭐ **网关路径必须带 `v1/` 前缀 + 异步端点是 `v1/video/generations`（2026-09-15 两轮血案，极易改错）**
  - **前缀**：`base_url = NEWAPI_BASE_URL`（不带 /v1），故所有相对路径必须自带 `v1/`。
    ⛔ 漏掉 → 打到 nginx 上不存在的路径 → **被兜底给 new-api 前端 SPA** → 返回 `200 text/html`（`<title>New API</title>`）→ BFF 解析 JSON 失败 → 前端 **502** 并**误报**「网关未实现该端点」。
    **快速判据：返回 `text/html` = 路径不存在（落到前端兜底）；返回 `application/json` = 端点存在。**
  - **异步端点**：`POST /v1/video/generations`（提交）+ `GET /v1/video/generations/{task_id}`（轮询），
    ⛔ **不是** `v1/contents/generations/tasks`（**实测 404**，早期是照契约文档未实测填的）。
    依据 new-api 源码 `router/video-router.go`；图片/视频**共用该组**靠 body 分流 → image/video 两 env 默认同值。
  - new-api **未实现** `DELETE` 取消路由（实测 404）→ `cancel_task` 需**容忍失败**（记 warning 后照常标 cancelled）。
  - 回归测试：`tests/test_gateway_path_prefix.py`（锁 `v1/` 前缀 + 异步路径值 + 轮询拼接形态）
- ⭐ **前端端点选择按 baseUrl 判定，不按 provider**（2026-09-14）
- 契约文档：`docs/gateway-endpoint-contracts.md`、`docs/gateway-mapping-design.md`、`IMAGE-ASYNC-TASKS-CONTRACT.md`

## 素材共享（点对点）
- `admin_resolve_uid_by_username`；`cloud_media_shares`(owner+target+media_key+perm)；字节不复制，`/api/shared/media/{key}` 代理校验
- 共享本地素材前先 `ensureCloudKey` 上传 BFF 拿 cloudMediaKey

## 工作流素材云端持久化
- 项目元数据走 cloudSync→`cloud_docs`；**素材字节**经 `media.ts` 的 `ensureProjectMediaUploaded` 上传 `/api/me/media`，cloudMediaKey 回写节点 metadata（节点/封面/分层各一份）
- 读取兜底：`loadWorkflowMediaBlob` 本地 miss → `restoreWorkflowMediaFromCloud`
- 索引 `registerWorkflowCloudMediaIndex`（storageKey→cloudMediaKey），在 `setWorkflowMediaCanonicalProjects`/`pullOnce`/projects 订阅时重建
- **铁律：上传失败只 warn 不抛（下次 push 重试）；回写 metadata 不改 `updatedAt`，否则 schedulePush 死循环**
- 生成历史镜像到 `cloud_docs` 的 `history` scope（doc_key=generations，整体覆盖+revision 乐观锁）；本地 IndexedDB 仍为主存，云端负责跨设备恢复

## 🔴🔴 产品模型解析口径铁律（已栽四次，务必遵守）
前端「产品模型」（用户能选的创作模型，如 `gpt-image-2` / `gpt-image-2.5-flare`）有**两套解析函数**，用错必出 bug：

| 函数 | 语义 | 未登记模型（用户自配网关模型） |
|---|---|---|
| `getProductModel(v)` | **只查登记目录** `PRODUCT_MODEL_CATALOG`，判定「是否登记」 | 返回 `undefined` |
| `resolveDisplayableProductModel(v)` | 登记模型走精确定义；未登记按能力**合成** `flovart:custom:<name>`；**幂等** | ✅ 返回合成定义 |

- ⛔ **凡「要拿到定义去干活」的地方（执行/路由/参数/闸门/展示/选项）一律用 `resolveDisplayableProductModel`**。
- ⛔ `getProductModel` **只允许**出现在「确实需要判定是否登记模型」的场景（如 `isRegisteredProductModel`）。
- ⛔ **幂等铁律（2026-09-15 血案）**：`resolveDisplayableProductModel` 传入**已是** `flovart:custom:<name>` 的值时，**必须原样解析回同一份定义，绝不能二次合成**。早期实现只查登记目录+兜底合成，会产出 `flovart:custom:flovart:custom:X`（套娃）→ 所有「先解析拿 id、再拿 id 查路由/能力」的调用点全部失配。**已修**：函数首行 `if (value.startsWith('flovart:custom:')) return resolveProductDefinition(value);`
- ⛔ **自查口诀**：出现「**能选中/能看到，但一用就报错或没反应**」→ **第一件事比对两侧用的是不是同一个解析函数**。

### ⭐ 准入原则（飞哥 2026-09-15 拍板，最高优先级，勿再加白名单）
> 「不应该限制产品目录啊，加入用户自己配置的模型，也不能用吗？**直连的不需要经过白名单的，只要配置的能通就可以使用**。」

- **产品目录只用于给「登记模型」提供精确渲染参数，绝不作为「能不能用」的准入闸门。**
- 准入唯一依据 = **能推断出创作能力（image/video）+ key 暴露该路由（能连通）**。三类来源一视同仁：① 用户 BYOK 自配 ② 管理员发布的平台服务 ③ 登记模型
- **已全量放开 12 处**（均 `getProductModel` → `resolveDisplayableProductModel`）：
  1. `services/workflowGeneration.ts` — 提交闸门（抽出 `resolveWorkflowProductModel` 导出以便测试）
  2. `services/generationCapabilities.ts` — 参数面板（未登记模型走 `getCapabilityDictionary` 通用字典兜底）
  3. `services/promptBarPolicy.ts` — 模型标签（不再退化成「图片 AI 服务」）
  4. `services/aiGateway.ts` — `platformImageModel` 匹配
  5. `components/PromptBar.tsx` — 服务列表 fallback
  6. `components/SettingsPanel.tsx` — 卡片展示文案（3 处）
  7. `services/providerGenerationAdapter.ts` — `supportedModes()`（**原报错点**：「当前 AI 服务不支持「text-to-image」，不能降级为其它生成方式。」）
  8. `utils/modelRefs.ts` — 6 处（`keyOwnsBareModel`/`normalizeModelSelectionWithKeys`/`buildCapabilityModelOptions`/`modelRefProvider`/`modelRefLabel`/`modelRefSearchText`）
  9. `services/workflowPromptPolicy.ts` — `resolveWorkflowDefaultModel`
  10. `hooks/useApiKeys.ts` — `dynamicModelOptions`（PromptBar 服务列表）
  11. `App.tsx` — 平台 Key 注入时的默认图片/视频/文本模型识别
  12. `utils/platformModelGate.ts` — `productAvailableViaPlatform`（**第二道拦截**：App.tsx 过滤 `dynamicModelOptions` 时会把 custom id 再滤掉）
- **仍未放开（有意保留）**：非创作模型黑名单（`inferProductCapability` 返回 null 者，如 `text-moderation-latest`）。白名单放开 ≠ 什么都放行。
- **回归测试** `tests/unregisteredModelRouting.test.ts`（19 例）——含「四链路整体放行」5 例 + 「第二轮执行侧 4 调用点」6 例 + 幂等铁律 + 平台闸门。**对照组已验**：回退任一处 → 对应用例精准失败。
- ⚠️ **排查提醒**：`resolveRouteMapping` 读的是 key 上**已持久化**的 `routeMappings`；单独造 key 测试时必须先过 `mergeSuggestedProductRouteMappings`（真实保存路径会做），否则误判成「解析不出来」。

## 🔴 产品模型条目拆分（2026-09-08，GPT Image 2.5 双模型）
- 飞哥会配 `GPT-Image-2.5-Flare`（速度优先）与 `GPT-Image-2.5-Sunburst`（精度优先）**两个独立模型**，下拉里必须**按模型名区分**。
- ⛔ 曾把两者登记为**同一** `flovart:gpt-image-2.5` 的 `officialModelIds` → 同 id → 下拉只有一条「GPT Image 2.5」→ 用户无法区分。**必须拆成两个独立条目**。
- 拆分要点：`tools/flovart/product-models.js` 两条 + `services/productModelCatalog.ts` 的 `CAPABILITY_BY_ID` 两条（均 `GPT_IMAGE_2_5_QUALITIES`=`['low','medium','high','xhigh','max']`）；`normalizeLoose` **不剥 `-flare`/`-sunburst` 后缀**，只能靠 officialModelIds/aliases 精确命中。
- `promptBarPolicy.productFamily()` 按 `includes('gpt-image')` 归组 → 两者落同一「GPT Image」family（左侧分组相同，右侧模型名不同，可区分）。**PromptBar 最终展示给用户的是 `product.name`。**

## 🔴 前端本地保险库持久化铁律（keyVault，2026-09-15 血案）
`utils/keyVault.ts` = 浏览器端 API Key 加密库（PBKDF2 100k + AES-GCM，密文进 IndexedDB/localforage，实例名经 `ns()` 带 `__u<uid>`）。
- ⛔ **禁用 `btoa(String.fromCharCode(...bytes))`**：展开运算符把每个字节变成**函数实参**，V8 超过 **~128KB 抛 `RangeError: Maximum call stack size exceeded`**。必须用 `bytesToBase64()`（分块 32KB + `apply(null, subarray)`）。解码侧 `Uint8Array.from(atob(...))` 无此问题。
- **触发条件极易命中**：`SettingsPanel.handleSaveKey` 会把该端点**全部模型清单**（真实网关 743 个）写进 `customModels`/`models` → 序列化后远超阈值。小 key 能存、大 key 静默存不进。
- ⛔ **`saveKeysEncrypted` 必须返回成败且把 `encryptKeys` 纳入 try**：历史上 fire-and-forget、异常只在 catch 里 `console.error` → **UI 提示「已保存」但实际没落盘**（飞哥「强刷新后服务消失」的原始现象）。调用方 `hooks/useApiKeys.ts` 持久化 effect 已改为检查返回值并告警。
- **排查手法（可复用）**：`page.addInitScript` hook `IDBObjectStore.prototype.put/delete/clear` + `page.on('pageerror', e => e.stack)`。前者一秒分清「没写库」还是「写了读不回」，后者直接给栈。
- **回归测试** `tests/browserPersistence.test.ts`（4 例，含 200KB+ payload 大对象用例；改 keyVault 前必跑）

## 🎯 多账号本地存储隔离（已闭环，勿推翻）
`utils/storageNamespace.ts` 的 `ns()` 给 localforage 实例名追加 `__u<uid>`；14 个存储模块在**模块顶层**调用；登录/登出**整页 reload** 重建实例。
- **真根因 = ES module import 求值顺序**：`index.tsx` 的 `import { RouterHost }` 会传递求值整条 storage 链 → 若 `primeStorageNamespace()` 写在其后**晚了**。
- **修法（两条必须同时满足）**：① `storageNamespace.ts` **文件末尾裸调用** `primeStorageNamespace();` ② `index.tsx` 首行 `import './utils/storageNamespace';`
- **`bff_uid`**：`bff_session` 是 httponly，JS 读不到 uid → BFF 额外种 `httponly=False` 的 `bff_uid`。**非凭据**，只用于挑 IndexedDB 库名后缀
- **HashRouter 铁律**：凡依赖整页 reload 重建模块顶层单例者，**禁用 `location.assign(pathname#x)`** → 须 `window.location.hash = x; window.location.reload();`
- reload 防循环：`clearStorageReloadMarker` / `consumeStorageReloadMarker`(读后即焚) / `markStorageReload`
- ⛔ **运行时动态实例方案已回退，勿再引入**
- **回归测试** `tests/storageNamespaceIsolation.test.ts`（17 例，改存储隔离前必跑）

## ⭐⭐⭐ 分支策略（2026-09-14 起，务必先读）
**`main` = 以「公共平台服务」为主的线上版本**；**`dev` = 接入网关模型新需求的开发区**。
- `main` 上「平台共享服务」= **旧语义（共享 Key）**：条目含 `key`+`baseUrl`，用户侧持管理员 key 直连，**不经 new-api 计费**。`_gateway` **必须带**。
- `dev` 上「平台共享服务」= **新语义（模型清单）**：条目**仅含模型清单**（无 key/baseUrl），用户用**自己的默认 Key** + BFF 下发的 `gatewayBaseUrl` 走 new-api，**计费落用户自己配额**。`_gateway` **绝不能带**；BFF `_FORBIDDEN_FIELDS=("key","baseUrl")` 强制剔除。
- ⚠️ **基线 tag 不可信**：init commit（web `5ad25eb`/bff `6538bc7`）**已含** dev 改造，`dev-baseline-20260914` 不是改造前快照，回退只能手工反向改写。main 回退 = web `f08ff71` / bff `8acc81d`。
- ⚠️ **飞哥要求发布到 main 给普通用户用**：新增模型必须在 main 语义下也能走通（平台共享服务 + 平台 Key 池）。

## ⭐⭐ 平台共享 AI 服务（管理员发布 → 服务端存储 → 全员拉取）

### 🔑 两个标记、三条链路
| | 平台 Key 池 | 平台共享服务（**main=旧语义**） | 平台共享服务（**dev=新语义**） |
|---|---|---|---|
| 标记 | `extraConfig.flovart_platform='1'` | `extraConfig.platformSource='1'` | 同左（标记不变，语义变） |
| 来源 | `/api/me/ensure-key` 按用户签发 | `/api/platform/services` 管理员发布 | 同左 |
| 密钥 | 每用户一把自己的 new-api token | 管理员那把 key **下发给所有人** | **不含密钥**，用用户自己的 sk- |
| 链路 | BFF 网关代发 → new-api **计费落各自配额** | `params._gateway` 交 BFF `_run_external` 直连外部网关 | 用户自己的 sk- 打 BFF `gatewayBaseUrl` → new-api |
| 计费 | new-api | **不经 new-api** | new-api，**落用户自己配额** |

两者**并存不互斥**，**严禁复用同一标记**。BFF `_sanitize()` 强制剔除 `flovart_platform` 并有单测守护。
**两者都必须被 `isHostedPlatform()` 认作平台模式**（否则掉 BYOK 直连 → 拿本地 key 直连上游），区别只在「要不要带 `_gateway`」：共享服务带、Key 池不带。注入函数 `platformSharedGatewayParams()`。

### 存储与鉴权
- 复用 `cloud_docs`，**`uid=0` + `scope='platform'` + `doc_key='services'`** 存全局配置；字段名是 **`payload`**（不是 `data`）
- `GET /api/platform/services` = `require_session`；`PUT` = `require_admin`，整体覆盖 + `base_revision` 乐观锁（409）
- `_ALLOWED_FIELDS` 16 个 + `updatedBy` 审计；下发补 `keyPresent` 布尔
- 数据形状：`{services:[{id,provider,name,baseUrl,key,capabilities,customModels,defaultModel,imageGenModel,imageGenMode,videoGenModel,videoGenMode,routeMappings,extraConfig}],revision}`
- 模块：`app/routers/platform_services.py`（已在 `app/main.py` 注册）

### 发布链路（普通用户可用性）
`GET /api/config` → `detectHosted()` → `GET /api/platform/services`（require_session，全员可读）→ `GET /api/models` → `POST /api/me/ensure-key`。

### 前端接入
- `hostedClient.ts`：`fetchPlatformServices()` / `publishPlatformServices()`
- `useHostedStore`：`platformServices` + `loadPlatformServices()`（失败静默降级）；`refresh()` 预取；`logout()` 清空
- `App.tsx` 注入 effect：`sharedEntries` 与平台 Key 池**在同一函数式 setState 内合并**。**不要拆成两个 setUserApiKeys**
- ⚠️ **注入尾部必须 `return mergeSuggestedProductRouteMappings(entry)`**。否则服务端没带 `routeMappings` → 普通用户解析不出图片路由 → **「文生图/图生图」按钮消失**。**任何往 key 列表注入条目的路径都要走这套补全**
- `SettingsPanel.tsx`：`platformSourceKeys`（只读区）与 `managedApiKeys`（可增删改）**分开过滤**，后者排除两个标记
- 服务卡片副标题**优先显示 `imageGenModel`**（编辑弹窗「模型名称」写的是它）；`defaultModel` 是另一个字段（端点探测推导），只显示它会「弹窗填 2.5、卡片显示 2」

### 前端模型选项生成链路（完整）
1. `App.tsx` `platformFilteredModelOptions`（useMemo）：`sharedModels = hostedPlatformServices.flatMap(s => s.models?.length ? s.models : s.customModels || [])`；未登录或 allowed 空 → 直返 `dynamicModelOptions`；否则对 image/video 分别调 `filterProductModelsByPlatform()`。**只过滤不新增。**
2. `hooks/useApiKeys.ts` L168-201 `dynamicModelOptions`：按 key 的 `imageGenModel`（优先）或 `customModels` 逐条 `resolveDisplayableProductModel(raw).id` 去重 → image 桶；空则回退全量目录。
3. `utils/modelRefs.ts` `buildCapabilityModelOptions()`：底 = `getProductModels(capability).map(m => m.id)`；unshift 当前选中项；再把 `extraConfig.flovart_platform === '1'` 的 key 的 `customModels` 解析结果 push（L104-123）；**不暴露网关原始名**。
4. `modelRefLabel()` / `modelRefSearchText()`：`resolveDisplayableProductModel(value)?.name`。
5. `services/promptBarPolicy.ts` `productFamily(model)`：`model.id.includes('gpt-image') → 'GPT Image'`；`productModelGroups` 按 family 聚合；`filteredProductModelGroups` 按 capability 过滤；`displayedModelGroup` = active family 对应组或第一组。
6. `components/PromptBar.tsx` 渲染：左侧 family 按钮（`group.family` + `group.company · 已配置/总数`），右侧 `displayedModelGroup?.models.map(product => ...{product.name}...{product.badge || '已连接'})`。**最终展示给用户的就是 `product.name`。**

### 🔧 平台服务去重 / 管理员编辑（2026-09-14 修复）
- **去重**：`sameService(a,b)` 按**服务身份**（provider + baseUrl 去尾斜杠 + imageGenModel + videoGenModel + defaultModel）判定，**不得用本机随机 id**
- **管理员可编辑**：`hostedIsAdmin` 显示「编辑/删除」；编辑态 `editingPlatformId`/`platformDraft`（**与 BYOK 的 `editingKeyId` 分开**）
- 回归 `tests/platformSharedService.test.tsx`（11 例）+ `tests/platformSharedServiceRouting.test.ts`

### 🔧 前端调试真实运行时的正确姿势
- `App.tsx` 挂在 **`#/app`**（HashRouter），goto `/` 只是 Landing Page，**App 不 mount**
- **不能「先加载页面再 fetch 登录」**：`status` 只在初始化判定一次。正确：`ctx.addCookies()` 预注入 `bff_session` 再 `goto`；cookie 用 `curl -D - -o /dev/null -X POST .../api/user/login` 取
- 测试账号：`POST /api/user/register`（BFF 自有注册，非 new-api）
- agent-browser daemon 跨命令丢页面状态 → 用 **playwright-core + agent-browser 自带 Chrome**：`C:/Users/81068/.agent-browser/browsers/chrome-152.0.7977.64/chrome.exe`（实测 `chromium.launch({channel:'msedge'})` 亦可）
- 页面内 `await import('/services/xxx.ts')` 可直接调前端模块（vite dev 支持）——**验证解析链最快的办法**，比点 UI 可靠
- ⚠️ **Playwright 已知问题**：`addCookies` 注入 `bff_session`/`bff_uid` 后，`useHostedStore.status` 可能停在 `probing`。绕行：脚本内 fetch 登录 / UI 真实登录 / `page.evaluate` 注入 store
- ⚠️ **Git Bash 路径陷阱**：`/tmp/xxx.mjs` 传给 node 会解析成 `D:\tmp\xxx.mjs`（MODULE_NOT_FOUND）→ 脚本必须放项目目录
- ⚠️ **Grep 全仓搜索易超时/SIGTERM** → **按 path 限定到单目录/单文件**才稳定。耗时操作前先 `cp` 备份，用 `cp` 还原（不用 git stash/checkout）
- `UID` 是 bash 保留变量 → 用别的名；`curl -D` 写 `'D:/code/_hdr.txt'`

## ⭐ 平台 Key 注入（普通用户唯一可用入口）
- **两个独立存储域**：「平台 AI 服务」= 服务端共享（**所有登录用户可用、不隔离**）；「工作流/素材」= 浏览器 IndexedDB，`ns()` 按 uid 隔离。**修 A 不得影响 B**
- `isHostedPlatform(key)` = **只认条目标记**，**不得加 `status` 必要条件**（status 瞬时，冷启动仍 `probing` → 静默降级 BYOK → 拿平台 key 直打上游 401）。**同时严禁放宽到「无标记也当平台」**
- **守卫只依赖 `hostedAuthed`，严禁依赖 `apiKeysLoaded`**（正交条件；读库失败卡 false 则平台 Key 永不注入且**一条日志都不打**）
- **`existing` 判断必须用函数式更新**，不得读 effect 闭包快照
- ❌ 曾用 `platformInjectRef.current` 一次性守卫 → 首跑 `hostedModels` 未回即置 ref → 目录填充被永久挡掉。**已移除**
- ✅ `apiKeysLoaded` 卡死（已修）：加载链包 try/catch 且**无论成败必置位**
- **铁律：凡门控后续关键逻辑的 boolean 状态，其置位路径必须有 catch 且必须保证置位**
- 「服务为空」类问题**先查前端门控状态，不是后端**

## ⭐⭐ 路由映射两处口径必须一致（2026-09-14 真 bug）
- **`keyModels()`（生成映射）含 `imageGenModel`，而 `keyExposesRoute()`（校验可用性）曾漏掉它** → 用户只改「模型名称」（只写 `imageGenModel`、不同步 `customModels`）后：生成映射 ✅ / 校验可用 ❌ → `routeAvailable=false` → `activeRoute=null` → PromptBar 判 `missing-key` → **点提交弹「配置 AI 服务」弹框**
- 修：`services/routeMapping.ts` 的 `keyExposesRoute` 口径补齐为 `[defaultModel, imageGenModel, ...models[].id, ...customModels]`（全部 trim+lowercase 后比对）
- **铁律：凡「生成映射」与「校验可用性」分处两函数，字段口径必须逐项对齐**；同源字段别分头维护
- 回归：`tests/routeMapping.test.ts` 有专例

## ⭐ `utils/platformModelGate.ts`（平台闸门，勿误判）
- `productAvailableViaPlatform(productId, platformModels)`：无平台数据 → true；`flovart:custom:` 剥前缀后 `platformModels.includes(norm(raw))`；否则 `getProductModel(productId)` 取 `[id, ...officialModelIds, ...aliases]` 归一，先精确匹配，再无长度<4 的宽松子串互含匹配
- `filterProductModelsByPlatform()` 过滤后空则回退原列表
- ⚠️ **宽松子串匹配**意味着平台侧只要有 `gpt-image-2.5-flare`，反向也可能放行 `gpt-image-2.5` —— 拆分后需留意

## 🔧 图片端点选择：按 baseUrl 判定，不按 provider（2026-09-14）
- 现象：`provider='openai'` 只表明「这是 GPT Image 系」，baseUrl 却是第三方网关 → 旧代码 `officialGptImage = provider === 'openai' && isOpenAIImageEditModel(m)` 判真 → 图生图打 `/images/edits` → 网关 404
- 修：新增 `isOfficialOpenAIEndpoint(baseUrl)`（hostname 为 `api.openai.com`/`*.openai.com`/`openai.azure.com`），两处（`generateImageWithProvider`/`editImageWithProvider`）改为 `isOpenAIImageEditModel(mappedModel) && isOfficialOpenAIEndpoint(baseUrl)`
- **注意**：`isOpenAIImageEditModel` 正则 `$` 锚定 → `gpt-image-2.5` 不匹配（**只影响 mask 能力判定，不影响端点选择**，因网关侧统一走 generations）
- 回归：`tests/reproImageToImageBranch.test.ts`（9 例）

## 🔧 BFF 接口速查（易错）
- `/api/models` = **GET**，数据在 `data.items`（**不在顶层**）
- `/api/user/login`（**非** `/api/auth/login`）、`/api/user/self`、`/api/config`
- `/api/me/ensure-key` = **POST**（GET 会 405）
- `/api/platform/services` = GET（全员读）/ **PUT**（管理员写，body `{services, base_revision?}`）
- `/api/me/docs/{scope}/{doc_key}` = GET/PUT/DELETE，body 是 **`{payload}`**（不是 `{data}`）
- `/api/tasks` = POST `{type, params}`；任务类型见 `app/tasks.TASK_TYPES`
- `/api/me/points` 剩余积分；`/api/log/self` 消费记录；`/api/me/requests` 请求日志
- `detectHosted()` 是 hosted 模式**总开关**：请求 `/api/config`，失败 → `status='local'` → **整体退回 Flovart 原生 UI**（BFF 没起时会误以为「UI 改造丢失」）

## ⚠️ 网关渠道分组：模型「列表里有但调不了」（2026-09-11 已治本）
- 症状：`model_not_found: No available channel for model X under group default`
- 根因：new-api **渠道绑定分组**；普通用户都在 `default`，而图片模型只挂在 `group='keypool'` 渠道
- ✅ **已修**：`/api/models` 改用**该用户自己的 sk-** 查 `/v1/models`（**按分组过滤**），失败才回落全站。`newapi_client.user_available_models(sk)`（**不带 New-Api-User**）。契约：**目录里有的就是真能调的，不得硬塞能力**
- 判定：管理员账密登网关（**PAT 易失效，排查别用**）→ `GET /api/channel/?p=0&page_size=100` 看 `group` 与 `models`
- **仍需网关侧动手**：①分组未配 ② ~~`contents/generations/tasks`（异步）未实现~~ → ✅ **2026-09-15 已查明：正确端点是 `v1/video/generations`（当时是 BFF 路径填错，非网关缺失）** ③语义化图片端点未实现（**已收敛到 `v1/images/generations` 规避**）

## 🖼 PromptBar / 设置页的 AI 服务语义（勿误判为 bug）
- **PromptBar 图片模式**：按「AI 服务（key）」列出，每服务只显示一个代表性图片模型 → **只显示一条「平台模型」是正确设计**
- **设置页两类卡片**：`platformEntry`（平台统一配置）vs `managedApiKeys`（各自浏览器，互不可见）
- ⚠️ 管理员在**设置页**加的 Key **不是平台共享服务**，普通用户看不到是**正确隔离**
- **AI 服务三类**：`flovart_platform==='1'`=平台 Key 池（只读）；`platformSource==='1'`=平台共享服务（只读，服务端下发）；其余=本地 BYOK（可编辑）。`platformSource` 唯一注入点是 `App.tsx` 注入 effect

## ⭐ 云同步与删除一致性（已修）
- **铁律：任何「删除」都必须立即落远端**（`flushWorkflowCloudDeletions()` 绕过 800ms 防抖立即 DELETE；曾因挂在防抖 → 「删掉的工作流又回来」）
- 纵深防御：`cloudIdsOwnerUid` 归属戳（`pullOnce` 写入、登出清 null），uid 不匹配**跳过不发**
- 门控：`storageAligned` 为 false 时禁启 `startWorkflowCloudSync`

## 后端隔离已验证
- `cloud.py` 全部 `_uid(session)` 隔离无问题
- 🔒 **跨账号误删结构上不可能**：`cloud_docs` 主键 `(uid, scope, doc_key)`
- ⚠️ **认知陷阱**：A 对 B 的 doc_key 发 DELETE/PUT/GET 返回 **200 而非 404** → **判断越权必须看「B 端数据是否变化」**
- **排查隔离问题先分清前后端**（服务端已证明干净）

## 其他坑
- ⭐ **antd Modal 是 portal 挂 body** → `panelRef.contains(target)` 判内外会误判，致「弹框+侧边栏一起消失」。修：handler 先 `target.closest('.ant-modal-root,.ant-modal-wrap,.ant-modal,.ant-dropdown,.ant-select-dropdown,.ant-tooltip,.ant-popover')` 命中即 return
- 🪤 **共享服务 Key 自愈**：`aiGateway.refreshPlatformServiceEntry()` 在**鉴权类失败**时重拉 `/api/platform/services`、按 `platformServiceId` 取最新、**重试一次**
- 🔑 `request_log.payload` 里 `_gateway.api_key` 恒为 `***` 是**正确设计**。**不能**据此推断前端带了假 key
- 🩺 **失败必须留痕**：统一 catch `Exception` + `_error_record(...)` 落库。**铁律：任何失败分支都要把原因写进 result**。⚠️ `NewApiError` 属性是 **`.message` / `.status_code`**（非 `.detail`/`.status`）
- 🩹 **401 文案分场景**：`_parse_response` 按 `target` 是否完整 http(s) URL 区分 —— 外部网关 =「AI 服务的 API Key 无效」；new-api path =「凭证已失效，请重新登录」
- 🧯 `workflowGeneration.describeGenerationError()` 把裸 `Failed to fetch|Network Error|ERR_*` 翻译成中文
- 弱 `SECRET_KEY` 占位 `dev-only-secret-change-me` 由 `/readyz` 拦截，勿删逻辑
- ⚠️ **改 `BFF_SECRET_KEY` 会让所有加密 Cookie 失效 → 全部账号掉登录**；去重 `.env` 重复行时须保留原始值（曾出现文件名 `env` 少点的坑）
- 🧪 **测试定性方法论**：全量跑有跨文件抖动。**不能只看「修复前后失败数差几」，必须比对失败集合差集**，并对波动文件做「单文件连跑」交叉验证。
  - **既存失败基线**（与本项目改动无关，勿误判为回归）：`platformSharedServiceGateway` 3 条（旧 `_gateway` 语义）、`platformServiceModelList` 1 条（断言误匹配 BFF Python docstring）、`apiGatewayValidation` 3 条、`apiKeyProductRouting` 1 条、`workflowImageToolService` 1 条、`releaseCandidateProviderResilience` 1 条，以及 `workflowRightPanel`/`dockPage`/`workflowImageTools`/`workflowNodeOverlays`/`workflowEditor`/`flovartAgentPanel` 等 UI 组件的 DOM 元素缺失
  - **tsc 基线 = 16 错**（全部在 `dsh-plugin/*`、`ProductionCrewPanel`、`StudioTopMenu`，与本项目无关）
- 🔴 **GPT Image 2.5 参数区分（2026-09-08 已修）**：2.5 五档 quality `['low','medium','high','xhigh','max']`，2 保持三档 `['low','medium','high']`；`PromptBar.tsx` 文案 low→低画质/medium→标准画质/xhigh→超高画质/max→极致画质/其余→高画质；`WorkflowNodePromptBar.tsx` 的 `normalizeQualityForModel()` 用 `getEffectiveProductModelCapabilities()` 夹取非法 quality 回退 `'high'`
- M1 待办：console 渠道/模型/用户列表契约对真实 new-api **逐条实测**后回填 `newapi_client.py` 头部注释（现标 ⚠️）

## 代码地图
- 后端：`app/routers/{auth,keys,usage,console,billing,promo,chat,shares,platform_services,tasks}.py`；`app/user_keys.py`；`app/tasks.py`+`app/thirdparty/wavespeed.py`；`app/{db,oss,security,store,config,newapi_client,cloudstore}.py`
- 前端：`FlovartAgentPanel.tsx` / `services/{browserAgentKernel,imageTask,aiGateway,hostedClient,historyCloudSync,cloudSync,routeMapping,productModelCatalog,promptBarPolicy,providerGenerationAdapter,workflowGeneration,workflowPromptPolicy}.ts` / `stores/useHostedStore.ts` / `hooks/useApiKeys.ts` / `utils/{storageNamespace,keyVault,modelRefs,platformModelGate}.ts` / `components/{SettingsPanel,PromptBar,ConfigManager/*,workflow/*}` / `tools/flovart/product-models.js`
