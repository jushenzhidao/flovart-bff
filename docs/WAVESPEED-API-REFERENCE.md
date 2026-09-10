# WaveSpeed AI API 能力参考（flovart-bff 直连第三方）

> 用途：本 key 已接入 flovart-bff，用于**绕过 new-api 网关、直连 WaveSpeed** 跑网关未接入的能力
> （首期 = 多角度 `multi-angle`）。本文给飞哥一份「这把 key 能干什么」的能力地图 + 接入要点。
> 关联：`docs/multi-angle-spec.md`（多角度方案）、`app/thirdparty/wavespeed.py`（适配器实现）。
> ⚠️ 定价/模型清单来自 wavespeed.ai 官方（2026-09-07 抓取），随官方调整，最终以控制台账单为准。

---

## 0. 一句话结论

WaveSpeed 是一个**聚合型推理平台**（自称 600–700+ 模型，一个 key 通全部），覆盖
**文生图 / 图生图·多图一致性 / 抠图·人脸增强 / 文生视频·图生视频 / LLM**。
定价**按量付费、无订阅**；注册送 $1 额度；无冷启动、99.9% SLA。
对 flovart 的价值：**多角度、3D/视频特效等网关缺的能力可以直连补上**，且同一把 key 还能平替
我们现有的 upscale / matting / split-layers（见 §6 待办）。

---

## 1. 鉴权与调用形态（务必先读）

```
鉴权：Authorization: Bearer $WAVESPEED_API_KEY        # 仅 env 注入，绝不进代码/前端
提交：POST  https://api.wavespeed.ai/api/v3/{developer}/{model}
轮询：GET   https://api.wavespeed.ai/api/v3/predictions/{prediction_id}/result
```

- **异步任务制**（与现有网关 tasks 框架一致）：提交返回 `prediction id`，轮询 `result` 端点，
  `data.status` ∈ `created | processing | completed | failed | cancelled | timeout`；
  完成读 `data.outputs`（图片/视频 URL 数组）。
- 提交响应里 prediction id 通常在 `data.id`（个别模型 `data.prediction_id` / `data.task_id`，
  适配器已做多键容错）。
- **两个实用开关**（部分模型支持，API 专属）：
  - `enable_sync_mode: true` → 服务端等到结果再返回，**免轮询**（适合短任务）。
  - `enable_base64_output: true` → 输出直接返 base64，**省一次下载回程**（适配器当前走 URL 再落 BFF 盘）。
- **图片输入要求**：`images` / `image` 字段接受**公网可访问 URL**（我们走 OSS presigned URL）；
  本地文件需先上传换 URL（适配器 `_upload_local` 已留接口，prod 走 OSS 不触发）。

---

## 2. 能力地图（这把 key 能干什么）

### 2.1 文生图（T2I）
| 模型路径 | 价格/张 | 特点 | flovart 适配 |
| --- | --- | --- | --- |
| `wavespeed-ai/flux-2-pro/text-to-image` | $0.03 | 旗舰写实、电影感、复杂场景 | 通用生图平替 |
| `wavespeed-ai/flux-2-dev/text-to-image` | 低 | 轻量、适合 LoRA/批量 | 批量草稿 |
| `black-forest-labs/flux-2-pro` / `-schnell` | — | Flux 官方系 | 通用 |
| `bytedance/seedream-4.5` | $0.04 | 中文排版/海报/品牌最强 | 海报、带字创意 |
| `stability-ai/stable-diffusion-3-5-large` | — | 开源灵活 | 实验 |
| `google/nano-banana-pro` | $0.14 | Gemini 系、多图一致 | 大批量 |
| `Tongyi-MAI/Z-Image-Turbo` | $0.005 | 极便宜极快 | 低成本批量 |
| `Flux 2 Klein` | $0.008 | 便宜 | 低成本 |

### 2.2 图生图 / 多图一致性（⭐ 多角度就在这里）
| 模型路径 | 价格 | 输入 | 说明 |
| --- | --- | --- | --- |
| **`wavespeed-ai/flux-kontext-max/multi`** | **$0.08** | **最多 5 张参考图 + prompt** | **多图上下文、主体/风格一致性最强 → 多角度首选用它** |
| `wavespeed-ai/flux-kontext-dev/multi` | — | 多图 | 同系 dev 版 |
| `wavespeed-ai/flux-kontext-dev/multi-ultra-fast` | — | 多图 | 批/多视输入加速 |
| `wavespeed-ai/uno` | $0.05 | 1–5 张参考图 | 角色/商品一致性、虚拟试穿 |
| `bytedance/seedream-4.5/edit` | $0.04 | 1–10 张图 | 多图编辑、保人脸/光影 |

> **多角度怎么用**：把画布选中的图作为参考图，prompt 写「从右侧 90° 看 / 俯视 / 特写，
> 保持主体一致」，WaveSpeed 在同一主体上生成角度变体。适配器 `build_multiangle_prompt()`
> 已把 Lovart 的 rotate/tilt/scale 参数翻译成英文机位提示词。本质是**图像条件反射的角度变体**
> （非真 3D），但产品体验最贴近 Lovart「多角度」，且单 worker 最易落地。

