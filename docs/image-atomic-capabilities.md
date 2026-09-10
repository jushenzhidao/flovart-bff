# Flovart 图片原子能力全景清单（含模型 / 接口路径 / 接入方式）

> 排查基准：flovart-web（前端）+ flovart-bff（BFF 代理）。
> 前端工具栏入口：`components/workflow/InfiniteWorkflow.tsx:568` `builtInImageTools`
> 前端 operation 封装：`services/workflowImageOperations.ts`
> BFF 任务表：`flovart-bff/app/tasks.py:44` `TASK_TYPES`
> 前端任务客户端：`services/imageTask.ts`（统一视图 `submitImageTask/getImageTask` → `/api/tasks`）

---

## 一、两条主干通路（先理解这个，再看下一个表）

| 通路 | 是否走 BFF 网关 | 是否消耗 new-api 积分 | 模型来源 | 关键代码 |
|---|---|---|---|---|
| **平台模式（免 key）** | ✅ 走 BFF → new-api 网关 | ✅ 消耗（由网关计费） | 网关已接入模型，前端 key 配 `imageGenModel` 触发 | `submitImageTask('image-gen'/'upscale'/'remove-background'/'split-layers'/'outpaint'/'mask'/'annotate'/'relight'/'edit'/'video-gen')`；反推走 `/api/chat/completions`(CHAT_VISION_MODEL) |
| **BYOK 直连（用户 key）** | ❌ 前端直连第三方 | ❌ 不消耗 BFF 积分（扣用户自己的 key） | 用户在「供应商设置」配的模型 + API Key + BaseURL | `editImageWithProvider`(无 imageGenModel 时) / `generateImageWithProvider`(BYOK 分支) / `splitImageLayersWithProvider`(BYOK 分支) |

判断规则（前端 `resolveImageRoute`）：用户 key 里配了 `imageGenModel` → 走平台；否则走 BYOK 直连。
**2026-09-10 改造**：outpaint/mask/annotate/relight/reversePrompt 五个原纯 BYOK 能力已改为「平台模式优先、用户免 Key」——`editImageWithProvider` 在 `key.imageGenModel` 配置时经 `submitImageTask` 走 BFF；`reversePrompt*` 在 `!key.key` 时走 BFF `/api/chat/completions`。计费经 `request_as_user`(New-Api-User:uid) / 每人 sk- 落到登录用户配额。

---

## 二、全部图片原子能力清单（14 项调模型 + 纯前端）

### A. 调模型的原子能力

