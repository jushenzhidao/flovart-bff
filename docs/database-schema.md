# flovart-bff 数据库结构说明

> 生产元数据库：PostgreSQL（compose 服务 `postgres`，容器名 `flovart-pg`，库/用户均 `flovart`）。
> 表由 BFF 启动时自动创建/补列（`app/db.py` 的 `SCHEMA` + `migrate()`），**零手工建表**。
> 本地开发未配 `POSTGRES_*` 时回落 SQLite（`data/flovart_cloud.db`），表结构与此完全一致。
>
> 设计核心：**PG 只存结构化元数据，创作产物的字节全部在对象存储（MinIO/OSS）**。

## 目录

- [访问方式](#访问方式)
- [表总览](#表总览)
- [1. cloud_docs —— 云端 KV 文档](#1-cloud_docs--云端-kv-文档)
- [2. cloud_media —— 媒体索引](#2-cloud_media--媒体索引)
- [3. cloud_media_shares —— 媒体分享授权](#3-cloud_media_shares--媒体分享授权)
- [4. cloud_request_log —— 调用日志](#4-cloud_request_log--调用日志)
- [不在 PG 里的数据（重要）](#不在-pg-里的数据重要)
- [常用查询速查](#常用查询速查)
- [备份与运维](#备份与运维)

---

## 访问方式

```bash
# 进交互式 psql（SSH 到服务器后）
docker exec -it flovart-pg psql -U flovart -d flovart

# 单条命令
docker exec -it flovart-pg psql -U flovart -d flovart -c "\dt"
```

> 注意：宝塔面板的 PgSQL 页签管的是宝塔自装的 PostgreSQL（daxpay 等），**看不到** flovart-pg；
> flovart-pg 刻意不发布端口，公网/宿主机不可直连。GUI 需求走 SSH 隧道（Navicat/DBeaver）。

## 表总览

| 表 | 用途 | 主键 | 备注 |
|---|---|---|---|
| `cloud_docs` | 云端 KV JSON 文档（项目/历史/资产/设置/平台服务配置） | (uid, scope, doc_key) | 前端工作区云同步的载体 |
| `cloud_media` | 媒体元数据索引 | media_key | 字节在 MinIO；**2026-09-21 起默认不限配额、按 OSS_RETENTION_DAYS 定期清理本表过期行（字节+索引一起删）** |
| `cloud_media_shares` | 媒体分享授权 | id | owner → target 的 view 授权 |
| `cloud_request_log` | 生图/生视频调用日志 | request_id | 面板「调用记录」数据源 |

---

## 1. cloud_docs —— 云端 KV 文档

前端（创作站画布、历史记录、资产、设置）的 JSON 文档按
`(uid, scope, doc_key)` 三元组存取，payload 为 JSON 文本。

| 列 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `uid` | BIGINT | — | 用户 id（new-api 侧 uid） |
| `scope` | TEXT | — | 文档域：`projects` / `history` / `assets` / `settings` … |
| `doc_key` | TEXT | — | 文档键（如项目 id） |
| `payload` | TEXT | — | JSON 内容本体 |
| `revision` | INTEGER | 1 | 版本号（乐观并发控制，写入带 revision 校验） |
| `updated_at` | TIMESTAMPTZ | now() | 更新时间 |

**主键**：`(uid, scope, doc_key)`

⚠️ **特殊行**：平台共享服务全局配置 = `uid=0, scope='platform', doc_key='services'`，
payload 里是全部平台服务定义（含模型清单）。管理端改平台服务就是改这一行。

## 2. cloud_media —— 媒体索引

用户上传/生成产物在对象存储的**索引层**。一行 = 一个对象；字节本体在 MinIO 桶。

| 列 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `media_key` | TEXT | — | **主键**，OSS 对象 key（含 prefix 路径） |
| `uid` | BIGINT | — | 归属用户（有索引 `idx_cloud_media_uid`） |
| `kind` | TEXT | `media` | 语义类型：image / video / audio / asset |
| `mime` | TEXT | `application/octet-stream` | MIME 类型 |
| `size` | BIGINT | 0 | 字节数。配额保险丝（OSS_ENFORCE_QUOTA，默认关）按本表统计；清理统计 freed 也按它 |
| `source_request_id` | TEXT | NULL | 血缘：产出的 request_id（关联 cloud_request_log，有索引） |
| `source_kind` | TEXT | NULL | 来源类型（如生成任务类型） |
| `width` | INTEGER | NULL | 图片宽 |
| `height` | INTEGER | NULL | 图片高 |
| `thumb_key` | TEXT | NULL | 缩略图对象 key |
| `deleted_at` | TIMESTAMPTZ | NULL | 软删时间（当前无写入方，保留列）。**NULL=在用；配额统计排除已软删行** |
| `created_at` | TIMESTAMPTZ | now() | 创建时间 |
| `updated_at` | TIMESTAMPTZ | now() | 更新时间 |

**索引**：`idx_cloud_media_uid (uid)`、`idx_cloud_media_src (source_request_id)`

> 读取路径：`GET /api/me/media/{media_key}` → 校验归属 → **307 重定向到 OSS presigned URL**。

## 3. cloud_media_shares —— 媒体分享授权

用户把某个媒体分享给另一个用户（素材共享）。

| 列 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `id` | TEXT | — | **主键**（随机 id） |
| `owner_uid` | BIGINT | — | 拥有者（有索引） |
| `media_key` | TEXT | — | 被分享的媒体 |
| `target_uid` | BIGINT | — | 被分享人（有索引） |
| `perm` | TEXT | `view` | 权限（目前仅只读） |
| `name` | TEXT | `''` | 分享备注名 |
| `created_at` | TIMESTAMPTZ | now() | 创建时间 |

**约束**：`UNIQUE (owner_uid, target_uid, media_key)` —— 同一对人同一媒体只一条授权。

## 4. cloud_request_log —— 调用日志

每次生图/生视频任务提交写一行，状态与结果随任务推进更新。
前端面板「调用记录」、用户调用历史查询的数据源。

| 列 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `request_id` | TEXT | — | **主键**，BFF 生成，也是前端轮询键 |
| `uid` | BIGINT | — | 用户 id |
| `kind` | TEXT | — | 任务类型：image-gen / split-layers / upscale / video-gen … |
| `provider` | TEXT | NULL | 网关/渠道标识（gateway、runninghub…） |
| `model` | TEXT | NULL | 模型名 |
| `payload_json` | TEXT | — | 提交参数快照（JSON） |
| `task_id` | TEXT | NULL | 网关异步任务 id（async 模式） |
| `gateway_request_id` | TEXT | NULL | 网关侧请求 id（对账用） |
| `status` | TEXT | `submitted` | submitted → succeeded / failed / cancelled |
| `mode` | TEXT | `async` | async / sync |
| `result_json` | TEXT | NULL | 产物结果快照（JSON，含输出 url/b64 等） |
| `created_at` | TIMESTAMPTZ | now() | 提交时间 |
| `updated_at` | TIMESTAMPTZ | now() | 最后更新时间 |

**索引**：`idx_reqlog_uid_created (uid, created_at DESC)`

---

## 不在 PG 里的数据（重要）

| 数据 | 实际位置 | 说明 |
|---|---|---|
| 用户账号/密码 | **new-api** | BFF 影子建号，BFF 自身不存任何密码 |
| 登录会话 | 加密 Cookie | AES-256-GCM（`app/security.py`），服务端零存储 |
| 用户自配 API Key | `data/user_keys.json` | JSON 文件，600 权限 |
| 注册口令/赠送状态 | `data/signup_state.json` | promo 模块 |
| 管理员凭据缓存 | `data/`（BFF_ADMIN_CRED_FILE） | new-api 管理员 PAT 快照 |
| 图片/视频字节 | MinIO/OSS 桶 | PG 只存索引 |
| 平台服务配置 | cloud_docs 表 `uid=0/platform/services` | 不是独立表 |

## 常用查询速查

```sql
-- 某用户的调用记录（最近 20 条）
SELECT request_id, kind, model, status, created_at
FROM cloud_request_log WHERE uid = 143
ORDER BY created_at DESC LIMIT 20;

-- 某用户的媒体占用（含对象数与总 MB，排除已删）
SELECT count(*) AS objects,
       round(sum(size)/1048576.0, 1) AS mb
FROM cloud_media WHERE uid = 143 AND deleted_at IS NULL;

-- 全站占用 TOP 10（配额已默认关闭，此查询用于观察清理前的磁盘分布）
SELECT uid, count(*) AS n, round(sum(size)/1048576.0, 1) AS mb
FROM cloud_media WHERE deleted_at IS NULL
GROUP BY uid ORDER BY mb DESC LIMIT 10;

-- 某次任务的产物血缘（request → 媒体文件）
SELECT media_key, kind, size, created_at
FROM cloud_media WHERE source_request_id = '<request_id>';

-- 平台服务配置（看模型清单）
SELECT payload FROM cloud_docs
WHERE uid = 0 AND scope = 'platform' AND doc_key = 'services';
```

## 备份与运维

- **建表/迁移**：BFF 启动自动执行（`init_pool()` → `migrate()`），新列用
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 补齐，升级即生效，无需手工 SQL。
- **备份对象**：`flovart-pgdata` Docker 卷（或 `pg_dump`）。宝塔自动备份**不覆盖**本库。
- **备份命令示例**：
  ```bash
  docker exec flovart-pg pg_dump -U flovart flovart | gzip > flovart-pg-$(date +%F).sql.gz
  ```
- 字节恢复 = 恢复 MinIO/OSS 数据 + 本表元数据，两者需配套。

---
*依据代码：`app/db.py`（SCHEMA/migrate）、`app/cloudstore.py`（Pg/Local 双实现）。更新表结构时请同步本文件。*
