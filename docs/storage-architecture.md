# Flovart BFF 持久化存储 — 框架与技术方案

> 适用模块：flovart-bff（FastAPI 后端，面向「在线创作站」用户数据 / 媒体产物 / 调用日志）
> 拍板时间：2026-09-07
> 母版参考：hewapi-bff

---

## 1. 目标与约束

| 目标 | 说明 |
| --- | --- |
| 产物不落本地磁盘 | 图片 / 视频 / 音频二进制必须写远端对象存储，BFF 自身服务器不保留用户文件 |
| 元数据结构化 | 文档索引、媒体索引、配额、调用日志用关系型数据库管理，便于分页 / 聚合 / 对账 |
| 多副本可共享 | 后端可横向扩副本，存储层不依赖本地文件锁（摒弃 SQLite WAL 单文件方案） |
| 开发兜底 | 未配置云时本地 SQLite + 本地文件自动降级，保证本机 `uvicorn` 一键跑通 |
| 零前端 CORS 麻烦 | 前端只跟 BFF 同源 `/api`，读媒体走 BFF 307 重定向到 presigned URL |

**核心边界（飞哥拍板）**：
- 字节（媒体二进制）→ 外部对象存储（OSS / COS / S3，统一 boto3 S3 协议）
- 元数据（KV 文档 + 媒体索引 + 配额 + 请求日志）→ PostgreSQL（asyncpg 连接池）
- 本地 SQLite / 本地文件仅作**未配置云时的开发兜底**，生产必须启用云后端

---

## 2. 总体架构（分层）

```
┌──────────────────────────────────────────────────────────────┐
│  前端（flovart-web） 仅同源 /api，带 session Cookie            │
└───────────────┬──────────────────────────────────────────────┘
                │  /api/me/media/{key}  ── 307 ──▶ presigned URL (OSS)
┌───────────────▼──────────────────────────────────────────────┐
│  BFF 路由层  routers/cloud.py · routers/tasks.py               │
├───────────────┬──────────────────────────────────────────────┤
│  组合数据面    app/cloudstore.py   （全部 async，调用方只认此层）│
│   ├─ META : PgMeta ──▶ PostgreSQL (asyncpg)                    │
│   │           LocalMeta ──▶ 本地 SQLite（兜底）                │
│   └─ BLOB : OssBlob ──▶ OSS / COS / S3 (boto3)                │
│               LocalBlob ──▶ 本地文件（兜底）                   │
├───────────────┴──────────────────────────────────────────────┤
│  存储适配层                                                    │
│   ├─ app/oss.py     （boto3 S3 协议，三大云靠 env 差异消除）   │
│   └─ app/db.py      （asyncpg 连接池 + 建表迁移）              │
└───────────────┬──────────────────────────────────────────────┘
                │
   ┌────────────▼──────────┐        ┌──────────────────────────┐
   │  PostgreSQL（元数据）  │        │  对象存储（字节）          │
   │  cloud_docs            │        │  {prefix}/users/{uid}/     │
   │  cloud_media           │        │     media/{media_key}      │
   │  cloud_request_log     │        └──────────────────────────┘
   └────────────────────────┘
```

**关键设计**：`routers/*` 与 `tasks.py` **只调用 `cloudstore` 公开异步 API**，绝不直接 import `oss` / `db`。后端切换（云 ↔ 本地）在 `cloudstore` 顶层 `META` / `BLOB` 两个组合对象完成，调用方零改动。

---

## 3. 两层存储后端

### 3.1 字节层 — 外部对象存储（`app/oss.py`）

- 协议：**boto3 S3 协议**，天然兼容阿里云 OSS、腾讯云 COS、AWS S3、MinIO。
- 三大云差异**只靠 env 消除**，不写分支代码：
  - 腾讯云 COS：`OSS_ENDPOINT=https://cos.<region>.myqcloud.com`，`OSS_ADDRESSING_STYLE=virtual`
  - 阿里云 OSS：`OSS_ENDPOINT=https://oss-<region>.aliyuncs.com`，`OSS_ADDRESSING_STYLE=path`
  - AWS S3：不填 `OSS_ENDPOINT`，只填 `OSS_REGION`，`OSS_ADDRESSING_STYLE=auto`