| # | 原子能力 | 工具栏入口 | 前端封装函数 | 模式 | 模型 / 接口路径 | 备注 |
|---|---|---|---|---|---|---|
| 1 | 文生图 / 图生图 | 生成面板 | `generateImageWithProvider` / `runImageAgentWithProvider` | 平台+BYOK | 平台：`POST /api/tasks {type:'image-gen'}` → BFF→网关 `GATEWAY_IMAGE_TASKS_PATH`(async)/`GATEWAY_SYNC_IMAGE_PATH`(sync)；BYOK：直连生图模型 `/images/generations` | 核心能力 |
| 2 | 高清放大 upscale | `upscale` | `runWorkflowUpscaleOperation`→`runImageAgentWithProvider('upscale')` | 平台+BYOK | 平台：`/api/tasks {type:'upscale'}` → 网关 `GATEWAY_SYNC_UPSCALE_PATH`（Stability / Real-ESRGAN）；BYOK：编辑模型 | 同步阻塞 |
| 3 | 去除背景 remove-background | `removeBackground` | `runWorkflowRemoveBackgroundOperation`→`runImageAgentWithProvider('remove-background')` | 平台+BYOK | 平台：`/api/tasks {type:'remove-background'}` → 网关 `GATEWAY_SYNC_REMOVE_BG_PATH`（remove.bg / Photoroom）；BYOK：编辑模型 | 同步阻塞 |
| 4 | 图层分解 split-layers | `splitLayers` | `runWorkflowSplitLayersOperation`→`splitImageLayersWithProvider` | 平台+BYOK | 平台：`/api/tasks {type:'split-layers'}`；BYOK：`${baseUrl}/split-layers` | 推荐 Seedream v5 Pro Layer Decomposition（平台）/ wavespeed qwen-image/layered（BYOK） |
| 5 | 多角度 multi-angle | `multiAngle` | wavespeed 直连 | 平台/第三方 | `WAVESPEED_MULTIANGLE_MODEL`（flux-kontext-max/multi），需 `WAVESPEED_API_KEY` | |
| 6 | 扩展画面 outpaint | `outpaint` | `runWorkflowImageEditOperation('outpaint')`→`editImageWithProvider` | 平台+BYOK | 平台：`/api/tasks {type:'outpaint'}` → 网关 `GATEWAY_SYNC_OUTPAINT_PATH`；BYOK：编辑模型 `/images/edits` | 平台优先免 Key |
| 7 | 编辑蒙版 mask（局部重绘 inpaint） | `mask` | `runWorkflowImageEditOperation('mask')`→`editImageWithProvider` | 平台+BYOK | 平台：`/api/tasks {type:'mask'}` → 网关 `GATEWAY_SYNC_MASK_PATH`（带 mask 图）；BYOK：编辑模型 | 平台优先免 Key |
| 8 | 标注涂鸦 annotate | `annotate` | `runWorkflowImageEditOperation('annotate')`→`editImageWithProvider` | 平台+BYOK | 平台：`/api/tasks {type:'annotate'}` → 网关 `GATEWAY_SYNC_ANNOTATE_PATH`（传标注图作参考）；BYOK：编辑模型 | 平台优先免 Key |
| 9 | 打光面板 relight | `relight` | `runWorkflowImageEditOperation('relight')`→`editImageWithProvider` | 平台+BYOK | 平台：`/api/tasks {type:'relight'}` → 网关 `GATEWAY_SYNC_RELIGHT_PATH`（`LIGHTING_PRESETS` 拼 prompt）；BYOK：编辑模型 | 平台优先免 Key |
| 10 | 反推 Prompt | `reverse` | `reversePromptStreamWithProvider` | 平台+BYOK | 平台：`/api/chat/completions`(CHAT_VISION_MODEL，免 Key)；BYOK：用户 vision LLM（gpt-4o / gemini / claude） | 平台优先免 Key |
| 11 | 视频生成 video-gen | 视频面板 | `submitVideoTask('video-gen')` | 平台 | `/api/tasks {type:'video-gen'}` → 网关 `GATEWAY_VIDEO_TASKS_PATH` | 恒 async，mp4 产物 |

### B. 纯前端 Canvas（不调模型，无接口）

| 能力 | 入口 | 说明 |
|---|---|---|
| 裁剪 crop | `crop` | `runWorkflowCropOperation` 源→Operation→结果，不原地覆盖 |
| 旋转/镜像 rotate | `rotate` | `runWorkflowRotateOperation` |
| 滤镜 filter | `filter` | 仅 patch 节点 filters 状态，Canvas 渲染 |
| 宫格切分 splitGrid | `splitGrid` | `runWorkflowSplitGridOperation` |
| 分镜拼接 storyboard | `storyboard` | `composeImageGrid` 前端拼图 |
| 自由缩放 | 工具栏「缩放」/ 节点尺寸 | 改 node 尺寸或导出分辨率，**前端 resize，非模型**（高清放大才是 upscale 模型） |
| 画笔/橡皮擦/套索/文字/形状 | 标注涂鸦 UI 层 | 前端绘布交互；最终"标注编辑"才调模型（#8） |
| 预览/替换/下载/复制/删除 | — | 纯交互 |

---

## 三、后续怎么接入（3 种场景）

