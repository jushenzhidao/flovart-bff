# pitfalls（细节卷 · 从 MEMORY.md 拆出 2026-09-16）

> MEMORY.md 只留铁律摘要，血案全过程/完整表格见本文件。

## 其他坑
- ⭐ **antd Modal 是 portal 挂 body** → `panelRef.contains(target)` 判内外会误判，致「弹框+侧边栏一起消失」。修：handler 先 `target.closest('.ant-modal-root,.ant-modal-wrap,.ant-modal,.ant-dropdown,.ant-select-dropdown,.ant-tooltip,.ant-popover')` 命中即 return
- 🪤 **共享服务 Key 自愈**：`aiGateway.refreshPlatformServiceEntry()` 在**鉴权类失败**时重拉 `/api/platform/services`、按 `platformServiceId` 取最新、**重试一次**
- 🔑 `request_log.payload` 里 `_gateway.api_key` 恒为 `***` 是**正确设计**，不能据此推断前端带了假 key
- 🩺 **失败必须留痕**：统一 catch `Exception` + `_error_record(...)` 落库。**铁律：任何失败分支都要把原因写进 result**。⚠️ `NewApiError` 属性是 **`.message`/`.status_code`**（非 `.detail`/`.status`）
- 🩹 **401 文案分场景**：`_parse_response` 按 `target` 是否完整 http(s) URL 区分 —— 外部网关=「AI 服务的 API Key 无效」；new-api path=「凭证已失效，请重新登录」
- 🧯 `workflowGeneration.describeGenerationError()` 把裸 `Failed to fetch|Network Error|ERR_*` 翻译成中文
- 弱 `SECRET_KEY` 占位 `dev-only-secret-change-me` 由 `/readyz` 拦截，勿删逻辑
- ⚠️ **改 `BFF_SECRET_KEY` 会让所有加密 Cookie 失效 → 全部账号掉登录**（也会让 `user_keys.json` 无法解密）；去重 `.env` 重复行时须保留原始值（曾出现文件名 `env` 少点的坑）
- 🧪 **测试定性方法论**：全量跑有跨文件抖动。**不能只看「修复前后失败数差几」，必须比对失败集合差集**，并对波动文件做「单文件连跑」交叉验证
  - **既存失败基线**（与本项目改动无关）：`platformSharedServiceGateway` 3、`platformServiceModelList` 1（断言误匹配 BFF docstring）、`apiGatewayValidation` 3、`apiKeyProductRouting` 1、`workflowImageToolService` 1、`releaseCandidateProviderResilience` 1，以及 `workflowRightPanel`/`dockPage`/`workflowImageTools`/`workflowNodeOverlays`/`workflowEditor`/`flovartAgentPanel` 等 UI 组件 DOM 缺失
  - **tsc 基线 = 16 错**（全在 `dsh-plugin/*`、`ProductionCrewPanel`、`StudioTopMenu`，与本项目无关）
- M1 待办：console 渠道/模型/用户列表契约对真实 new-api **逐条实测**后回填 `newapi_client.py` 头部注释（现标 ⚠️）

## 🔧 BFF 接口速查（易错）
- `/api/models` = **GET**，数据在 `data.items`（**不在顶层**）
- `/api/user/login`（**非** `/api/auth/login`）、`/api/user/self`、`/api/config`
- `/api/me/ensure-key` = **POST**（GET 会 405）
- `/api/platform/services` = GET（全员读）/ **PUT**（管理员写，body `{services, base_revision?}`）
- `/api/me/docs/{scope}/{doc_key}` = GET/PUT/DELETE，body 是 **`{payload}`**（不是 `{data}`）
- `/api/tasks` = POST `{type, params}`；任务类型见 `app/tasks.TASK_TYPES`
- `/api/me/points` 剩余积分；`/api/log/self` 消费记录；`/api/me/requests` 请求日志
- `detectHosted()` 是 hosted 模式**总开关**：请求 `/api/config`，失败 → `status='local'` → **整体退回 Flovart 原生 UI**（BFF 没起时会误以为「UI 改造丢失」）

