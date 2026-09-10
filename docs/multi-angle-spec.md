# Flovart 多角度（Multi-Angle）功能 — 对标 Lovart 技术方案

> 背景：飞哥反馈 Lovart 的多角度功能满足电商 / IP / 角色设计的多视角展示需求，当前 Flovart 只有 CSS `hue-rotate` 等调色滤镜，无法生成同一主体的真实多角度变体，需要对标落地。

---

## 1. 功能定义（Lovart 对照）

**核心体验**：用户上传或选中画布内的一张图片，系统在同一张图的基础上，通过调整视角参数生成不同角度的变体，而非重新抽卡生成。

| Lovart 能力 | Flovart 目标 | 说明 |
| --- | --- | --- |
| **入口** | 选中图片 → 顶部工具栏「多角度」按钮 | 与 Lovart 工具栏一致 |
| **主体模式（Image）** | 主体模式（MVP 优先） | 鼠标拖拽/参数化控制，所见即所得，普通用户友好 |
| **相机模式（Camera）** | 相机模式（第二阶段） | 调整虚拟摄像机机位，偏专业摄影语言 |
| **参数矩阵** | Rotate × Tilt × Scale | 最多 8×4×3 = **96 个视角组合** |
| **批量输出** | 支持单张 / 预设阵列批量生成 | 用于电商三视图、潮玩 96 角度阵列 |
| **一致性** | 保持主体、服饰、光影一致 | 依赖底层多视角扩散 / 3D 一致性模型 |

### 1.1 参数档位（与 Lovart 对齐）

| 参数 | 档位 | 说明 |
| --- | --- | --- |
| **Rotate** 水平旋转 | `0° / 45° / 90° / 135° / 180° / 225° / 270° / 315°` | 8 档，绕主体 Y 轴 |
| **Tilt** 俯仰 | `-30° / 0° / 30° / 60°` | 4 档，正负仰俯 |
| **Scale** 远近 | `close-up / medium / wide` | 3 档，特写 / 中景 / 广角 |

---

## 2. 总体架构（直连第三方 Provider 模式）

> ⚠️ 关键修正（2026-09-07）：原方案假设"走 new-api 网关"，但 new-api 当前未接入多视角/3D 模型，且 Lovart 多角度本质是 **image→3D→多视角渲染** 或 **多视角扩散**，**必须直连第三方 API**。本方案据此改为直连第三方 provider，并把该模式做成 BFF 可复用的 `provider` 抽象（不只服务多角度，后续 3D / 视频特效等同样复用）。

```
画布选中图片
    │
    ▼
前端「多角度」面板（主体/相机模式 + 参数 + 预览/生成）
    │
    ▼  POST /api/tasks {type:'multi-angle', params:{source_media_key, mode, rotate, tilt, scale, batch?}}
BFF app/routers/tasks.py
    │
    ▼  落请求日志（cloud_request_log）+ 按 TASK_TYPES[kind].provider 分流
BFF app/tasks.py
    ├── provider='gateway'   → _proxy_client() 转发 new-api（image-gen/upscale/... 现有逻辑）
    └── provider='thirdparty'→ _thirdparty_client(provider) 直连第三方 API（multi-angle / 3D / ... 新增）
            │
            ▼  第三方异步任务（submit→poll，或 fal.ai queue/subscribe）
        第三方 API（Tripo / fal.ai / Meshy / CSM ...）
            │
            ▼  产物（多视角 PNG / GLB）回写 BFF cloud_media（复用 _persist_outputs）
前端轮询 /api/tasks/{request_id} 取结果 → 画布插入多角度产物
```

### 2.1 直连第三方 Provider 抽象（通用，可复用于其它功能）

在现有 `tasks.py` 的 `TASK_TYPES` 上增加 `provider` 字段，区分"走网关"与"直连第三方"，二者复用同一套 **提交→轮询→落盘 BFF cloud_media** 管线：

```python
TASK_TYPES = {
    # 现有：走 new-api 网关
    "image-gen":        {"provider": "gateway", "mode": ..., "async_path": ...},
    "upscale":          {"provider": "gateway", "mode": "sync", "sync_path": ...},
    # 新增：直连第三方
    "multi-angle":      {"provider": "thirdparty", "tp": "wavespeed", "mode": "async"},
    # 未来可加：3d-model / video-fx ... 同样 provider='thirdparty'
}
```

