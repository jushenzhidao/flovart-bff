# platform-services（细节卷 · 从 MEMORY.md 拆出 2026-09-16）

> MEMORY.md 只留铁律摘要，血案全过程/完整表格见本文件。

## ⭐⭐ 平台共享 AI 服务（管理员发布 → 服务端存储 → 全员拉取）

### 🔑 两个标记、三条链路
| | 平台 Key 池 | 平台共享服务（main=旧） | 平台共享服务（dev=新） |
|---|---|---|---|
| 标记 | `extraConfig.flovart_platform='1'` | `extraConfig.platformSource='1'` | 同左（语义变） |
| 来源 | `/api/me/ensure-key` 按用户签发 | `/api/platform/services` 管理员发布 | 同左 |
| 密钥 | 每用户一把自己的 token | 管理员那把 key **下发给所有人** | **不含密钥**，用用户自己的 sk- |
| 链路 | BFF 网关代发 → new-api **计费落各自配额** | `params._gateway` 交 `_run_external` 直连外部网关 | 用户 sk- 打 BFF `gatewayBaseUrl` → new-api |
| 计费 | new-api | **不经 new-api** | new-api，**落用户自己配额** |

两者**并存不互斥**，**严禁复用同一标记**。BFF `_sanitize()` 强制剔除 `flovart_platform` 并有单测守护。
**两者都必须被 `isHostedPlatform()` 认作平台模式**（否则掉 BYOK 直连 → 拿本地 key 直打上游），区别只在「要不要带 `_gateway`」：共享服务带、Key 池不带。注入函数 `platformSharedGatewayParams()`。

### 存储与鉴权
- 复用 `cloud_docs`，**`uid=0` + `scope='platform'` + `doc_key='services'`**；字段名是 **`payload`**（不是 `data`）
- `GET /api/platform/services`=`require_session`；`PUT`=`require_admin`，整体覆盖 + `base_revision` 乐观锁（409）
- `_ALLOWED_FIELDS` 16 个 + `updatedBy` 审计；下发补 `keyPresent` 布尔
- 数据形状：`{services:[{id,provider,name,baseUrl,key,capabilities,customModels,defaultModel,imageGenModel,imageGenMode,videoGenModel,videoGenMode,routeMappings,extraConfig}],revision}`
- 模块：`app/routers/platform_services.py`（已在 `app/main.py` 注册）
- 发布链路：`GET /api/config` → `detectHosted()` → `GET /api/platform/services` → `GET /api/models` → `POST /api/me/ensure-key`

### 前端接入
- `hostedClient.ts`：`fetchPlatformServices()`/`publishPlatformServices()`；`useHostedStore`：`platformServices`+`loadPlatformServices()`（失败静默降级）；`refresh()` 预取；`logout()` 清空
- `App.tsx` 注入 effect：`sharedEntries` 与平台 Key 池**在同一函数式 setState 内合并**。**不要拆成两个 setUserApiKeys**
- ⚠️ **注入尾部必须 `return mergeSuggestedProductRouteMappings(entry)`**。否则服务端没带 `routeMappings` → 普通用户解析不出图片路由 → **「文生图/图生图」按钮消失**。**任何往 key 列表注入条目的路径都要走这套补全**
- `SettingsPanel.tsx`：`platformSourceKeys`（只读区）与 `managedApiKeys`（可增删改）**分开过滤**，后者排除两个标记
- 服务卡片副标题**优先显示 `imageGenModel`**（编辑弹窗「模型名称」写的是它）；`defaultModel` 是另一字段（端点探测推导），只显示它会「弹窗填 2.5、卡片显示 2」
- 🔧 **去重**：`sameService(a,b)` 按**服务身份**（provider + baseUrl 去尾斜杠 + imageGenModel + videoGenModel + defaultModel）判定，**不得用本机随机 id**
- 🔧 **管理员可编辑**：`hostedIsAdmin` 显示「编辑/删除」；编辑态 `editingPlatformId`/`platformDraft`（**与 BYOK 的 `editingKeyId` 分开**）
- 回归 `tests/platformSharedService.test.tsx`（11 例）+ `tests/platformSharedServiceRouting.test.ts`

