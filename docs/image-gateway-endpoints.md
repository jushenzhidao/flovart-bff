# Flovart 图片能力 → 网关端点 / 模型对接清单

> 用途：给网关团队对接参考。前端已全改平台模式（用户免 Key，走 BFF → new-api 网关），
> 计费经 `request_as_user` 落到登录用户自己的 new-api 配额。
> BFF 按前端 `type` 把 `params` 原样透传到对应网关端点，网关侧按本表把端点接到具体模型即可。

## 一、网关图像端点（BFF /api/tasks → new-api 网关）

前端 `type` → 网关端点 → 推荐模型（按你给的格式）：

```
images/generations   → image-2（GPT Image 2）文生图/图生图          # type: image-gen
images/upscale       → 超分模型（Stability ESRGAN / Replicate Real-ESRGAN）  # type: upscale（⚠️ image-2 不能真超分）
images/remove-bg     → image-2（复用 images/generations，prompt 去背）或 remove.bg/Photoroom  # type: remove-background
images/outpaint      → image-2（复用 images/generations，带 image+mask）  # type: outpaint
images/mask          → image-2（复用 images/generations，带 image+mask+prompt）  # type: mask
images/annotate      → image-2（复用 images/generations，带 image+涂鸦参考+prompt）  # type: annotate
images/relight       → image-2（复用 images/generations，带 image+光照 prompt）  # type: relight
images/edits         → image-2（复用 images/generations，带 image+prompt）  # type: edit
GATEWAY_VIDEO_TASKS_PATH → 视频生成模型（文生视频/图生视频）         # type: video-gen
```

### 对应 BFF `.env` 配置（默认值，一般无需改，仅需网关侧端点就绪）

| 环境变量 | 默认端点 | 前端 type |
|---|---|---|
| `GATEWAY_SYNC_IMAGE_PATH` | `images/generations` | image-gen（sync 分支） |
| `GATEWAY_IMAGE_TASKS_PATH` | `images/generations` | image-gen（async 分支） |
| `GATEWAY_SYNC_UPSCALE_PATH` | `images/upscale` | upscale |
| `GATEWAY_SYNC_REMOVE_BG_PATH` | `images/remove-bg` | remove-background |
| `GATEWAY_SYNC_OUTPAINT_PATH` | `images/outpaint` | outpaint |
| `GATEWAY_SYNC_MASK_PATH` | `images/mask` | mask |
| `GATEWAY_SYNC_ANNOTATE_PATH` | `images/annotate` | annotate |
| `GATEWAY_SYNC_RELIGHT_PATH` | `images/relight` | relight |
| `GATEWAY_SYNC_EDIT_PATH` | `images/edits` | edit |
| `GATEWAY_VIDEO_TASKS_PATH` | （视频 tasks 端点） | video-gen |

### 参数要点（BFF 透传给网关的 `params` 形状）

| type | 关键入参 | 说明 |
|---|---|---|
| image-gen | `prompt`, `size`, `n`, 可选图像参考 | 文生图/图生图 |
| upscale | `image`, `scale` / `width` / `height` | 超分放大 |
| remove-background | `image` | 去背，建议返回透明 PNG |
| outpaint | `image`, `mask`（边缘透明区）, `size` | 扩图 |
| mask | `image`, `mask`（透明 PNG 指定区域）, `prompt` | 局部重绘 |
| annotate | `image`, 涂鸦参考图, `prompt` | 涂鸦引导 |
| relight | `image`, `prompt`（光照描述） | 重打光 |
| edit | `image`, `prompt` | 通用编辑（换装等） |
| video-gen | `prompt`, 可选 `image`, `duration` | 视频 |

## 二、第三方直连（BFF 直连，不走 new-api 网关，需 `WAVESPEED_API_KEY`）

```
WaveSpeed split-layers → Seedream v5.0 Pro Layer Decomposition（推荐，返回 base + ≤16 透明层）  # type: split-layers
WaveSpeed multi-angle  → FLUX Kontext Max Multi（保主体一致性多角度）                            # type: multi-angle
```

## 三、聊天通道（BFF /api/chat/completions，用户免 Key，计费走登录用户 sk-）

```
/api/chat/completions → CHAT_VISION_MODEL（vision LLM，如 gemini-2.5-flash / gpt-4o）  # 反推 Prompt（图生 prompt，SSE 流式）
```

## 四、纯前端能力（不走模型 / 不调端点，纯 Canvas/状态）

裁剪、旋转（±90/180/翻转）、缩放、滤镜、画笔、橡皮擦、套索、文字、形状、
宫格切分、分镜拼接、切换自由缩放、预览、下载、复制、撤销/重做、全屏。

---

## 现状小结
- 管理员在 new-api 接入 image-2 渠道 + 在网关把上述 8 个图像端点（generations 已接，其余 7 个待接）内部复用 `images/generations` 接口、按能力拼装参数，
  即可让 image-gen/upscale/remove-bg/outpaint/mask/annotate/relight/edit 全部免 Key 可用。
- `upscale` 必须接专用超分模型（image-2 只能"重绘放大"非保真）。
- `split-layers` / `multi-angle` 走 WaveSpeed 直连，需 `WAVESPEED_API_KEY`。
- `video-gen` 端点需网关侧视频模型就绪。
- 反推 Prompt 走聊天通道，需 `CHAT_VISION_MODEL` 指向可用视觉模型。