> **首期 Provider 已定 WaveSpeed（2026-09-07）**：飞哥已拿到 WaveSpeed key，多角度首期走
> `wavespeed-ai/flux-kontext-max/multi`（多图上下文、主体一致性最强，$0.08/张），
> 本质是「图像条件反射的角度变体」而非真 3D，但产品体验最贴 Lovart、单 worker 最易落地。
> 实现见 `app/thirdparty/wavespeed.py`，能力地图与接入要点见 `docs/WAVESPEED-API-REFERENCE.md`。
> Tripo/fal 等 3D 一致性路线列为**二期**（需 GLB 渲染步），切换只改 `tp` 字段。

`app/tasks.py` 新增：
- `_thirdparty_client(tp: str) -> httpx.AsyncClient`：按 `tp` 取 base_url + 注入 `Authorization: Bearer <THIRDPARTY_<TP>_KEY>`（来自 env），无 base_url 时每次调用显式拼 URL。
- `_submit_thirdparty(uid, kind, params)` / `_poll_thirdparty(...)`：通用第三方异步提交/轮询分支；各 provider 的真实请求/响应解析下沉到 `app/thirdparty/<tp>.py`（如 `tripo.py`、`fal.py`），实现统一接口 `submit()/get_status()/normalize_result()`。
- 复用 `_normalize_sync_result` / `_persist_outputs`：第三方产物（多张 PNG / GLB）同样落 `cloud_media`，result 改写为 BFF media URL。

`app/config.py` 新增：
```python
THIRDPARTY_TP_KEYS: dict = {  # 各第三方服务的 API Key（env 注入，绝不进代码）
    "tripo": os.getenv("TRIRPO_API_KEY", ""),     # 注意变量名以实际为准
    "fal":   os.getenv("FAL_KEY", ""),
    "meshy": os.getenv("MESHY_API_KEY", ""),
}
THIRDPARTY_ENABLED: bool = bool(any(THIRDPARTY_TP_KEYS.values()))  # 无 key 时 /readyz 提示但未崩
```

> 设计要点：**BFF 永不暴露第三方 Key 给前端**（前端只拿 BFF media URL）；多副本共享靠 PG + 第三方任务状态可由 Redis 暂存（架构已预留）；第三方调用必须包 Semaphore 限流 + 超时 + 重试（现有 `call_gateway_upstream` 已有上游保护，第三方分支复用同一套）。

---

## 3. 前端改造

### 3.1 入口
- 画布选中图片节点时，顶部工具栏增加「多角度」按钮（与 Lovart 的 快捷编辑 Tab / 评论 / 放大 / 去背景 / 图层拆分 / 编辑文字 / 多角度 / 动态图片 对齐）。
- 右键菜单 / 属性面板也可增加「多角度」。

### 3.2 面板 UI（新增 `MultiAnglePanel.tsx`）

```tsx
interface MultiAngleParams {
  mode: 'subject' | 'camera';
  rotate: 0 | 45 | 90 | 135 | 180 | 225 | 270 | 315;
  tilt: -30 | 0 | 30 | 60;
  scale: 'close-up' | 'medium' | 'wide';
  batch?: 'single' | 'preset-front-side-back' | 'all-96'; // 批量模式
}
```

面板分三栏：
- **模式切换**：主体模式 / 相机模式
- **参数控制**：
  - Rotate：圆形旋钮或 8 向罗盘
  - Tilt：-30° ~ +60° 滑块
  - Scale：close-up / medium / wide 三档按钮
- **批量预设**：
  - 单个角度（当前参数）
  - 电商三视图（0° / 90° / 180°，medium）
  - 96 角度全景阵列（全部组合，分批提交）

### 3.3 调用链
- `services/imageTask.ts` 的 `ImageTaskType` 增加 `'multi-angle'`。
- `submitImageTask('multi-angle', params)` → `pollImageTask` → `extractImageOutputs`。
- 产物如果是多张，在画布内以网格/阵列形式插入或生成新节点。

---

## 4. BFF 改造（直连第三方分支）

### 4.1 `app/tasks.py`

在 `TASK_TYPES` 中新增 `provider='thirdparty'` 条目（见 §2.1），并实现第三方分发分支：

```python
"multi-angle": {"provider": "thirdparty", "tp": "tripo", "mode": "async"},
```