### 前端模型选项生成链路（完整）
1. `App.tsx` `platformFilteredModelOptions`（useMemo）：`sharedModels = hostedPlatformServices.flatMap(s => s.models?.length ? s.models : s.customModels || [])`；未登录或 allowed 空 → 直返 `dynamicModelOptions`；否则对 image/video 分别 `filterProductModelsByPlatform()`。**只过滤不新增**
2. `hooks/useApiKeys.ts` L168-201 `dynamicModelOptions`：按 key 的 `imageGenModel`（优先）或 `customModels` 逐条 `resolveDisplayableProductModel(raw).id` 去重 → image 桶；空则回退全量目录
3. `utils/modelRefs.ts` `buildCapabilityModelOptions()`：底 = `getProductModels(capability).map(m=>m.id)`；unshift 当前选中项；再把 `extraConfig.flovart_platform==='1'` 的 key 的 `customModels` 解析结果 push（L104-123）；**不暴露网关原始名**
4. `modelRefLabel()`/`modelRefSearchText()`：`resolveDisplayableProductModel(value)?.name`
5. `promptBarPolicy.ts` `productFamily(model)`：`model.id.includes('gpt-image') → 'GPT Image'`；`productModelGroups` 按 family 聚合；`filteredProductModelGroups` 按 capability 过滤；`displayedModelGroup` = active family 组或第一组
6. `PromptBar.tsx` 渲染：左侧 family 按钮，右侧 `displayedModelGroup?.models.map(product => product.name + badge)`。**最终展示给用户的就是 `product.name`**

### 🔧 前端调试真实运行时的正确姿势
- `App.tsx` 挂在 **`#/app`**（HashRouter），goto `/` 只是 Landing Page，**App 不 mount**
- **不能「先加载页面再 fetch 登录」**：`status` 只在初始化判定一次。正确：`ctx.addCookies()` 预注入 `bff_session` 再 `goto`；cookie 用 `curl -D - -o /dev/null -X POST .../api/user/login` 取
- 测试账号：`POST /api/user/register`（BFF 自有注册，非 new-api）
- agent-browser daemon 跨命令丢页面状态 → 用 **playwright-core + agent-browser 自带 Chrome**：`C:/Users/81068/.agent-browser/browsers/chrome-152.0.7977.64/chrome.exe`（`chromium.launch({channel:'msedge'})` 亦可）
- 页面内 `await import('/services/xxx.ts')` 可直接调前端模块（vite dev 支持）——**验证解析链最快的办法**
- ⚠️ **Playwright 已知问题**：`addCookies` 注入后 `useHostedStore.status` 可能停在 `probing`。绕行：脚本内 fetch 登录 / UI 真实登录 / `page.evaluate` 注入 store
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

## 🖼 PromptBar / 设置页的 AI 服务语义（勿误判为 bug）
- **PromptBar 图片模式**：按「AI 服务（key）」列出，每服务只显示一个代表性图片模型 → **只显示一条「平台模型」是正确设计**
- **设置页两类卡片**：`platformEntry`（平台统一配置）vs `managedApiKeys`（各自浏览器，互不可见）
- ⚠️ 管理员在**设置页**加的 Key **不是平台共享服务**，普通用户看不到是**正确隔离**
- **AI 服务三类**：`flovart_platform==='1'`=平台 Key 池（只读）；`platformSource==='1'`=平台共享服务（只读，服务端下发）；其余=本地 BYOK（可编辑）
- ⭐ **`platformSource` 影子条目构造点已抽出**：`flovart-web/utils/platformSharedEntries.ts`
  的 `buildPlatformSharedEntries()`。两个调用方都走它（`App.tsx` 的登录首铺 + 定时/聚焦自动对齐），
  **不要再在 App.tsx 里自己 map 一遍**（漂移会让 routeMappings 补全漏掉 → 图片节点「文生图/图生图」按钮消失）。

## ⭐⭐⭐ 平台服务「下架 / 删除」准入闸门（2026-09-16 飞哥需求）

**需求原文**：「我发布过的 AI 服务，我管理员删除掉了，用户使用的时候还是可以看到；
我希望加一个下架按钮、且直接删除后用户这边也不应该看到，就算用户没有刷新拉取，
直接使用也是提示用户模型下架了」

