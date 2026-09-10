# Flovart 网关端点入参 / 返回契约

> 适用范围：Flovart BFF（`flovart-bff`）调用 **new-api 网关** 的 `/images/*` 与视频端点，以及聊天通道 `/api/chat/completions`。
> 本文档是给**网关团队**的对接契约：每个端点的入参字段、返回结构、以及 BFF 如何归一化。
> 配套文档：`image-gateway-endpoints.md`（能力→端点清单）、`gateway-mapping-design.md`（capability→模型映射设计）。

---

## 一、通用约定（所有端点适用）

### 1.1 调用链与鉴权
```
前端 → BFF POST /api/tasks (body: {type, params}) 或 POST /api/chat/completions
     → BFF 注入头后转发 new-api 网关：
         Authorization: Bearer <管理员 PAT>
         New-Api-User: <登录用户 uid>      # 计费落到该用户配额
       POST /images/{capability}
```
- **网关无需从 body 读任何 key**：鉴权与计费由 BFF 注入的头完成。
- BFF 把前端 `params` **原样 JSON 透传**给网关（不增删字段，除 `_gateway` 旁路见 1.4）。

### 1.2 同步 vs 异步
| 模式 | 网关行为 | BFF 处理 |
|---|---|---|
| **sync**（upscale / remove-background / outpaint / mask / annotate / relight / edit） | `POST /images/{cap}` 直接返回图片 JSON | BFF 阻塞等待，落盘后回传前端 |
| **async**（image-gen 当 `GATEWAY_IMAGE_GEN_MODE=async`、video-gen） | `POST /images/{cap}` 返回 `task_id`；`GET /images/{cap}/{task_id}` 轮询 | BFF 立即返 processing，前端轮询 |

### 1.3 返回归一化规则（BFF 认哪些字段）
BFF `_normalize_sync_result` / `_iter_outputs` 兼容以下返回形态（网关任选其一）：

| 形态 | 示例 | 说明 |
|---|---|---|
| OpenAI 风数组 | `{ "data": [ { "url": "..." } \| { "b64_json": "..." } ] }` | 自动转成 `{images:[...]}` |
| 单图对象 | `{ "image": { "url": "..." \| "b64_json": "..." } }` | |
| 顶层直出 | `{ "url": "..." }` / `{ "b64_json": "..." }` / `{ "base64": "..." }` | |
| 分层（split-layers 用） | `{ "layers": [ {name,bbox,url\|b64_json,...} ], "image": {...} }` | |

**产物对象字段识别**（任一即可被落盘）：`url` / `href` / `image_url` / `b64_json` / `base64` / `dataUrl` / `data_url`。
**结果容器 key**：顶层 / `data` / `image` / `images` / `layers` / `video` / `videos` / `outputs`（string[] url）。

> ⚠️ 推荐网关统一返回 `{ "data": [ { "b64_json": "..." } ] }` 或 `{ "url": "..." }`，最稳妥。

### 1.4 `_gateway` 旁路（可选，一般网关侧忽略）
当用户 AI 服务同时配了 `baseUrl + key` 时，BFF 走**外部直连**（不经 new-api admin PAT），params 会带 `_gateway: { base_url, api_key }`。新版 new-api 网关模式不带此字段；若带，网关应忽略 `_gateway` 字段（BFF 外部直连时才会解析它）。

### 1.5 错误码
网关返回非 2xx → BFF 包装为 `NewApiError`，原样透传前端：`{ "success": false, "message": "<网关错误信息>" }`（HTTP 同网关状态码）。

---

## 二、同步图像端点（POST /images/{capability}）

