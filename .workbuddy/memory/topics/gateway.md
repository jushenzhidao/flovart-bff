# gateway（细节卷 · 从 MEMORY.md 拆出 2026-09-16）

> MEMORY.md 只留铁律摘要，血案全过程/完整表格见本文件。

## ⭐⭐⭐ `/v1` 只认用户 `sk-`，绝不能拿管理员 PAT 打（2026-09-15 血案）
- `na.request_as_user()`（PAT + `New-Api-User`）**只适用管理类 `/api/*`**；打 `/v1/*` 一律 **401 Invalid token**
  → **普通用户生图必失败**（管理员在控制台直连不走这条路，故「管理员能用、普通用户不能用」）
- ✅ 统一走 `tasks._gw_call(method, path, uid, ...)`：内部取该用户 sk-（`user_keys.get_key` → 无则 `admin_mint_user_api_key(uid)` 代建并持久化），**401 自动轮换 sk- 重试一次**
  ⚠️ **tasks.py 里不允许再出现 `request_as_user`**（4 处已全替换：异步提交/同步调用/轮询/取消）
- 判据：`/api/tasks` 返回「凭证已失效」但登录态正常 → 就是拿了 PAT 打 /v1
- 同款约束也适用于 `chat.py`（早已用按需 mint 用户 sk- 规避）

## ⭐⭐⭐ `image-gen` 必须 `mode=sync`（`GATEWAY_IMAGE_GEN_MODE` 默认 sync，勿改回）
- 本网关**图片模型只支持同步端点 `v1/images/generations`**。异步端点 `v1/video/generations` 是**视频任务语义** → 图片模型必 **404 `fail_to_fetch_task`**（上游 Not Found）
- 实测对照（同模型 `gpt-image-2.5-flare`、用户 sk-）：
  `POST /v1/images/generations` → **200 / 63.5s 真实出图(b64_json)** ✅ ；`POST /v1/video/generations` → 404 ❌
- `fail_to_fetch_task` 语义（源码 `relay/relay_task.go`）：**提交阶段上游响应为空或非 200**（≠查询失败）
- sync 模式前端体验不变（仍「提交 → 轮询」），BFF 后台阻塞

## ⭐⭐⭐ 网关路径必须带 `v1/` 前缀 + 异步端点 = `v1/video/generations`（两轮血案）
- **前缀**：`base_url=NEWAPI_BASE_URL`（不带 /v1），故相对路径必须自带 `v1/`。
  ⛔ 漏掉 → 打到 nginx 不存在路径 → **被兜底给 new-api 前端 SPA** → `200 text/html` → BFF 解析 JSON 失败 → 前端 **502** 并**误报**「网关未实现该端点」
  **判据：返回 `text/html` = 路径不存在；返回 `application/json` = 端点存在**
- **异步端点**：`POST /v1/video/generations`（提交）+ `GET .../{task_id}`（轮询）
  ⛔ **不是** `v1/contents/generations/tasks`（实测 404，早期照契约文档未实测填的）。依据源码 `router/video-router.go`；图片/视频**共用该组**靠 body 分流
- new-api **未实现** `DELETE` 取消路由（实测 404）→ `cancel_task` 须**容忍失败**（记 warning 后照常标 cancelled）
- 回归：`tests/test_gateway_path_prefix.py`（锁前缀 + 异步路径 + 轮询拼接形态）
- ⚠️ **async 分支 body**：`{"type":kind,"params":params}` 会被网关报 `Model name not specified`（要**顶层** `model`）。video-gen 仍走 async，待对齐

## ⭐⭐ 路由映射两处口径必须一致（2026-09-14 真 bug）
- **`keyModels()`（生成映射）含 `imageGenModel`，而 `keyExposesRoute()`（校验可用性）曾漏掉它** → 用户只改「模型名称」（只写 `imageGenModel`、不同步 `customModels`）后：生成映射 ✅ / 校验 ❌ → `routeAvailable=false` → `activeRoute=null` → PromptBar 判 `missing-key` → **点提交弹「配置 AI 服务」弹框**
- 修：`services/routeMapping.ts` 的 `keyExposesRoute` 补齐为 `[defaultModel, imageGenModel, ...models[].id, ...customModels]`（全部 trim+lowercase 比对）
- **铁律：凡「生成映射」与「校验可用性」分处两函数，字段口径必须逐项对齐**
- 回归 `tests/routeMapping.test.ts` 有专例

## 🔧 图片端点选择：按 baseUrl 判定，不按 provider（2026-09-14）
- 现象：`provider='openai'` 只表明「这是 GPT Image 系」，baseUrl 却是第三方网关 → 旧代码 `officialGptImage = provider==='openai' && isOpenAIImageEditModel(m)` 判真 → 图生图打 `/images/edits` → 网关 404
- 修：新增 `isOfficialOpenAIEndpoint(baseUrl)`（hostname 为 `api.openai.com`/`*.openai.com`/`openai.azure.com`），两处（`generateImageWithProvider`/`editImageWithProvider`）改为 `isOpenAIImageEditModel(mappedModel) && isOfficialOpenAIEndpoint(baseUrl)`
- 注意：`isOpenAIImageEditModel` 正则 `$` 锚定 → `gpt-image-2.5` 不匹配（**只影响 mask 能力判定，不影响端点选择**）
- 回归 `tests/reproImageToImageBranch.test.ts`（9 例）

## ⚠️ 网关渠道分组：模型「列表里有但调不了」（已治本）
- 症状：`model_not_found: No available channel for model X under group default`
- 根因：new-api **渠道绑定分组**；普通用户都在 `default`，而图片模型只挂在 `group='keypool'` 渠道
- ✅ **已修**：`/api/models` 改用**该用户自己的 sk-** 查 `/v1/models`（**按分组过滤**），失败才回落全站。`newapi_client.user_available_models(sk)`（**不带 New-Api-User**）。契约：**目录里有的就是真能调的，不得硬塞能力**
- 判定：管理员账密登网关（**PAT 易失效，排查别用**）→ `GET /api/channel/?p=0&page_size=100` 看 `group` 与 `models`