### 为什么「删了还看得到」——两件事，不是一个
| 层 | 机制 | 只治这一层的做法 |
|---|---|---|
| **显示** | 用户本地 keyVault 有**持久化影子条目**（`platformSource='1'`），只在登录/冷启动对齐一次 | 前端过滤 `suspended` + 定时/聚焦重拉 |
| **准入** | 该模型只要还在上游网关渠道里，用户拿**自己的平台 Key** 照样调得通 | ❌ 前端怎么做都没用，**必须服务端拦** |

⇒ **准入判定只能放服务端**，这是本次的核心结论。

### 服务端设计：撤回集 `revoked`（不是允许集）
- 判定规则（纯 diff，`app/platform_catalog.py::merge_revoked`）：
  `(历史撤回集 ∪ 上次已发布集) − 本次已发布集` → 被删/被下架的模型进撤回集；
  **重新发布自动解除**（自愈）；且管理员的**「删除」不需要任何额外接口**，PUT 一 diff 就知道它供给过什么
- 🚫 **绝不能反着写成「模型必须在平台已发布清单里才放行」** —— 会误杀两条正常链路：
  ① 平台 Key 池的模型来自 `/api/models`（用户分组网关渠道目录），**根本不在平台服务清单里**；
  ② 用户 BYOK（`params._gateway`）直连自己的端点。
  撤回集方案天然不碰这两者。
- 落盘：`cloud_docs(uid=0, scope='platform', doc_key='services')` 的 payload 加 `"revoked": {模型名小写: 时间戳}`；
  180 天时效 + 上限裁剪；`catalog.invalidate()` 在 PUT 后**立刻**清 TTL 缓存（下架不能等 3 秒）

### 🔴🔴 撤回集上限：`REVOKED_MAX=500` 曾是**真漏洞**（2026-09-16 本地实测发现）
- **前提认知**：一条平台服务携带的是**整个网关目录**（本地实测 **741 / 743 个模型**），
  **不是**直觉上的「几个模型」。所以「下架一条服务」= 撤回 ~743 个模型名。
- 旧值 500 → `_prune_revoked` 按时间裁掉最旧 243 条 → **那 243 个模型静默漏过闸门、照旧可调用**，
  直接违背「直接使用也要提示下架」。实测：`=500` 保留 500/743 **漏拦 243**；`=20000` 保留 743/743 **漏拦 0**。
- 现策略（`app/platform_catalog.py`）：
  - `REVOKED_MAX = 20000`（**定位是「异常写入的保险丝」，不是业务策略**；必须远大于真实目录规模）
  - `_prune_revoked` 真触发截断时**打 ERROR 并说明「这些模型将不再被闸门拦住」**，绝不静默
  - 回归测试锁死：743 模型全量撤回不得被裁 / `REVOKED_MAX >= 10000` / 删整条 743 模型服务后抽查首中尾都被拦
- ✅ 可迁移的教训：**任何「为防膨胀而设的上限」，如果裁剪是静默的，就等于自己开了个洞。**
  先问「被裁掉的东西会不会影响正确性/安全性」——会，就只能当保险丝（取值远超真实规模 + 触发即报错）。

### 闸门接在三处（都要有）
| 位置 | 作用 |
|---|---|
| `app/routers/tasks.py::create_task` | 生图/生视频拦截。**`if not body.params.get("_gateway")` 才校验** —— BYOK 外呼必须放行 |
| `app/routers/chat.py::chat_completions` | 聊天拦截 |
| `app/routers/keys.py::/api/models` | 出口过滤（`catalog.filter_available`）→ 用户侧选择器自然消失，双保险 |
- 异常：`ModelSuspendedError` → `main.py` 全局处理器 → **409 + `{success:false, message, code:'MODEL_SUSPENDED'}`**
- ⚠️ **必须返回统一响应壳**：前端 `hostedClient.api()` 只读 `body.message`，
  FastAPI 默认的 `{"detail": ...}` 会退化成「请求失败(409)」→ 又把关键信息吞掉（正是本次要修的毛病）

### 前端（只管显示 + 给管理员按钮）
- `PlatformService.suspended` 类型 + `availablePlatformServices()` 过滤；
  `App.tsx` 的 `sharedEntries` 与 `sharedModels` 两处都要滤
