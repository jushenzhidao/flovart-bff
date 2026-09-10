# 方案 A：Flovart 图片能力 → 网关「能力→模型」映射设计

> 背景：当前 Flovart 图片能力在 BFF 层**硬编码**到 new-api 网关的固定端点（`images/mask` / `images/outpaint` …），
> 网关端点背后接哪个模型由网关后台手动配置，**没有消费 model-plaza 模型映射 UI**。
> 导致管理员在 model-plaza UI 配的「GPT Image 2 线路」对 Flovart 不可见，Flovart 编辑类按钮只能 400。
>
> 方案 A 目标：**让 model-plaza 的模型映射 UI 成为唯一事实来源**，网关层建立
> 「Flovart 能力 → 产品模型」的映射并消费 model-plaza 映射，Flovart 前端/BFF 零改动即可随管理员配置生效。

---

## 一、目标架构

```
┌─────────────┐   type=mask    ┌──────────────┐   images/mask   ┌─────────────────────┐
│ Flovart 前端 │ ─────────────▶ │ Flovart BFF  │ ───────────────▶ │  你的网关层          │
│ (免 Key)    │                │ (透传 params)│                  │  (NEW-API 网关/自研) │
└─────────────┘                └──────────────┘                  └──────────┬──────────┘
                                                                           │ 查能力→模型映射
                                                                           ▼
                                                              ┌──────────────────────────┐
                                                              │ model-plaza 模型映射 (单一事实来源)
                                                              │ GPT Image 2 → 文生图/图生图/编辑复用线路
                                                              │ Remove.bg   → 线路B
                                                              │ Stable Upscale → 线路C
                                                              └──────────────────────────┘
```

**关键变化**：网关收到 `images/mask` 后，不再"固定调某个写死的模型"，而是：
1. 查「能力映射表」：`mask` → 产品模型 `GPT Image 2`
2. 查 model-plaza 映射：`GPT Image 2` → 实际渠道/endpoint/key
3. 按能力（`mask`）把 Flovart 参数翻译成 GPT Image 2 的 `/images/generations` 编辑参数（带 `image`/`mask`/`prompt`），调真实模型，把结果按 Flovart 约定格式回传

---

## 二、能力 → 产品模型 映射表（网关侧维护）

这是网关层的**路由核心表**。每个 Flovart 能力对应一个 model-plaza 里的「产品模型」。

| Flovart 能力 (网关端点) | 推荐产品模型 (model-plaza 中需存在) | 模型用途 | 网关内部调用方式 | 备注 |
|---|---|---|---|---|
| `images/generations`（image-gen） | `GPT Image 2` | 文生图/图生图 | 直接转 `/images/generations`（`prompt`, `size`, `n`） | 已可用 |
| `images/outpaint` | `GPT Image 2`（复用 images/generations） | 扩图 | 转 `/images/generations` 并带 `image` + mask | 原生强项 |
| `images/mask` | `GPT Image 2`（复用 images/generations） | 局部重绘 | 转 `/images/generations` 并带 `image` + `mask` + `prompt` | 原生强项 |
| `images/edits` | `GPT Image 2`（复用 images/generations） | 通用编辑/换装 | 转 `/images/generations` 并带 `image` + `prompt` | — |
| `images/annotate` | `GPT Image 2`（复用 images/generations） | 涂鸦引导 | 转 `/images/generations` 并带 `image` + 涂鸦参考 + `prompt` | 半支持 |
| `images/relight` | `GPT Image 2`（复用 images/generations） | 重打光 | 转 `/images/generations` 并带 `image` + 光照 `prompt` | 半支持 |
| `images/remove-bg` | `GPT Image 2`（复用 images/generations）**或** `Remove.bg` | 去背 | 转 `/images/generations`（prompt 去背）/ 专用 API | 去背建议专用模型更准 |
| `images/upscale` | `Stable Image Upscale`（独立产品模型） | 超分放大 | 超分 API | ⚠️ **不能复用 GPT Image 2**，必须独立超分模型 |
| `GATEWAY_VIDEO_TASKS_PATH`（video-gen） | 视频模型（如 `Seedance`/`Wan`） | 视频 | 视频 tasks | 异步 |
| `split-layers`（WaveSpeed 直连） | `Seedream v5.0 Pro Layer` | 图层分解 | WaveSpeed API | BFF 直连，不经网关 |
| `multi-angle`（WaveSpeed 直连） | `FLUX Kontext Max Multi` | 多角度 | WaveSpeed API | BFF 直连，不经网关 |
| 反推 Prompt（chat 通道） | `Gemini 2.5 Flash` / `GPT-4o`（vision） | 图生 prompt | `/api/chat/completions` | BFF `CHAT_VISION_MODEL` 决定 |

> 注：`split-layers` / `multi-angle` 当前在 BFF 直连 WaveSpeed，不属于本方案网关端点改造范围，
> 若未来想统一进网关，可同样加 `images/split-layers` / `images/multi-angle` 端点并按此表映射。

---

## 三、网关层改造点

### 3.1 能力路由中间件（新增）
网关在 `images/*` 端点入口加一层：

