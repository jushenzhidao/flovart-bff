# tasks（细节卷 · 从 MEMORY.md 拆出 2026-09-16）

> MEMORY.md 只留铁律摘要，血案全过程/完整表格见本文件。

## 图片/视频任务（`app/tasks.py` 同步/异步双模式）
- `TASK_TYPES` 每项含 `mode` + `async_path`/`sync_path` + `provider`(gateway/thirdparty)；`multi-angle` 走 thirdparty(wavespeed)
- `submit(uid,kind,params)`：带 `_gateway`→BFF 直连用户指定外部网关（BYOK）；否则走平台网关（计费落用户配额）
- ⭐ **非阻塞**（2026-09-14）：落请求日志后 `asyncio.create_task`（强引用集 `_BACKGROUND_TASKS`）立即返回 `processing`。**根因**：原阻塞到底 + nginx `proxy_read_timeout 60s` < 生图 60~62s → 上游已出图但响应被掐 → 前端裸 `Network Error`。⇒ nginx 读超时调 **600s**（`data/release/nginx.conf` 已改）
- sync 分支曾有真 bug：**漏 `request_log_put`** → 内部只有 UPDATE 打在不存在行上静默 0 行 → 轮询恒 404。**已补 put + 改非阻塞**（`_spawn_sync_call`/`_run_sync_safe`）
- 前端轮询键用返回 `id`(=request_id)，非旧 task_id；`processing` 是中间态非终态
- 产物统一落 BFF 对象存储（元数据入 PG `cloud_media`），result 改写为 `/api/me/media/{key}` + `_bffMediaKey`
- 归一化 `_normalize_sync_result`：OpenAI `{data}`→`{images}`；前端 `extractImageOutputs` 扫 `image/images/layers/data`
- ⭐ **参考图入参契约（curl 实测，勿翻转）**：网关 `image` **必须是 data URL 字符串数组** `["data:image/png;base64,..."]`。对象 `{data,mimeType}` → 422；`[{data,mimeType}]` → 422
- ⭐ **图片端点已全面收敛（2026-09-14 飞哥拍板）**：9 个 `GATEWAY_SYNC_*_PATH` **全部 = `v1/images/generations`**（image/upscale/remove-bg/split/outpaint/mask/annotate/relight/edit）。上游只实现 `/images/generations`，**不实现 `/images/edits` 等语义化端点**（必 404），能力靠入参（`image[]`/`mask`/`variant`/`task`）区分。**勿改回语义化路径**
- ⭐ **前端端点选择按 baseUrl 判定，不按 provider**（2026-09-14）
- 契约文档：`docs/gateway-endpoint-contracts.md`、`docs/gateway-mapping-design.md`、`IMAGE-ASYNC-TASKS-CONTRACT.md`