- **自动对齐 effect**：`visibilitychange` + `focus` + 3 分钟兜底 → 重拉并重铺（`sameSharedEntries` 判等避免无谓落盘）。
  ⚠️ 只重铺 `platformSource` 条目，**不要顺手刷平台 Key 池**（那条有 401 重签，重跑会竞态）
- `aiGateway.refreshPlatformServiceEntry`：**服务已被删/已下架 → 不自愈**（旧的 `|| services[0]` 兜底已删，会把用户指到别的服务上）
- 设置页按钮三态：未发布→「发布到平台」；已发布→「下架」；已下架→「重新上架」。
  ⚠️ 已下架条目**不许因为一次「发布」悄悄复活**（`existing.suspended ? {suspended:true}`）
- 🔴 **必须保留「平台服务」管理区（仅管理员可见）**：管理员可能已经删掉本机卡片，
  那时**没有任何入口**能下架/删除服务端条目（这正是飞哥踩到的场景）。
  2026-09-15 把它整块拿掉的判断只对普通用户成立 → 现在 `hostedIsAdmin &&` 门控渲染。
- `_redact`：**下架条目剥掉 key/baseUrl** —— 下架 = 用户不可用，没有理由继续下发凭据

### 为什么先前「按 id 直删」是死代码
`removePlatformServiceById` / `handleUnpublishFromPlatform` 早就写了，但**从没在 JSX 里被调用过**。
所以「管理员删除」实际上只删了本机 keyVault 那条，**服务端条目原封不动** → 所有用户一直看得到。
本次把删除按钮接上，并在删已发布服务时提示「同时从平台移除」。


---

## 🔴🔴 铁律：**服务的模型清单 = 管理员显式配置的，绝不含探测结果**（2026-09-16 飞哥拍板）

### 血案
上游是聚合网关，一份 `/v1/models` 实测 **743 个模型**。前端把探测结果当清单写进服务，两条链路：
1. `SettingsPanel.handleSaveKey`：`finalModels = detectedModelItems.length > 0 ? detectedModelItems : editModels` → **探测覆盖手填**
2. `useApiKeys` 启动时后台刷新 effect：`refreshAllProviderModels` + `mergeFetchedModelsIntoKey` → **每次开应用再灌一次**

⚠️ **只改链路 1 完全无效**。这就是「配置了一个模型，却出现 700 多个」的直接答案。

### 连带效应（都源自同一个污染）
| 现象 | 机制 |
|---|---|
| 设置页显示「模型 743 / 映射 532」 | 清单就是整个网关目录 |
| 选择器里出现没配过的模型 | 同上 |
| **下架一条服务，别的服务也不能用了** | `suspended_models()` 取**并集**；被下架那条携带 738 个模型 → 拦掉全部 4 条服务 |
| `defaultModel` 全是 `gpt-image-2` | 当年 `setEditDefaultModel(modelItems[0].id)` 的残留。**它是探测产物，不是管理员配置**（卡片注释自证「弹窗里没有对应输入框」） |

### 定稿口径
- **显式配置** = 只认 `imageGenModel` / `videoGenModel`；两者都空时才退回 `defaultModel`
- 收窄 = 只保留显式配置的模型，**并且 `routeMappings` 必须「重算」而不是「合并」**
  （`mergeSuggestedProductRouteMappings` 只做并集，清单缩小后旧映射残留 → 用 `suggestProductRouteMappings`）
- 展示名 = **一律显示网关原始模型名**（`gemini-3.1-flash-image-preview`），不做产品目录翻译
- 工具函数在 `flovart-web/services/aiServiceSetup.ts`：`configuredModelsOf` / `needsModelScopeShrink` / `shrinkServiceModelScope`

### 存量治理
一次性迁移 effect + `localStorage['flovart:serviceModelScopeShrink:v1']` 标记。
⚠️ **标记位必须写在 `setState` 外部** —— StrictMode 下更新器会被调用两次，在里面写 localStorage 会出现「标记已置位但状态没更新」。
平台侧用设置页新增的「**同步清单**」按钮覆盖（`listDiverged` 时才显示）。

### 教训
**「探测到的能力」与「管理员声明的配置」是两个东西，永远不要让前者自动覆盖后者。**
聚合网关尤其危险：一次探测返回几百条，UI 上完全看不出异常，但计费/准入/映射全被带偏。