```
收到 POST /images/{capability}
  → 读 body.params
  → capability_to_model[capability] 得到 产品模型名 (如 "GPT Image 2")
  → capability_to_call_format[capability] 得到 当前能力的参数拼装模板
  → 调 model-plaza 映射服务: resolve(产品模型名) → {channel, endpoint, api_key}
  → 按模板把 params 转成 GPT Image 2 的 /images/generations 请求（是否带 image/mask/prompt 由 capability 决定）
  → 调真实模型，归一化返回 (image / images / layers / url)
```

### 3.2 调用格式翻译
不同产品模型调用格式不同，网关需按模型类型翻译：

| 产品模型类型 | 网关内部转换 |
|---|---|
| GPT Image 2（文生图） | `params` → `/images/generations`（`prompt`, `size`, `n`） |
| GPT Image 2（复用 images/generations） | `params` → `/images/generations`，按能力决定是否带 `image`/`mask`/`prompt` |
| Remove.bg | `params` → remove.bg API（`image`） |
| Stable Upscale | `params` → 超分 API（`image`, `scale`） |

同一个 `GPT Image 2` 产品模型只配一条线路；网关端点内部按 `capability` 区分参数拼装，**不需要在 model-plaza 为 outpaint/mask 单独建“edit 线路”**。

### 3.3 回传归一化（不变）
沿用 Flovart BFF 已有约定：`result` 支持 `data` / `image` / `images` / `layers` / `url`，
BFF `tasks.py` 的 `_normalize_sync_result` / `_iter_outputs` 已兼容。

---

## 四、映射表同步机制（让 UI 真正生效）

model-plaza 模型映射 UI 是**单一事实来源**，网关不能各自写死。三种同步方式：

| 方式 | 机制 | 优点 | 缺点 | 推荐度 |
|---|---|---|---|---|
| **A1 共享配置中心** | 网关与 model-plaza 读同一份 DB/配置（如 PostgreSQL `model_mappings` 表） | 实时一致、无延迟 | 需两系统共享存储 | ✅ 最推荐 |
| **A2 网关轮询拉取** | 网关定时（如 30s）从 model-plaza API `GET /api/mappings` 拉全量缓存 | 解耦、易实现 | 有最长 30s 延迟 | ✅ 次选 |
| **A3 Webhook 推送** | model-plaza 改映射时 `POST` 通知网关热加载 | 实时 | 需双向鉴权、容错复杂 | ⚠️ 复杂 |

**建议落地顺序**：先 A2（网关轮询，最快打通）→ 稳定后切 A1（共享配置中心）。

> 不论哪种，管理员的「增删改线路 / 切换 GPT Image 2 线路」操作，
> 都应在 Flovart 下一次请求时自动生效，无需改 Flovart 代码、无需重启 BFF。

---

## 五、BFF 侧是否需要改

**基本不用改。** 当前 BFF 已满足方案 A 前提：
- `tasks.py` `TASK_TYPES` 已含 `mask/outpaint/remove-bg/upscale/annotate/relight/edit`（sync 透传）
- `config.py` 的 `GATEWAY_SYNC_*_PATH` 默认值 `images/mask` 等已对
- 前端已全改平台模式（免 Key，走 `/api/tasks`）

唯一可选增强：网关就绪前，BFF 可在「网关返回 404/能力未接」时返回更友好提示
（如「该编辑能力网关尚未配置模型，请联系管理员」），避免前端裸 400。

---

## 六、计费与隔离（不变）

- 图片/编辑类：BFF `request_as_user`（带 `New-Api-User:<uid>`）打网关 → 落到**登录用户自身 new-api 配额**
- 反推 Prompt：BFF `/api/chat/completions` 用每人 `sk-` → 同一个人配额
- 多用户零串账，与当前一致

---

## 七、落地步骤清单（甩给网关团队）

1. [ ] 网关新增「能力路由中间件」，消费 `capability_to_model` 表（第二节）
2. [ ] 在 model-plaza 确认/创建以下产品模型记录：
   - `GPT Image 2`（复用 `images/generations` 做文生图/图生图/编辑）
   - `Stable Image Upscale`（独立超分）
   - 可选 `Remove.bg`
3. [ ] 网关接 model-plaza 映射（A2 轮询先上）：`GET /api/mappings` 拉「产品模型→渠道/endpoint/key」
4. [ ] 网关实现 `images/mask` `images/outpaint` `images/edits` `images/annotate` `images/relight`
   → 全部路由到 `GPT Image 2`，内部按能力把请求参数拼成 `images/generations` 格式
5. [ ] 网关实现 `images/remove-bg` → `GPT Image 2`（prompt 去背）或 `Remove.bg`
6. [ ] 网关实现 `images/upscale` → `Stable Image Upscale`（**不可接 GPT Image 2**）
7. [ ] 联调：Flovart 点「编辑蒙版」→ 网关查映射 → GPT Image 2 `/images/generations`（带 mask）→ 返回透明 PNG
8. [ ] 验证管理员在 model-plaza UI 切换 GPT Image 2 线路后，Flovart 下一次请求即时生效

---

## 八、与现状文档的关系

- 本文是 `image-gateway-endpoints.md` 的**上层架构升级**：前者假设"网关端点写死接某模型"，
  本文解决"网关端点动态按 model-plaza 映射选模型"，让管理员 UI 配置真正生效。
- Flovart 前端/BFF 代码无需因本文改动；改动集中在**网关层 + model-plaza 映射数据**。