### 2.1 `images/generations` —— 文生图 / 图生图
- **BFF env**：`GATEWAY_IMAGE_TASKS_PATH`（async 默认 `images/tasks`）/ `GATEWAY_SYNC_IMAGE_PATH`（sync 默认 `images/generations`）
- **前端 type**：`image-gen`
- **入参 params**：
  | 字段 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | `model` | string | 否 | 网关映射的模型名，可忽略 |
  | `prompt` | string | ✅ | 文生/图生提示词 |
  | `image` | string[] | 图生时必填 | 参考图 **dataURL 字符串数组**（完整 `data:image/png;base64,...`）；文生图不带 |
  | `size` | string | 否 | 如 `"1024x1024"`（OpenAI 风，来自 `buildOpenAIImageRequestParams`） |
  | `quality` | string | 否 | 来自 AI 服务 extraConfig.imageQuality |
  | `background` | string | 否 | extraConfig.imageBackground |
  | `moderation` | string | 否 | extraConfig.moderation |
  | `output_format` | string | 否 | extraConfig.outputFormat |
  | `output_compression` | number | 否 | extraConfig.outputCompression |
  | `mode` | string | 否 | `"sync"` / `"async"`，来自 `key.imageGenMode` |
- **反参**：`{ "data": [ { "b64_json": "..." \| "url": "..." } ] }` 或单图形态。
- **模型**：GPT Image 2（文生/图生同一端点）。

### 2.2 `images/upscale` —— 高清放大（⚠️ 必须独立超分模型）
- **BFF env**：`GATEWAY_SYNC_UPSCALE_PATH`（默认 `images/upscale`）
- **前端 type**：`upscale`
- **入参 params**：
  | 字段 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | `model` | string | 否 | |
  | `task` | string | ✅ | 固定 `"upscale"`（网关据此识别能力） |
  | `image` | object | ✅ | **`{ data: <裸base64>, mimeType: <string> }`**（见 2.9 编码约定） |
  | `options` | object | ✅ | `{ "targetLongEdge": number, "algorithm": "high"\|"bilinear"\|"nearest" }` |
  | `mode` | string | 否 | |
- **反参**：单图 `{ "image": { "b64_json": "..." } }` 或 `{ "url": "..." }`。
- **模型**：**Stability ESRGAN / Replicate Real-ESRGAN 等超分模型**；**不可接 GPT Image 2**（只能重绘放大、不保真）。

### 2.3 `images/remove-bg` —— 去除背景
- **BFF env**：`GATEWAY_SYNC_REMOVE_BG_PATH`（默认 `images/remove-bg`）
- **前端 type**：`remove-background`
- **入参 params**：
  | 字段 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | `model` | string | 否 | |
  | `task` | string | ✅ | 固定 `"remove-background"` |
  | `image` | object | ✅ | `{ data, mimeType }` |
  | `options` | object | ✅ | 固定 `{}`（空） |
- **反参**：返回**透明 PNG** 的 `{ "image": { "b64_json": "..." } }` 或 `{ "url": "..." }`。
- **模型**：GPT Image 2 edit（prompt 去背）或 remove.bg / Photoroom（像素级更准）。

### 2.4 `images/outpaint` —— 扩展画面（扩图）
- **BFF env**：`GATEWAY_SYNC_OUTPAINT_PATH`（默认 `images/outpaint`）
- **前端 type**：`outpaint`
- **入参 params**（来自 `editImageWithProvider` 平台分支）：
  | 字段 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | `model` | string | 否 | |
  | `image` | object | ✅ | `{ data, mimeType }` 源图 |
  | `prompt` | string | ✅ | 如 `"向左侧扩展画面。"` + 用户补充 |
  | `variant` | string | ✅ | 固定 `"outpaint"` |
  | `mask` | object | 否 | 一般不带（扩图由网关按 prompt 决定方向） |
- **反参**：扩图后单图 `{ "image": { "b64_json": "..." } }` 或 `{ "url": "..." }`。
- **模型**：GPT Image 2 edit（原生强项）。

### 2.5 `images/mask` —— 编辑蒙版（局部重绘 inpaint）
- **BFF env**：`GATEWAY_SYNC_MASK_PATH`（默认 `images/mask`）
- **前端 type**：`mask`
- **入参 params**：
  | 字段 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | `model` | string | 否 | |
  | `image` | object | ✅ | `{ data, mimeType }` 源图 |
  | `prompt` | string | ✅ | 用户局部重绘指令 |
  | `variant` | string | ✅ | 固定 `"mask"` |
  | `mask` | object | ✅ | `{ data, mimeType }` 蒙版图（白色/不透明=待重绘区域） |