- 写入：前端经 BFF 上传 → BFF `put_object` 写 OSS（**无 CORS 需求**，服务端到服务端）。
- 读取：`head_object` 取元数据 → `generate_presigned_url` 生成临时 URL → 路由层 **307 重定向**给前端（前端域名需 bucket 配置 GET CORS）。
- 键布局（统一前缀隔离多环境 / 多租户）：
  - 媒体：`{OSS_PREFIX}/users/{uid}/media/{media_key}`
  - 文档 KV：当前 KV 文档落 PG `cloud_docs`（不占 OSS 字节），`oss.doc_key()` 预留接口未启用。
- 健壮性：boto3 `Config` 设 `signature_version=s3v4`、`retries=3`、`addressing_style` 按云配置。

### 3.2 元数据层 — PostgreSQL（`app/db.py`）

- 驱动：**asyncpg 纯异步连接池**（`min_size=1, max_size=10, command_timeout=30`）。
- 生命周期：`main.py` 的 lifespan 启动时 `db.init_pool()`（建池 + 建表），关闭时 `db.close_pool()`。
- `/readyz` 探针含 `db.ping()`，PG 不可达时健康检查失败，便于 K8s 摘除。
- 仅存结构化元数据，**不存任何字节**：媒体表只存 `media_key / mime / size` 索引，字节在 OSS。

---

## 4. 抽象层设计（`app/cloudstore.py`）

两个抽象基类，调用方依赖抽象而非实现：

| 抽象 | 实现 | 触发条件 |
| --- | --- | --- |
| `_Meta`（KV / 索引 / 日志 / 配额） | `PgMeta` / `LocalMeta` | `config.USE_PG` |
| `_Blob`（字节读写） | `OssBlob` / `LocalBlob` | `config.OSS_ENABLED` |

顶层组合（模块加载时决定）：
```python
META: _Meta = PgMeta() if config.USE_PG else LocalMeta()
BLOB: _Blob = OssBlob() if config.OSS_ENABLED else LocalBlob()
```

公开异步 API（调用方唯一入口）：
```
doc_get / doc_put / doc_delete / doc_list        # KV JSON
media_put / media_get / media_delete            # 字节 + 索引
storage_overview                                 # 配额 / 用量
request_log_put / request_log_update / request_log_get / request_log_list  # 调用日志
```

`media_put` 是组合写：先 `BLOB.put`（字节落 OSS），成功后再 `META.media_index_put`（索引落 PG），返回统一视图 `{media_key, size, mime, url}`；`media_delete` 删索引 + 删字节，二者任一成功即返回 True（幂等容错）。

---

## 5. 数据模型（PostgreSQL 表）

### 5.1 `cloud_docs` — KV 文档（projects / history / assets / settings）
| 列 | 类型 | 说明 |
| --- | --- | --- |
| uid | BIGINT | 用户 ID（PK 一部分） |
| scope | TEXT | 业务域（如 `projects` / `history`） |
| doc_key | TEXT | 文档键 |
| payload | TEXT | JSON 字符串（文档主体） |
| revision | INTEGER | 乐观锁版本号，`ON CONFLICT` 自增 |
| updated_at | TIMESTAMPTZ | 更新时间 |
| **PK** | (uid, scope, doc_key) | |

写入用 `INSERT ... ON CONFLICT DO UPDATE SET payload=..., revision=revision+1`，实现乐观并发；单文档上限 `MAX_DOC_BYTES=5MB`。

