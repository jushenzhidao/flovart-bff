# 图片任务契约（BFF 同步/异步双模式 + 请求日志）

> 状态：**2026-09-07 架构反转** —— BFF 不再承担异步执行，只做网关异步接口的透传代理；
> **2026-09-07 补丁** —— BFF 同时兼容「同步 / 异步」任务（网关部分模型同步更好用，转异步成本高），
> 且每次提交落一条「用户请求日志」（请求结构 + 参数，按 request_id 落 PG）供后续拉日志对齐。
> 旧版（BFF 内 Redis store + worker 池 + dispatch + 产物落地）已废弃。
> 配套前端：`flovart-web/services/imageTask.ts`。

---

## 1. 核心架构（同步 / 异步双模式）

```
                    ┌─ async ─▶ POST {GATEWAY_IMAGE_TASKS_PATH} ──▶ 网关异步接口
                    │            ◀── task_id（BFF 透传，轮询 GET）─── 网关
前端 ─POST /api/tasks {type,params}─▶ BFF ─┤
     ◀── {id, taskId, mode, status, result?}                     
                    │            ┌─ sync ─▶ POST {GATEWAY_SYNC_*_PATH} ──▶ 网关同步接口（阻塞直出）
                    └─ sync ─────┤            ◀── result（BFF 落盘后回写请求日志）── 网关
                                 └─ 每次提交都写一条请求日志（request_id → PG cloud_request_log）
     ──GET /api/tasks/{id}──────▶ BFF ── async: 转发网关查询 / sync: 返回已存 result
     ◀── {status, result} ──────── BFF
```

- **执行方 = 网关侧**（new-api 兼容，由其他同学实现）。每个 task type 由 `app/tasks.py:TASK_TYPES`
  指定 `mode`：
  - `async`（默认，如 `image-gen`）：网关持有任务状态机；BFF 透传「提交→轮询→取消」。
  - `sync`（`upscale` / `remove-background` / `split-layers` 默认）：网关同步直出；
    BFF **阻塞调用网关同步接口**、拿到结果后立即落盘并回写请求日志，前端仍走「提交→轮询」。
- **BFF 轻状态**：不跑 worker、不存独立任务表；仅写一条「请求日志」用于对齐（见 §2.5）。
  异步任务的状态仍由网关持有。
- **产物统一归 BFF 盘（2026-09-07 拍板「所有创作产物都放 BFF 盘，不分隔」）**：
  当 `GET /api/tasks/{id}` 返回 `status=succeeded` 时，BFF 把 `result` 内图片/视频产物
  （`url` 下载 / `b64_json` 解码）落进**外部对象存储 OSS/COS/S3**（元数据入 PostgreSQL
  `cloud_media` 索引；与画布 projects / 上传素材 / 生成历史共用同一套 BFF 存储），并把
  `result` 改写——每个产物 `url` 指向 `/api/me/media/{key}`、
  附 `_bffMediaKey`。前端 `fetchFirstMedia` / `fetchFirstVideoMedia` 优先走 `_bffMediaKey`
  （带 session cookie 取 BFF 媒体），换设备恢复画布时媒体不再依赖网关有效期。
  **视频产物（video-gen）现已接入 BFF 代理**：走 new-api 平台透传，`_persist_outputs` 以
  `kind="video"` 落盘（mp4 等），与图片同构。仅「非 new-api 平台的第三方视频网关
  BYOK 直连」（Veo/Seedance/Kling/RunningHub）仍由前端拿 blob 后 `POST /api/me/media` 回存。
  两部分最终都进同一个 BFF 对象存储（元数据入 PG）。落盘为异步 `await cloudstore.media_put`
  （BFF→OSS put_object + PG 索引 upsert）；进程内 `_PERSISTED` 做幂等，同进程重复 GET 不重复落盘。
- **鉴权**：BFF 用平台同一把**管理员 PAT** 发起，但请求头带 `New-Api-User: <end-user uid>`
  （new-api 语义：以管理员身份代该用户发起，请求归属与计费落到该用户，并按 uid 隔离任务）。
  → 等价于「视频前端持有用户 PAT 直连网关」，只是 PAT 留在 BFF 服务端、前端永不接触 key。
  ⚠️ 绝不能用 `admin_request`（它会把 New-Api-User 写成管理员自身 uid，导致计费错挂 + 无法隔离）。
  对应实现：`newapi_client.request_as_user(method, path, uid, ...)`。
- **计费边界**：扣费在网关（按 uid 走 new-api 计费）。BFF 全程不扣费、只展示（见 billing.py）。

---

