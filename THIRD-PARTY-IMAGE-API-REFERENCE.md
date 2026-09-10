# 第三方图片处理 API 接入参考（upscale / matting / split-layers）

> 用途：给**网关侧（new-api 后端渠道）**建 `upscale` / `matting` / `split-layers` 三个能力时，照着选型 + 响应格式对接。
> 读者：飞哥（建网关渠道）+ BFF 维护者（核对解析形状）。
> 关联文档：`IMAGE-ASYNC-TASKS-CONTRACT.md`（BFF 异步任务框架接口契约）。

---

## 0. 边界与前提（务必先读）

1. **计费归网关，BFF 不扣费**。BFF 只做两件事：
   - 调网关跑任务（成功产物存云端 media）
   - 展示「剩余积分」+「消费记录」（从 new-api 用户 quota / 调用日志拉，经 `config.quota_to_points` 换算）
   - 因此本文**不讨论扣费逻辑**，只讨论「怎么把图片处理跑通并归一化返回」。

2. **异步化位置（2026-09-07 更新）**：前端 ↔ BFF、BFF ↔ 网关**两端都是异步**——BFF 只把
   `type+params` 透传给网关的异步接口（`GATEWAY_IMAGE_TASKS_PATH`，默认 `contents/generations/tasks`），
   网关自身持有任务状态机与产物，前端提交后轮询 `GET /api/tasks/{id}` 取 `result`。旧版「BFF 同步长超时
   调网关」与 `GATEWAY_ASYNC` 开关已废弃，BFF 不再做任何同步等待。本文三类能力的 `result` 归一化形状
   见 IMAGE-ASYNC-TASKS-CONTRACT.md §3.5（image / images / layers，url 或 base64）。

3. **可信度标注**：文中标 `[公开]` = 第三方官方公开 API 格式（高可信）；标 `[建议]` = 我们建议网关做的一层归一化（按 BFF 现有解析定制）；标 `[推断]` = 基于 Lovart 能力平替的选型建议，具体字段以你实际接的渠道为准。

---

## 1. Lovart 是怎么接的（为什么复制不了，但能力可平替）

爬 Lovart 公开资料结论：

| 能力 | Lovart 实际做法 | 是否可复制 |
|---|---|---|
| 放大 upscale | 自研 **Nano Banana 系列 upscaler**（基于 Gemini 2.5 Flash Image），「理解内容后补高频细节」而非插值 | 不可复制（闭源），但有对等第三方 |
| 抠图 / 分层（Edit Elements） | 自研 **MCoT 引擎 + Nano Banana Pro**，「一键炸开」成背景/前景/文字独立层，被挡区 inpainting 补全 | 不可复制（独家黑科技），但 Seedream v5.0 Pro Layer Decomposition 是平替 |
| 文生图 / 视频 | 接第三方：Seedream（版式密集）、Seedance / Veo3 / Kling（视频） | 已接 |

**结论**：Lovart 的核心黑科技不开放 API，但三类能力都有对等第三方可平替。下面按能力给选型。

---

## 2. 三类能力选型总览

| 能力 | 推荐第三方 | 同步/异步 | 网关端点 | 归一化后 BFF 解析 |
|---|---|---|---|---|
| 放大 upscale | **Stability Upscale**（简单，直返 b64） | 同步 | `/agent` | `image.imageBase64` |
| 放大 upscale（高质量） | Replicate `google/upscaler` / Real-ESRGAN（加 `Prefer: wait=300` 变同步） | 异步→同步化 | `/agent` | `image.imageBase64` |
| 抠图 matting | **remove.bg**（行业标准） | 同步 | `/agent` | `image.imageBase64` |
| 分层 split-layers | **Seedream v5.0 Pro Layer Decomposition**（⭐ 你网关已接 Seedream，免另找渠道） | 同步 | `/split-layers` | `layers[].imageBase64` + `bbox` + `name` + `z_index` |

> 分层优先用 Seedream v5.0 Pro Layer Decomposition：单图 → base + ≤16 透明 PNG 层，每层带 `name` / `z_index` / `bbox`，被挡背景自动 inpaint 补全。这与 Lovart「Edit Elements」最接近，且复用你已有 Seedream 渠道。