- **反参**：重绘后单图 `{ "image": { "b64_json": "..." } }` 或 `{ "url": "..." }`。
- **模型**：GPT Image 2 edit（原生强项）。

### 2.6 `images/annotate` —— 标注涂鸦（参考图编辑）
- **BFF env**：`GATEWAY_SYNC_ANNOTATE_PATH`（默认 `images/annotate`）
- **前端 type**：`annotate`
- **入参 params**：
  | 字段 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | `model` | string | 否 | |
  | `image` | object | ✅ | `{ data, mimeType }` 源图 |
  | `prompt` | string | ✅ | 固定 `"根据标注修改图片"` |
  | `variant` | string | ✅ | 固定 `"annotate"` |
  | `mask` | object | ✅ | **涂鸦参考图** `{ data, mimeType }`（前端把用户涂鸦当 mask 字段传，网关视作参考引导图） |
- **反参**：编辑后单图 `{ "image": { "b64_json": "..." } }` 或 `{ "url": "..." }`。
- **模型**：GPT Image 2 edit（参考图+prompt 引导，半支持级）。

### 2.7 `images/relight` —— 打光（重打光）
- **BFF env**：`GATEWAY_SYNC_RELIGHT_PATH`（默认 `images/relight`）
- **前端 type**：`relight`
- **入参 params**：
  | 字段 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | `model` | string | 否 | |
  | `image` | object | ✅ | `{ data, mimeType }` 源图 |
  | `prompt` | string | ✅ | 由 `LIGHTING_PRESETS` 拼的光照指令（如 `"Relight to golden hour, soft warm light, intensity 0.6"`） |
  | `variant` | string | ✅ | 固定 `"relight"` |
  | `mask` | object | 否 | 不带 |
- **反参**：打光后单图 `{ "image": { "b64_json": "..." } }` 或 `{ "url": "..." }`。
- **模型**：GPT Image 2 edit（prompt 重绘光照，半支持级）。

### 2.8 `images/edits` —— 通用编辑 / 换装
- **BFF env**：`GATEWAY_SYNC_EDIT_PATH`（默认 `images/edits`）
- **前端 type**：`edit`（来自表格类「换装」等 `executeWardrobe`）
- **入参 params**：
  | 字段 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | `model` | string | 否 | |
  | `image` | object | ✅ | `{ data, mimeType }` 源图 |
  | `prompt` | string | ✅ | 换装/通用编辑指令 |
  | `variant` | string | ✅ | 固定 `"edit"` |
  | `mask` | object | 否 | 不带 |
- **反参**：编辑后单图 `{ "image": { "b64_json": "..." } }` 或 `{ "url": "..." }`。
- **模型**：GPT Image 2 edit。

### 2.9 ⚠️ `image` 字段两种编码风格（网关必须分别兼容）
前端两套调用链编码不同，**网关侧不可假设统一格式**：

| 端点 | `image` 字段格式 |
|---|---|
| `images/generations`（图生图） | `image: string[]` —— **完整 dataURL 数组**（`["data:image/png;base64,iVBOR..."]`） |
| `images/upscale` / `remove-bg` / `outpaint` / `mask` / `annotate` / `relight` / `edit` | `image: { data: "<裸base64,不含data:前缀>", mimeType: "image/png" }` |
| 上述端点可能带的 `mask` | 同上为 `{ data, mimeType }` 对象 |

建议网关对 `image` 做容错：既接受 `string`（dataURL）/ `string[]`，也接受 `{ data, mimeType }` 对象，统一解成「字节 + mime」。

---

## 三、异步端点（POST 提交 + GET 轮询）