## 2. BFF ↔ 前端接口（已落地）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/tasks` | 提交：`{type, params}` → 返回统一视图 `{id, taskId, kind, mode, status, result?, gatewayTask}` |
| GET  | `/api/tasks/{id}` | 轮询：`id` = 提交返回的 `id`（request_id）；async 转发网关、sync 返回已存 result |
| DELETE | `/api/tasks/{id}` | 取消：async 转网关 DELETE；sync 即时完成不可取消（返 400） |
| GET  | `/api/me/requests?limit=&offset=` | 当前用户请求日志列表（最新优先） |
| GET  | `/api/me/requests/{request_id}` | 单条详情（含完整 `payload` 请求结构 + `result` 产物） |

### 2.1 提交响应（统一视图 `data`）
```json
{
  "id": "req_xxx（request_id，前端轮询用）",
  "taskId": "gw_task_xxx（async=网关 task_id；sync=request_id）",
  "kind": "image-gen | upscale | remove-background | split-layers",
  "mode": "async | sync",
  "status": "submitted | processing | succeeded | failed | cancelled",
  "result": { "...": "产物（async 轮询到 succeeded / sync 直出后才有）" },
  "gatewayTask": { "...网关原生任务对象（含 progress 等）" }
}
```
> 前端轮询用 **`id`**（request_id），不要再用旧版 `task_id` 当轮询键。
> `result` 内的产物 `url` 已被 BFF 改写为 `/api/me/media/{key}` 并附 `_bffMediaKey`；
> 前端 `fetchFirstMedia` 优先走 `_bffMediaKey`（带 session cookie 取 BFF 媒体）。

### 2.5 用户请求日志（拉日志对齐）
- 每次 `POST /api/tasks` 都会在 `cloud_request_log`（PG / 本地兜底 SQLite）写一行：
  `request_id` / `uid` / `kind` / `provider` / `model` / `payload_json`（**用户调用模型时的请求结构+参数**）
  / `task_id` / `gateway_request_id`（对齐网关全站日志 `/api/log` 的 `request_id`）/ `status` / `mode` / `result_json`。
- BFF 与网关日志用 `gateway_request_id` 串联，便于排障时跨系统对齐同一次调用。
- 列表接口只返回摘要（不含 payload/result 大字段），详情接口才返回完整 `payload` + `result`。

---

## 3. BFF ↔ 网关接口（**待网关团队实现**，本文件即契约）

### 3.1 端点
- **异步**：相对 `NEWAPI_BASE_URL`，路径由 `GATEWAY_IMAGE_TASKS_PATH` 配置（默认 `contents/generations/tasks`，
  对齐视频；网关若另开端口用该 env 覆盖）。BFF 透传，**按 `type` 分流由网关负责，BFF 不感知具体端点。**
- **同步**：相对 `NEWAPI_BASE_URL`，路径由以下 env 配置（默认值待网关团队确认，可覆盖）：
  - `GATEWAY_SYNC_IMAGE_PATH`（默认 `images/generations`）—— `image-gen` 在 `GATEWAY_IMAGE_GEN_MODE=sync` 时走此
  - `GATEWAY_SYNC_UPSCALE_PATH`（默认 `images/upscale`）
  - `GATEWAY_SYNC_REMOVE_BG_PATH`（默认 `images/remove-bg`）
  - `GATEWAY_SYNC_SPLIT_PATH`（默认 `images/split-layers`）
  同步调用走独立 client，超时 `GATEWAY_SYNC_TIMEOUT`（默认 300s）。
- 代理（异步）超时 `GATEWAY_PROXY_TIMEOUT`（默认 60s）：只覆盖「提交/查询/取消」转发，网关应快速 ACK。

### 3.2 提交 `POST {path}`
请求体（BFF 原样转发，不校验 params 内部结构）：
```json
{ "type": "image-gen | upscale | remove-background | split-layers", "params": { "...": "原样透传网关" } }
```
响应（建议 HTTP 202）：
```json
{ "task_id": "gw_xxx", "status": "queued" }
```

### 3.3 查询 `GET {path}/{task_id}`
```json
{
  "task_id": "gw_xxx",
  "status": "queued | running | succeeded | failed | cancelled",
  "result": { "...": "产物，见 §3.5" },
  "error": { "message": "失败原因" }
}
```

### 3.4 取消 `DELETE {path}/{task_id}`
- 进行中 → 200/204（取消成功）。
- 已终态 → 409（不可取消）。