### 5.2 `cloud_media` — 媒体索引（不含字节）
| 列 | 类型 | 说明 |
| --- | --- | --- |
| uid | BIGINT | 用户 ID |
| media_key | TEXT | 主键，对应 OSS 对象键（uuid4 hex） |
| kind | TEXT | 媒体类型（media / doc / ...） |
| mime | TEXT | Content-Type |
| size | BIGINT | 字节数（用于配额统计） |
| created_at / updated_at | TIMESTAMPTZ | |
| **索引** | idx_cloud_media_uid | 按用户聚合用量 |

### 5.3 `cloud_request_log` — 用户调用日志（拉日志对齐网关）
| 列 | 类型 | 说明 |
| --- | --- | --- |
| request_id | TEXT | 主键，BFF 提交即生成，轮询键 |
| uid | BIGINT | 用户 |
| kind / provider / model | TEXT | 调用类型 / 供应商 / 模型 |
| payload_json | TEXT | **调用模型时的完整请求结构 + 参数**（对齐网关） |
| task_id | TEXT | 异步任务 ID（async 才有） |
| gateway_request_id | TEXT | 网关全站日志 request_id，用于 ↔ 网关 `/api/log` 对齐 |
| status | TEXT | submitted / running / succeeded / failed |
| mode | TEXT | async / sync |
| result_json | TEXT | 任务结果（成功时） |
| created_at / updated_at | TIMESTAMPTZ | |
| **索引** | idx_reqlog_uid_created | 按用户 + 时间倒序拉列表 |

---

## 6. 配置开关与部署参数

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `OSS_ENABLED` | `False` | 关 → 回落本地文件（开发） |
| `OSS_ENDPOINT` / `OSS_REGION` | 空 / ap-guangzhou | 按云填 |
| `OSS_BUCKET` | 空 | bucket 名（必填才启用 OSS） |
| `OSS_ACCESS_KEY` / `OSS_SECRET_KEY` | 空 | AK/SK 访问密钥（必填） |
| `OSS_PREFIX` | `flovart` | 对象键前缀，隔离多环境 / 多租户 |
| `OSS_ADDRESSING_STYLE` | `auto` | virtual(path) / path / auto |
| `OSS_PRESIGN_TTL` | `3600` | presigned URL 有效期（秒） |
| `OSS_ENFORCE_QUOTA` | `True` | 是否强制配额校验 |
| `OSS_QUOTA_BYTES` | `2GB` | 每用户配额（0=不限） |
| `POSTGRES_DSN` | 空 | 完整 DSN（优先）；缺省用分项拼装 |
| `POSTGRES_HOST/PORT/USER/PASSWORD/DB` | localhost/5432/... | 分项拼装用 |
| `USE_PG` | 由 DSN 推导 | DSN 非空即 `True` |
| `BFF_DATA_DIR` | 仓库 `data/` | 本地兜底 SQLite / 文件根目录 |
| `MAX_DOC_BYTES` | 5MB | 单 KV 文档上限（代码常量） |
| `MAX_MEDIA_BYTES` | 50MB | 单媒体文件上限（代码常量） |

---

## 7. 读写路径与请求日志

1. **上传媒体**：前端 POST `/api/me/media`（multipart）→ `cloudstore.media_put` → 写 OSS + 写 PG 索引 → 返回 `/api/me/media/{key}`。
2. **读取媒体**：前端 GET `/api/me/media/{key}` → `cloudstore.media_get`（查 PG 索引 + OSS head）→ 307 重定向到 presigned URL。
3. **KV 文档**：`doc_put/doc_get/doc_list` 直接落 / 取 PG `cloud_docs`（`LocalMeta` 落本地 SQLite）。
4. **调用日志（拉日志对齐）**：图片 / 视频任务提交时 `request_log_put`（写 request_id + 完整 payload）；轮询更新时 `request_log_update`（回填 task_id / gateway_request_id / status / result）；前端 `GET /api/me/requests` 列表 + `GET /api/me/requests/{id}` 详情，用于把 BFF 落库的请求结构与网关 `/api/log` 对齐排障。

---

## 8. 兜底策略（开发态）