---

## 3. 放大 upscale

### 3.1 候选 A：Stability Upscale `[公开]`
- 请求：`POST {stability}/v2beta/stable-image/upscale/enhance`
  - Header：`Authorization: Bearer <key>`，`Accept: application/json`
  - Body（multipart）：`image=<文件或base64>`、`prompt=<可选描述>`、`creativity=0.3`、`output_format=png`
- 响应（JSON）：
  ```json
  { "images": [ "<base64 字符串>", "..." ] }
  ```
- 特点：直接返 base64，解析最简单。

### 3.2 候选 B：Replicate（高质量）`[公开]`
- 提交：`POST https://api.replicate.com/v1/predictions`
  - Header：`Authorization: Bearer <key>`，`Prefer: wait=300`（让网关同步等到结果再返回，免去轮询）
  - Body：`{ "version": "<模型版本>", "input": { "image": "<url 或 base64>", "upscale_factor": "x4" } }`
- 响应（同步等到后）：`{ "status": "succeeded", "output": "<结果 url 或 base64>" }`
- 特点：质量高，但需网关支持 `Prefer: wait` 或自己做轮询中转。

### 3.3 网关归一化契约 `[建议]`
无论选 A 还是 B，网关 `/agent` 统一回：
```json
{
  "image": {
    "imageBase64": "<放大后 base64>",
    "mimeType": "image/png",
    "width": 2048,
    "height": 1536
  }
}
```
BFF `tasks.py` 的 `exec_agent` → `_agent_result` + `_pick_b64` **已认 `image.imageBase64`**，一行不改。

---

## 4. 抠图 matting

### 4.1 候选：remove.bg `[公开]`
- 请求：`POST https://api.remove.bg/v1.0/removebg`
  - Header：`X-API-Key: <key>`，`Content-Type: application/json`
  - Body：`{ "image_file_b64": "<原图 base64>", "size": "auto", "format": "png" }`
  - **或** 让网关走 JSON 响应（避免默认吐二进制 PNG）：加 `Accept: application/json` 后返：
    ```json
    {
      "data": {
        "result_b64": "<抠图后透明 PNG base64>",
        "foreground_top": 0, "foreground_left": 0,
        "foreground_width": 1024, "foreground_height": 768
      }
    }
    ```
- 特点：行业标准，质量稳。默认返二进制 PNG，建议网关转 JSON（`result_b64`）方便归一化。

### 4.2 备选：Photoroom / Pixelcut `[公开]`
- 请求：`POST {host}/v1/segment`，Body `{ "image_url": "<url>", "scale": "100%" }`
- 响应：`{ "status": "success", "data": { "url": "<抠图 url>" } }`（返 url，网关需下载转 b64）

### 4.3 网关归一化契约 `[建议]`
网关 `/agent` 统一回（与 upscale 同形状，BFF 同一解析路径）：
```json
{
  "image": {
    "imageBase64": "<抠图后透明 PNG base64>",
    "mimeType": "image/png",
    "width": 1024,
    "height": 768
  }
}
```

---

## 5. 分层 split-layers

### 5.1 候选 A（⭐ 推荐）：Seedream v5.0 Pro Layer Decomposition `[推断]`
- 前提：你网关已接 Seedream 渠道，可复用。
- 能力：单图 → 1 张 base（背景，被挡区 inpaint 补全）+ ≤16 张透明 PNG 层。
- 每层附：`name`（主体/文字/装饰等）、`z_index`（层级，越大越靠前）、`bbox`（{x,y,width,height} 在原图坐标）。
- 响应 `[推断，需你实测]`：
  ```json
  {
    "base": { "imageBase64": "<补全后背景 b64>", "mimeType": "image/png" },
    "layers": [
      { "name": "主体", "imageBase64": "<层 b64>", "mimeType": "image/png",
        "bbox": { "x": 120, "y": 80, "width": 400, "height": 360 }, "z_index": 2 },
      { "name": "文字", "imageBase64": "<层 b64>", "mimeType": "image/png",
        "bbox": { "x": 200, "y": 500, "width": 300, "height": 60 }, "z_index": 3 }
    ]
  }
  ```