- `_submit_thirdparty(uid, kind, params)`：按 `tp` 调 `app/thirdparty/tripo.py:submit()` 提交第三方任务，返回第三方 `task_id`。
- 轮询：复用现有 `get_task` 的 async 轮询逻辑，但状态查询改为 `_poll_thirdparty`（调 `thirdparty/tripo.py:get_status()`）。
- 产物：第三方返回多视角 PNG 数组（或 GLB→渲染出的 PNG），复用 `_persist_outputs` 落 `cloud_media`，`result.images` 改写为 BFF media URL。

### 4.2 `app/thirdparty/tripo.py`（新增，真实契约待 Key 联调回填）

实现统一接口，供 `tasks.py` 调用：
```python
async def submit(uid, params) -> str:            # 返回第三方 task_id
async def get_status(task_id) -> dict:           # 返回 {status, outputs}
def normalize_result(raw) -> dict:               # → {images:[{url/b64, label}]}
```

### 4.3 `app/config.py`

新增第三方配置（见 §2.1 的 `THIRDPARTY_TP_KEYS` / `THIRDPARTY_ENABLED`）。

### 4.4 请求日志

`routers/tasks.py` 已统一在提交时写 `cloud_request_log`：`kind='multi-angle'`，`payload_json` 含 `{source_media_key, mode, rotate, tilt, scale, batch}`；`provider='thirdparty'`、`gateway_request_id` 存第三方 task_id，便于后续拉日志对齐。

### 4.5 产物归 BFF 盘

`tasks.py` 的 `_persist_outputs` 已把产物落 `cloud_media`。多角度产物为 `images: [...]` 数组时，每张图分别 `media_put`，并把 `result` 改写为 BFF media URL + `_bffMediaKey`。

---

## 5. 第三方 API 选型（直连，关键决策）

new-api 网关无多视角模型，必须直连第三方。按"输出即多角度 PNG、与 Lovart 体验最贴、异步 task/poll 与现有框架契合"排序：

| 优先级 | Provider | 路线 | 输入 | 输出 | 异步模式 | 计费 | 适配度 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **P0（推荐）** | **Tripo** (`api.tripo3d.ai/v2`) | image→3D→渲染多视角 | 单图 URL | GLB + 多视角预览图 / 渲染 PNG | 任务制 `submit→GET /task/{id}` 轮询 | 信用点 ~30-50/次 | ⭐ 产品级一致性最好；需确认是否直出多角度 PNG（否则加渲染步） |
| P1 | **fal.ai**（统一托管） | 既托管 Tripo/Meshy，也托管 `zero123`/`sv3d` 多视角扩散 | 单图 | 多视角 PNG / 视频帧 | `queue.submit` + `queue.result` 或 `subscribe` | 按模型 | ⭐ 一个 Key 通多家；`zero123/sv3d` 直接出 2D 多视角，免渲染 |
| P1 | **Meshy** (`api.meshy.ai`) | image→3D | 单图 / 多图 | GLB + 预览 | 任务制轮询 | 信用点 | 文档最好，企业认证 |
| P2 | **CSM** / **Rodin** | image→3D | 单图 | GLB | 任务制 | 较高 | 偏企业/高价，MVP 不优先 |

**推荐落地路线**：
1. **MVP（直出 2D 多视角，最省事）**：用 **fal.ai** 托管的多视角扩散（`zero123` / `sv3d`），单图直接返回 N 张角度 PNG，**无需 3D 渲染服务**，与 BFF 单 worker Python 最契合。
2. **升级（产品级一致性）**：用 **Tripo** image→3D，再对 GLB 做正交渲染得到 front/back/left/right/top 多角度 PNG（渲染可借 fal.ai 的 `tripo` 预览，或后续接薄渲染服务）。
3. 二者经 `app/thirdparty/<tp>.py` 同一接口接入，BFF 只认统一 `normalize_result`，切换 provider 只改 `TASK_TYPES` 的 `tp` 字段。

> 注：具体请求/响应字段（如 Tripo 的 `output.rendered_images` 是否存在、fal `zero123` 的 `images[]` 结构）**需在拿到 sandbox Key 后真实联调回填**——避免像之前 upscale/matting/split 那样"按推断写解析导致未实测"。

---

## 6. 数据流与接口契约

### 6.1 提交请求
```http
POST /api/tasks
Content-Type: application/json

{
  "type": "multi-angle",
  "params": {
    "source_media_key": "a1b2c3...",  // BFF media key（原图已落 BFF 盘）
    "mode": "subject",                // subject | camera
    "rotate": 90,
    "tilt": 0,
    "scale": "medium",
    "batch": "single"                 // single | front-side-back | all-96
  }
}
```