### 2.3 工具类（可平替我们现有 upscale / matting / split）
| 模型路径 | 用途 | 备注 |
| --- | --- | --- |
| `wavespeed-ai/background-removal` | 抠图（matting） | 可替代 remove.bg |
| `wavespeed-ai/face-enhance` | 人脸增强 | 人像精修 |
| （FLUX.2 系列 Edit 变体） | 局部重绘/inpaint | 风格一致编辑 |

### 2.4 视频（文生视频 / 图生视频 / 参考生视频）
| 模型路径 | 价格 | 说明 |
| --- | --- | --- |
| `alibaba/wan-2.7/text-to-video` | $0.50/段 | 同族还有 image-to-video / reference-to-video / video-edit / video-extend / image-edit |
| `alibaba/wan-2.2` Animate | — | 120s 角色动画；Ultra Fast $0.01/秒 |
| `seedance-2.0-fast` | $0.10/秒 | 字节视频 |
| `kling-3.0-std` | $0.084/秒 | 强运动控制 |
| `veo-3.1-fast` | $0.15/秒 | 谷歌高品质 |
| `wan-3.0` | $0.05–0.20/秒 | 新架构、含音频 |
| `infinitetalk` | $0.03/秒 | 说话头像 |

### 2.5 LLM（OpenAI 兼容端点）
Claude Opus 4.8 / Sonnet 4.6、GPT-5.5、Gemini 3.1 Pro、Qwen3.7 Max、DeepSeek V4 Pro。
→ 经 OpenAI 兼容端点。**注：聊天补全我们仍走 new-api 网关（用户免 Key、配额归网关），
不抢这个 key**，避免计费口径混乱。

---

## 3. 定价速查（官方，可能变动）
- 图像：$0.005（Z-Image-Turbo）~ $0.14（Nano Banana Pro）/张；多角度 $0.08/张。
- 视频：按秒计费，$0.01（Wan2.2 Ultra Fast）~ $0.15（Veo3.1 Fast）/秒。
- 无订阅、按量付费；注册送 $1；部分模型阶梯/缓存计价。

---

## 4. flovart-bff 接入状态

| 能力 | 状态 | 落点 |
| --- | --- | --- |
| 多角度 `multi-angle` | ✅ 已接线（待填 key + 实测） | `TASK_TYPES["multi-angle"]` provider=thirdparty,tp=wavespeed；`app/thirdparty/wavespeed.py`；`tasks.py` 提交/轮询/取消分支 |
| 其余图生图/视频 | 🔲 设计预留（同 `thirdparty/<tp>.py` 接口） | 加 `TASK_TYPES` 条目即可，切换 provider 只改 `tp` 字段 |
| 聊天/LLM | 🚫 不接（走 new-api） | 计费口径统一 |

**配置项**（`.env`，key 留空则功能不可用但不崩）：
```
WAVESPEED_API_KEY=            # ← 把你的 key 填这里（https://wavespeed.ai/accesskey）
WAVESPEED_BASE_URL=https://api.wavespeed.ai/api
WAVESPEED_MULTIANGLE_MODEL=wavespeed-ai/flux-kontext-max/multi
WAVESPEED_TIMEOUT=300
WAVESPEED_POLL_INTERVAL=2
WAVESPEED_POLL_MAX=120
```

**调用链**：
```
POST /api/tasks {type:"multi-angle", params:{source_media_key, rotate, tilt, scale}}
  → BFF 取源图 presigned URL → WaveSpeed 提交（参考图+机位 prompt）
  → 前端轮询 GET /api/tasks/{id} → completed 时 BFF 把产物落 cloud_media 改写 result
```

---

## 5. 已实现的适配器要点（`app/thirdparty/wavespeed.py`）
- `submit_multi_angle(uid, source_media_key, rotate, tilt, scale, ...)` → prediction id。
- `get_status(prediction_id)` → `(status, image_urls)`，对 `outputs` 多形态（字符串/字典/嵌套数组）归一化。
- `cancel(prediction_id)` → 尽力取消（忽略失败）。
- `build_multiangle_prompt()` → rotate/tilt/scale 中文参数 → 英文机位提示词。
- 全走 `Authorization: Bearer <env key>`；**key 不落盘、不进响应**。

---

## 6. 待办 / 风险
- [ ] **填 key 并实测**：把 `WAVESPEED_API_KEY` 注入后，跑一次单角度（rotate=90）验证
      submit→poll→落盘全链路；核对 `data.id` / `outputs` 真实字段名（适配器已容错，但实测更稳）。
- [ ] 多角度批量（三视图/96 阵列）：前端分批提交多个 `multi-angle` 任务（BFF 单任务单角度）。
- [ ] 本地存储模式（无 OSS）下源图上传端点 `/v3/upload` 真实形态待实测回填（`_upload_local` 已留接口）。
- [ ] 可选：用 `enable_sync_mode` 简化短任务轮询；用 `enable_base64_output` 省下载回程（第二期优化）。
- [ ] 评估是否把 upscale/matting 也切到 WaveSpeed（`background-removal` / FaceEnhance），减少网关渠道依赖。