| 开关 | 云后端 | 本地兜底 |
| --- | --- | --- |
| `USE_PG=False` | PostgreSQL | `LocalMeta`：本地 SQLite（`{DATA_DIR}/flovart_cloud.db`，WAL + busy_timeout=5s） |
| `OSS_ENABLED=False` | OSS / COS / S3 | `LocalBlob`：`{DATA_DIR}/media/{uid}/{key}` 本地文件 |

兜底路径同样实现 `_Meta` / `_Blob` 全部方法，调用方透明。SQLite 操作统一 `asyncio.to_thread` 包裹，避免阻塞事件循环。

---

## 9. 配额与限流

- 单文件：`MAX_MEDIA_BYTES=50MB` 硬上限。
- 用户总量：`OSS_ENFORCE_QUOTA=True` 时，`media_put` 先算 `media_bytes_used(uid)`，超 `OSS_QUOTA_BYTES` 直接拒写（返回「配额不足」）。
- 单文档：`MAX_DOC_BYTES=5MB` 硬上限。
- 上游保护：`tasks.py` 对第三方网关调用包 Semaphore 限流 + 超时 + 重试；BFF 不自身扣额度（计费由 new-api 网关负责，见计费边界约定）。

---

## 10. 安全与 CORS

- **AK/SK 只在 BFF 服务端**，前端无感知，不暴露云端密钥。
- 前端读媒体走 BFF 307 → presigned URL，**需为 bucket 配置 CORS** 允许前端域名的 GET（如 `https://workbuddy.oneapis.cn`）。
- 写媒体走服务端 `put_object`，无 CORS 需求。
- DSN / AK/SK 经 env 注入，`db._mask_dsn` 日志脱敏密码。
- 请求日志按 `uid` 隔离，跨用户查询需管理员凭证（与管理后台同源 `NEWAPI_ADMIN_PAT` 通道，独立稳定）。

---

## 11. 多副本与水平扩展

- **元数据**：PostgreSQL 天然支持多副本共享同一库，`PgMeta` 无本地状态，多 BFF 副本可直接连同一 PG（无需 Redis / 文件锁）。
- **字节**：对象存储天然共享，无本地磁盘依赖。
- **任务状态**：图片异步任务在 `tasks.py` 用内存任务表（单 worker）；若多副本需换 Redis 共享任务表（架构已预留）。
- **单 worker 哲学**：当前部署单 worker，状态一致；多副本横向扩只需共享 PG + OSS（任务表可后续接 Redis）。

---

## 12. 待办与风险

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 真实 OSS bucket + PG 实例凭据注入 | ⏳ | 生产需注入 `OSS_*` / `POSTGRES_DSN` 并配 bucket CORS |
| 网关同步端点联调 | ⏳ | image-gen 默认 async；upscale / matting / split 默认 sync，待真实响应格式回填 |
| 多副本任务共享 | ⏳ | 当前内存任务表，多副本需 Redis（架构已预留） |
| 配额回收 | ⏳ | 删除媒体时 `media_bytes_used` 自动下降；无自动 GC 孤儿 OSS 对象（建议定时 `list_prefix` 对账） |

---

## 13. 文件清单（当前落地）

| 文件 | 职责 |
| --- | --- |
| `app/oss.py` | 外部对象存储适配（boto3 S3 协议，三大云 env 差异消除） |
| `app/db.py` | PostgreSQL 连接池 + 建表迁移 |
| `app/cloudstore.py` | 组合数据面（META/BLOB 抽象 + 公开 async API + 请求日志） |
| `app/config.py` | 存储相关 env 配置（`OSS_*` / `POSTGRES_*` / `USE_PG` / 上限常量） |
| `app/main.py` | lifespan：PG 池初始化 / 关闭 + `/readyz` 探针 |
| `app/routers/cloud.py` | 媒体上传 / 307 重定向读取；全部 `cloudstore` 异步调用 |
| `app/routers/tasks.py` | 图片任务提交 / 轮询 / 取消；落 `request_log_*` |
| `.env.example` | 配置样例与「每业务独立账号严禁共用」约定 |