### 6.2 响应（与现有 ImageTask 统一）
```json
{
  "success": true,
  "data": {
    "id": "req_xxx",
    "taskId": "gtask_xxx",
    "kind": "multi-angle",
    "mode": "sync",
    "status": "succeeded",
    "result": {
      "images": [
        { "url": "/api/me/media/{key}", "_bffMediaKey": "...", "mime": "image/png", "label": "r90_t0_medium" }
      ]
    }
  }
}
```

### 6.3 批量 96 角度
- 不建议一次性提交 96 个同步任务（会超时）。
- 前端分批：把 96 个参数组合分成多批（如每批 8 个），并发但受 `Semaphore` 限流。
- 或者后端提供批量提交接口：`POST /api/tasks/batch` 接收参数矩阵，后端串行/并发提交到网关并聚合结果。

---

## 7. 产物处理与画布集成

- **单张**：替换当前选中图或插入新节点。
- **电商三视图**：生成 3 张并排插入画布（0° / 90° / 180°）。
- **96 角度阵列**：
  - 方案 A：生成 96 张缩略图，用户点击某张再放大；
  - 方案 B：按 8×12 网格展示，支持导出；
  - 方案 C：仅生成预设 12 个相机机位（Lovart 的「12 个机位跑一圈」），减少消耗。

---

## 8. 计费与配额

- 计费由 new-api 网关负责，BFF 不在 handler 内扣额度（遵循现有计费边界）。
- 多角度属于**图生图 / 编辑类任务**，建议按生成张数计费（或每张等价于 1 次 image-gen 调用）。
- BFF 配额：`OSS_QUOTA_BYTES` 继续约束产物总存储量。

---

## 9. 风险与待办

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| **无第三方 Key / 未联调真实契约** | 阻塞 MVP | 先锁定 1 个 provider + sandbox Key，真实回填 `thirdparty/<tp>.py` 解析（勿推断） |
| 96 角度批量调用超时 | 用户体验差 / 配额爆 | 分批 + 限流；默认只给 8/12 个预设机位 |
| 主体一致性差 | 电商/角色设计不可用 | 选 3D 一致性路线（Tripo）；或后处理锁定特征 |
| 产物多张导致画布卡顿 | 前端性能 | 96 张用缩略图/分页；只加载可视区域 |
| 第三方 Key 泄露 / 限流 | 安全 / 稳定性 | Key 仅存 env、BFF 注入 header；Semaphore 限流 + 超时 + 重试 |

---

## 10. 落地步骤建议

1. **确认网关模型**：检查 new-api 渠道是否已有 Zero123 / InstantMesh / Tripo / 第三方多视角 API；没有则先接入一个。
2. **BFF 侧**：`config.py` 加 3 个 env + `tasks.py` `TASK_TYPES` 加 `multi-angle` + `routers/tasks.py` 无需改动（已通用）。
3. **前端侧**：
   - `services/imageTask.ts` 加 `multi-angle` 类型；
   - 新增 `components/MultiAnglePanel.tsx`；
   - 画布顶部工具栏加入口；
   - 产物插入逻辑（单张 / 三视图 / 缩略图阵列）。
4. **联调**：单张 0°→90°→180° 走通后，再放开展示交互优化。
5. **文档与测试**：补充 `IMAGE-ASYNC-TASKS-CONTRACT.md` 中 `multi-angle` 的 payload/result 契约。

---

## 11. 需要你拍板

- **第三方 Provider 选型**：MVP 走 **fal.ai（zero123/sv3d，直出 2D 多视角、免渲染）** 还是直接上 **Tripo（产品级一致性、需渲染步）**？或两者都要接（同一 `thirdparty/<tp>.py` 接口，切换只改 `tp` 字段）。
- **Sandbox Key**：给我对应 provider 的测试 Key（如 `FAL_KEY` / `TRIPO_API_KEY`），我才能真实联调、回填 `thirdparty/<tp>.py` 的请求/响应解析（避免"按推断写"）。
- **MVP 范围**：先做「主体模式 + 8 个固定角度」还是直接上 96 角度参数矩阵？
- **产物展示**：生成后是替换原图、插入新节点，还是弹出独立的多角度阵列面板？
- **渲染步（仅 Tripo 路线需要）**：是否有现成 GLB 渲染服务？没有则 MVP 先用 fal.ai 直出 2D 多视角规避。