### 场景 1：网关已接入模型，走平台模式（免 key，最省事）
适用：image-gen / upscale / remove-background / split-layers / **outpaint / mask / annotate / relight / edit** / video-gen / 反推 Prompt。
步骤：
1. new-api 网关接入对应模型（如 Stability 超分、remove.bg、Seedream Layer Decomposition、GPT Image/Seedream 编辑模型、视觉 LLM）。
2. BFF `.env` 配端点路径：
   - `GATEWAY_IMAGE_TASKS_PATH` / `GATEWAY_SYNC_IMAGE_PATH`
   - `GATEWAY_SYNC_UPSCALE_PATH` / `GATEWAY_SYNC_REMOVE_BG_PATH`
   - `GATEWAY_SYNC_OUTPAINT_PATH` / `GATEWAY_SYNC_MASK_PATH` / `GATEWAY_SYNC_ANNOTATE_PATH` / `GATEWAY_SYNC_RELIGHT_PATH` / `GATEWAY_SYNC_EDIT_PATH`
   - `GATEWAY_VIDEO_TASKS_PATH`
   - `CHAT_VISION_MODEL`（反推 Prompt 用的视觉模型，如 gpt-4.1-mini / claude-sonnet-4-5）
   - `GATEWAY_IMAGE_GEN_MODE=async|sync`
3. 前端用户 key 配 `imageGenModel`（非空即触发平台模式）；反推在无用户 Key 时自动走平台。
4. 无需改前端代码（工具栏已接 `submitImageTask` / 反推已接 `/api/chat/completions`）。

### 场景 2：用户自带 key，走 BYOK 直连（兜底，可选）
适用：编辑类/视觉理解类能力在「未配 imageGenModel 且用户有 key」时仍可走 BYOK。
步骤：
1. 用户在「供应商设置」配编辑/vision 模型 + API Key + BaseURL（OpenAI GPT Image / Google Gemini 编辑模型 / OpenRouter / Custom）。
2. 模型须支持图像编辑（`supportsReferenceImageEditing` / `supportsMaskImageEditing` 判定），纯文本模型会 400。
3. 前端已接 `editImageWithProvider`（Google→`generateContent`、OpenAI/Custom→`/images/edits`、OpenRouter→`chat/completions`），仅当 `key.imageGenModel` 未配时才走此分支。

### 场景 3：新增一个图片原子能力（如"风格迁移""卡通化"）
步骤：
1. BFF `tasks.py` `TASK_TYPES` 加 kind：`"style-transfer": {"provider":"gateway","mode":"sync","sync_path": config.GATEWAY_SYNC_STYLE_PATH}`。
2. `routers/tasks.py` 透传（已通用，无需改）。
3. 前端 `imageTask.ts` 的 `ImageTaskType` 联合类型加 `'style-transfer'`。
4. `workflowImageOperations.ts` 加 `runWorkflowStyleTransferOperation`（参考 `runWorkflowUpscaleOperation`）。
5. `InfiniteWorkflow.tsx` `builtInImageTools` 加 `styleTransfer: id => openImageTool('style-transfer', id)` + `WorkflowImageToolState['kind']` 加类型 + 加 confirm 分支。
6. 若走 BYOK：在 `aiGateway.ts` 加对应 `xxxWithProvider` 函数（参考 `editImageWithProvider`）。

---

## 四、当前关键缺口（待补）
- ✅ **outpaint/mask/annotate/relight/reversePrompt 已平台化（2026-09-10）**：BFF 新增 `outpaint/mask/annotate/relight/edit` 五个 sync 任务类型 + 前端平台分支，用户免 Key 即可用，计费走登录用户配额。
- **真实网关端点未实测**：outpaint/mask/annotate/relight/edit 的 `GATEWAY_SYNC_*_PATH` 默认值为约定占位（images/outpaint 等），需网关侧接入对应编辑能力并配真实端点后端点对点联调；`CHAT_VISION_MODEL` 需指向可用视觉模型。
- **multi-angle 真实 WaveSpeed 端点**：依赖 `WAVESPEED_API_KEY` 与 `WAVESPEED_MULTIANGLE_MODEL`。