### 3.1 `images/video-tasks`（视频生成）
- **BFF env**：`GATEWAY_VIDEO_TASKS_PATH`（默认 `images/video-tasks`）
- **前端 type**：`video-gen`
- **提交入参 params**：
  | 字段 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | `model` | string | 否 | |
  | `task` | string | ✅ | 如 `"video-generation"` |
  | `image` | object | 图生视频时 | `{ data, mimeType }` 首帧/参考图 |
  | `options` | object | ✅ | 视频参数（duration / resolution / fps / motion 等，前端透传） |
- **提交反参**：`{ "task_id": "<第三方/网关任务id>" }`（BFF 转成 processing 视图）。
- **轮询**：`GET /images/video-tasks/{task_id}` → 返回 `{ "status": "succeeded", "result": { "video": { "url": "..." } } }` 或 `{ "data": [ {url} ] }`。
- **模型**：视频生成模型（如 Seedance / Veo）。

### 3.2 `image-gen` 异步模式
当 `GATEWAY_IMAGE_GEN_MODE=async` 时，`images/generations` 走异步：提交反参 `{ "task_id" }`，轮询 `GET /images/{cap}/{task_id}`（路径由 `GATEWAY_IMAGE_TASKS_PATH` 决定），入参同 2.1。

---

## 四、聊天通道（反推 Prompt）

### 4.1 `POST /api/chat/completions`（BFF 端点，转发 new-api `/v1`）
- **前端 type**：`reverse`（反推 Prompt 按钮）
- **入参 body**（前端 `fetchReversePromptViaBff` 直发 BFF，BFF 自动选 `CHAT_VISION_MODEL` 转 new-api）：
  ```json
  {
    "messages": [
      { "role": "system", "content": "<反推 prompt 指令（中英）>" },
      { "role": "user", "content": [
          { "type": "text", "text": "Image dimensions: 1024 x 768." },
          { "type": "image_url", "image_url": { "url": "<dataURL 或 https>" } }
      ] }
    ],
    "stream": true
  }
  ```
  - 前端**不传 model**（BFF 用 `CHAT_VISION_MODEL` 兜底）。
- **反参**：OpenAI SSE 流，逐行 `data: { "choices": [ { "delta": { "content": "<片段>" } } ] }`，结束 `data: [DONE]`。
- **模型**：`CHAT_VISION_MODEL`（gemini-2.5-flash / gpt-4o 等视觉 LLM）。

---

## 五、第三方直连（非 new-api 网关，标注清楚）

以下两项**不走 new-api 网关**，由 BFF 直连 WaveSpeed（需 `WAVESPEED_API_KEY`）。列入仅为完整性：

| 前端 type | 路径 | 入参要点 | 模型 |
|---|---|---|---|
| `split-layers` | WaveSpeed `/v1/layer-decomposition` | `model`、`task:"layer-segmentation"`、`image:{data,mimeType}`、`num_layers` | **Seedream v5.0 Pro Layer Decomposition**（推荐，base + ≤16 透明层，每层 name/bbox/z_index） |
| `multi-angle` | WaveSpeed `/v1/multi-angle` | `source_media_key`、`rotate`、`tilt`、`scale`、`prompt`、`num_images` | FLUX Kontext Max Multi（保主体一致性多角度） |

返回：split-layers 用 `{ layers: [...] , image: {...} }`；multi-angle 用 `{ images: [...] }`。

---

## 六、网关团队落地清单

1. [ ] 实现 `images/generations`（已接 image-2，验证文生/图生可用）
2. [ ] 实现 `images/outpaint` / `images/mask` / `images/edits` / `images/annotate` / `images/relight` → 全部映射 image-2 `/images/generations`，按 `params.variant` 或 URL path 区分拼参
3. [ ] 实现 `images/remove-bg` → image-2 edit 或 remove.bg
4. [ ] 实现 `images/upscale` → **独立超分模型**（不可 image-2）
5. [ ] 实现 `images/video-tasks`（异步）→ 视频模型
6. [ ] 统一返回格式为 `{ data:[{b64_json|url}] }` 或 `{ url }`
7. [ ] 容错 `image` 两种编码（dataURL 数组 vs `{data,mimeType}` 对象）
8. [ ] 联调：用管理员账号登录 Flovart，逐个点按钮验证 200 + 返回图片