### 3.5 `result` 形状（BFF 原样透传，前端 `extractImageOutputs` / `extractVideoOutputs` 兼容多形状）
- **image-gen**：`{ "images": [ { "url"? , "b64_json"? , "mime"? } ] }`，或单图 `{ "url" }` / `{ "b64_json" }`
- **upscale / remove-background**：`{ "image": { "url"? , "b64_json"? , "mime"? } }`
- **split-layers**：`{ "layers": [ { "url"? , "b64_json"? , "name"? , "bbox"? } ] }`
- **video-gen**：`{ "videos": [ { "url"? , "b64_json"? , "mime"? } ] }`，或单视频 `{ "video": { "url"? } }` / 顶层 `{ "url" }`。
  BFF 落盘时 `kind="video"`，`result` 改写后产物 `url` 指向 `/api/me/media/{key}`、附 `_bffMediaKey`。

> 产物优先返回 **base64（`b64_json`）** —— 前端直接解码使用，无跨域/CORS/鉴权问题（推荐）。
> 若返回 **url**，需网关给出可公开访问的地址（前端 `fetch(url, {credentials:'omit'})` 直取）。

### 3.6 归属与隔离
- 网关必须按请求头 `New-Api-User: <uid>` 把任务归属到该用户，并**仅允许查/取消本人任务**
  （越权读他人 task_id 返回 404）。这是 BFF 不做本地 owner 校验的安全兜底。

### 3.7 同步端点响应（sync mode）
- `POST {GATEWAY_SYNC_*_PATH}`：BFF 原样转发 `params`，网关**同步返回结果**（HTTP 200，不包装 task）。
- 响应体即 §3.5 的 `result` 形状（BFF 经 `_normalize_sync_result` 归一化后落盘）：
  - **image-gen**：`{ "data": [ { "url"? , "b64_json"? } ] }`（OpenAI 图片风）
  - **upscale / remove-background**：`{ "image": { "url"? , "b64_json"? } }`
  - **split-layers**：`{ "layers": [ { "url"? , "b64_json"? , "name"? , "bbox"? } ] }`
- 若网关响应顶层带 `request_id` / `id`，BFF 记入 `gateway_request_id`（对齐网关 `/api/log` 全站日志）。
- 同步调用可能较长（如超分/去底），网关应在 `GATEWAY_SYNC_TIMEOUT`（默认 300s）内返回。

---

## 4. 前端调用点（已落地 `imageTask.ts`）
| 调用点 | 现状 |
|--------|------|
| `generateImageWithProvider`（文生/图生） | `submitImageTask('image-gen', params)` → `pollImageTask` → `fetchFirstMedia` ✅（默认 async） |
| `runImageAgentWithProvider('upscale')` | `submitImageTask('upscale', ...)` → 走 sync 分支 ✅ |
| `runImageAgentWithProvider('remove-background')` | `submitImageTask('remove-background', ...)` → 走 sync 分支 ✅ |
| `splitImageLayersWithProvider` | `submitImageTask('split-layers', ...)` → 走 sync 分支 ✅ |

> 前端注意：轮询键用提交响应里的 **`id`**（request_id），不要再用旧 `task_id`；`status`/`result`
> 从统一视图读取（sync 提交即返回 `status=succeeded` + `result`，可跳过轮询）。

---

## 5. 与原架构的差异（备忘）
| 维度 | 旧（已废弃） | 新（本文件） |
|------|------|------|
| 异步执行 | BFF worker 池调网关同步接口并等待 | 网关自身异步，BFF 仅转发 |
| 任务状态 | BFF Redis/内存 store | 网关持有（async）/ 请求日志回写（sync），BFF 轻状态 |
| 模式 | 仅异步 | **同步 + 异步双模式**（TASK_TYPES 按 type 指定 mode） |
| 产物落地 | 网关返回 url/base64，BFF `get_task` 成功时落 **OSS（元数据入 PG `cloud_media`）** 并改写 `result` | 同左；sync 直出后同样落 OSS/PG |
| 请求结构落库 | 无 | **每次提交写 `cloud_request_log`（request_id + payload + task_id + gateway_request_id）**，供拉日志对齐 |
| 并发/背压 | BFF `Semaphore` + 队列 429 | 网关侧负责（async）；sync 走独立长超时 client |
| 依赖 | `redis` | 无（已从 requirements 移除） |
| 配置 | `REDIS_URL`/`TASK_*`/`GATEWAY_ASYNC` | `GATEWAY_IMAGE_TASKS_PATH`/`GATEWAY_PROXY_TIMEOUT` + `GATEWAY_SYNC_*_PATH`/`GATEWAY_SYNC_TIMEOUT`/`GATEWAY_IMAGE_GEN_MODE` |
