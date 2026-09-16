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
- **AI 服务三类**：`flovart_platform==='1'`=平台 Key 池（只读）；`platformSource==='1'`=平台共享服务（只读，服务端下发）；其余=本地 BYOK（可编辑）。`platformSource` 唯一注入点 = `App.tsx` 注入 effect