### 5.2 候选 B：Qwen-Image Layered（WaveSpeedAI，阿里）`[公开]`
- 异步：`POST {host}/v1/predictions`，Body `{ "input": { "image": "<url>", "num_layers": 4 } }`
- 轮询：`GET /v1/predictions/{id}` → `data.outputs`（层图列表）

### 5.3 候选 C：Codia Image Layering `[公开]`
- 响应特殊：返 **JSON DSL 层树**（非原图像素，是描述 + url）：
  ```json
  { "code": 0, "data": { "type": "image_layer", "dsl": "{\"layers\":[...]}" } }
  ```
- 注意：返回的是可编辑图层描述，不是透明 PNG，前端需另做渲染。

### 5.4 网关归一化契约 `[建议]`
**优先按 5.1 形状**回（BFF `exec_split_layers` → `_layers_of` + `_pick_b64` 已认 `layers` 键、`imageBase64`、`bbox`）：
```json
{
  "layers": [
    { "name": "<层名>", "imageBase64": "<层 b64>", "mimeType": "image/png",
      "bbox": { "x": 0, "y": 0, "width": 1024, "height": 768 }, "z_index": 1 }
  ],
  "base": { "imageBase64": "<被补全背景 b64>", "mimeType": "image/png" }
}
```
> 若第三方真实返回的是 **URL 而非 b64**（如 Qwen / Codia），网关侧下载转 b64 最省事，BFF 不用改。

---

## 6. BFF 现有解析对应关系（网关按 §3.3/§4.3/§5.4 回，以下代码零改动）

| 能力 | BFF 入口 | 解析函数 | 认的字段 |
|---|---|---|---|
| upscale / matting | `exec_agent` | `_agent_result` → `_pick_b64` | `image.imageBase64`；回退 `result.imageBase64` / `data.imageBase64` / `imageBase64` |
| split-layers | `exec_split_layers` | `_layers_of` → `_pick_b64` | 顶层 `layers[]`；回退 `result.layers` / `data.layers`；每层 `imageBase64` + `bbox{x,y,width,height}` + `name` + `z_index` |

> BFF 解析已带多键回退（容错），但**网关按本文契约回最稳**。若第三方真实字段与本文不符，先改网关归一化层，别动 BFF。

---

## 7. 网关侧落地清单

- [ ] 接 **upscale** 渠道（Stability 或 Replicate+`Prefer: wait`），`/agent` 归一化回 §3.3
- [ ] 接 **matting** 渠道（remove.bg），`/agent` 归一化回 §4.3（走 JSON 响应拿 `result_b64`）
- [ ] 接 **split-layers** 渠道（Seedream v5.0 Pro Layer Decomposition），`/split-layers` 归一化回 §5.4
- [ ] 三个端点保证 **单次 ≤ 300s** 返回（否则 BFF `gw_client` 超时 → 任务 failed）
- [ ] 网关出口别让 BFF 单 IP 打满连接池 / 触发 IP 限流（BFF 已转发 `X-Forwarded-For`，需 new-api 信任代理）
- [ ] 计费在网关侧完成（BFF 不扣），仅暴露剩余积分 / 消费记录查询给 BFF

> 端点通后，把前端 `aiGateway.runImageAgentWithProvider` / `splitImageLayersWithProvider` 切到 `submitImageTask` 即可端到端跑通（BFF handler 已实现，前端调用点待接）。

---

## 8. 待实测核对（飞哥接渠道时确认）

- [ ] Seedream v5.0 Pro Layer Decomposition **真实返回字段名 / 层级**（§5.1 标 `[推断]`）
- [ ] remove.bg JSON 响应开关（`Accept: application/json` 是否生效、字段是否 `result_b64`）
- [ ] Replicate `Prefer: wait=300` 在网关环境是否可用（不行则走轮询中转）
- [ ] 三类真实产物尺寸 / 透明通道是否正确（影响前端 PS 导出 `convert.py` 的图层名与 bbox 对齐）
