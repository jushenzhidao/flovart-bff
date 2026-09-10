# flovart-bff · 全云端持久化设计（M5）

> 目标：hosted 在线模式下，用户**拖入的素材、生成的图片/视频/音频、Workflow 草稿、生成历史、资产库**全部持久化到服务端，跨浏览器 / 跨设备可恢复。
> 范围：`D:\code\flovart-web` 存储层替换 + `D:\code\flovart-bff` 新增云端数据面。
> 状态：**待飞哥 review**（v1）

---

## 1. 现状盘点（改造前的地图）

Flovart 本体是「本地优先」应用，数据全部落在浏览器本地持久化：

| 域 | 持久化载体 | 关键文件 | 数据形态 |
|---|---|---|---|
| Workflow 项目草稿（节点/连线/参数/视图） | localforage → IndexedDB `flovart_workflow_v2` | `components/workflow/store.ts` / `storage.ts` | JSON（projects 数组，**按 store key 存 JSON 字符串**） |
| 工作流媒体 Blob（拖入图/生成图/视频/音频） | localforage → IndexedDB `workflow_media` | `components/workflow/storage.ts` / `media.ts` | **原始 Blob**（可能几十 MB） |
| 图片（dataUrl 缓存） | IndexedDB `flovart_images` | `utils/imageDB.ts` | base64 dataUrl |
| 视频/音频 | IndexedDB | `utils/mediaDB.ts` / `mediaIndexedDB.ts` | Blob |
| 素材库（AssetLibrary） | IndexedDB / localforage | `utils/assetStorage.ts` | JSON + Blob |
| 生成历史（右侧抽屉） | localforage | `utils/generationHistory.ts` | JSON |
| 用户 API Key 配置（BYOK） | localforage keyVault | `utils/keyVault.ts` | JSON |

**Media 引用形态**（决定改造难度）：
- workflow 节点 media 引用 = `storageKey`（IndexedDB 键）或 `href`（blob: / data: / http(s) 直链）
- 渲染时经 `useWorkflowMediaUrl` / `loadWorkflowMediaBlob(storageKey?, href?, artifactRef?)` 统一加载
- 判断函数：`isIdbRef('idb:xxx')`、`isDataUrl`、`isFetchableMediaHref`

---

## 2. 目标架构（云端数据面）

```
┌─ 浏览器（flovart-web）──────────────────────────────┐
│  UI / 节点画布 / 渲染层（不动）                        │
│  ┌────────────────────────────────────────────────┐ │
│  │ CloudStorage 适配层（新增，替换原本地持久化调用点） │ │
│  │  - 读：先查本地缓存 → miss 则 GET /api/me/...    │ │
│  │  - 写：本地立即落 + 防抖同步到云端                 │ │
│  └────────────────────────────────────────────────┘ │
└──────────────────┬───────────────────────────────────┘
                   │ /api/me/*  (同源，已有 session)
┌──────────────────▼───────────────────────────────────┐
│ BFF（flovart-bff） 云端数据面（新增 app/cloudstore）   │
│  KV 域（JSON 文档）：projects/assets/history/keyvault │
│  Blob 域（媒体文件）：上传 → data/media/<uid>/<key>    │
│  鉴权：require_session（uid 来自 new-api）            │
│  配额：每用户容量上限，v1 固定默认（可后续接积分）      │
└──────────────────┬───────────────────────────────────┘
                   │ SQLite（新增，替换 JSON 原子写用于云数据）
```

### 2.1 BFF 存储选型（2026-09-07 反转：外部对象存储 + PostgreSQL）

现状：`store.py` 是 JSON 原子写（hewapi 风格）——适合低频运维状态（注册赠送幂等 / 管理员 PAT 缓存），**不适合多用户结构化云数据，也不落本地磁盘存用户产物**。

M5 采用 **「对象存储（字节）+ PostgreSQL（元数据）」** 双后端（均在 `app/` 下）：

| 层 | 载体 | 说明 |
|---|---|---|
| 字节层 | 外部对象存储 **OSS/COS/S3**（boto3 S3 协议，见 `oss.py`） | 图片/视频/音频二进制；BFF 联网 `put_object` 写入，**不落本地磁盘**；前端读取走 presigned URL（`/api/me/media/{key}` 307 重定向，需 bucket 配 CORS）。键布局 `{prefix}/users/{uid}/media/{key}` |
| 元数据层 | **PostgreSQL**（asyncpg 连接池，见 `db.py`） | `cloud_docs`(KV JSON, PK uid+scope+doc_key, revision 乐观锁) / `cloud_media`(媒体索引 uid/key/mime/size, 不含字节) / `cloud_request_log`(用户请求日志：request_id/payload/task_id/gateway_request_id/status/mode/result_json，PK request_id，索引 uid+created_at) / 配额概览。多副本共享同一 PG，无需本地文件锁 |
| 运维状态 | 本地 **JSON 原子写**（`store.py`，沿用） | `signup_bonus.json` / `admin_cred.json`（含密钥，不进对象存储） |

- `cloudstore.py` 是组合层：`META = PgMeta() if USE_PG else LocalMeta()`、`BLOB = OssBlob() if OSS_ENABLED else LocalBlob()`；调用方只认其异步 API。
- 未配置云时回落本地兜底（本地 SQLite 元数据 + 本地文件字节），保证开发机零依赖可跑。
- boto3 + asyncpg 现为硬依赖（见 `requirements.txt`）。

### 2.2 BFF API 契约（全部挂 require_session）

```
# —— KV 域（JSON 文档，scope: projects/assets/history/settings …）——
GET    /api/me/docs/{scope}/{doc_key}        → {doc, revision, updated_at}
PUT    /api/me/docs/{scope}/{doc_key}        body {payload, base_revision?}
                                              → 200 {revision} | 409 Conflict（base_revision 不匹配）
DELETE /api/me/docs/{scope}/{doc_key}

# —— Blob 域（媒体文件）——
POST   /api/me/media                          multipart {file, kind, mime}
                                              → {media_key, size, url}
GET    /api/me/media/{media_key}              → 二进制流（Content-Type 原 mime）
DELETE /api/me/media/{media_key}

# —— 配额 / 恢复 ——
GET    /api/me/storage/overview               → {doc_count, media_count, bytes_used, quota_bytes}
```

约定：
- doc_key = 前端项目 id（如 `proj_xxx`）或素材库 key；media_key = 服务端生成的 uuid（**不暴露本地路径**）
- 大小限制：单文件 ≤ 50MB（v1 不做分片，Flovart 媒体一般 <20MB）；单用户配额默认 **2GB**（v1 常量，后续可接积分扩容）
- 409 Conflict 语义：前端冲突 → 保留本地副本并提示「远端已有更新，另存为副本 / 覆盖」，v1 提供「保存为新项目」逃生口

### 2.3 前端 CloudStorage 适配层

新增 `services/cloudStorage/`，暴露与现有存储**同签名**的接口（业务零改动）：

| 现有调用方 | 现有 API | cloud 适配 |
|---|---|---|
| `components/workflow/store.ts`（zustand persist）| `workflowStorage.get/set` | `cloudDocStorage.get/set(scope='projects')`，key=项目 id |
| `components/workflow/media.ts` | `workflowMediaStorage` | `cloudMediaStorage`：blob 读写 |
| `utils/imageDB.ts` / `mediaDB.ts` / `mediaIndexedDB.ts` | `put/get/delete*` | 内部改走 cloud adapter（本地 cache + 云端） |
| `utils/assetStorage.ts` | — | KV（doc JSON 云）+ Blob 云 |
| `utils/generationHistory.ts` | — | KV |
| `utils/keyVault.ts` | — | KV（**但 hosted 平台 Key 由 BFF 签发，BYOK 若在 hosted 需谨慎**——见 §4 风险） |

**读写策略**：
1. **读**：本地 IndexedDB 缓存优先（毫秒级渲染）→ miss → `GET /api/me/...` 并回填缓存
2. **写**：先落本地缓存（即时可用）→ 防抖 800ms → 同步云端（带 base_revision 乐观锁）
3. **恢复**：hosted 登录成功后触发 `syncPull()` —— 拉取 `GET /api/me/storage/overview` + 全部 doc 列表，覆盖本地缓存；blob 按需懒拉（不预热全部大图）
4. **离线**：写入本地缓存的挂起队列，`navigator.onLine` 恢复后重放
5. 生成媒体下载：目前 Flovart 渲染 `data:` / `blob:` / 远端直链；云化后节点媒体应优先 `GET /api/me/media/{key}`，生成接口下载源图后本地仍可回退

### 2.4 关键改造点（Media 引用形态）

现有 `media.ts` `loadWorkflowMediaBlob` 识别 3 类引用：idb ref / dataUrl / fetchable href。云化后：
- 节点 media 新增第 4 类引用 `cloud:<media_key>`
- `isCloudMediaRef` + `loadCloudMediaBlob(media_key)` 分支
- 上传/生成结果写回时把 blob 存云，media 引用写 `cloud:<key>`
- `useWorkflowMediaUrl` 渲染优先 cloud 引用 → 下载后 objectURL（与现在 idb 加载后 objectURL 同款）

---

## 3. 里程碑拆分

| 阶段 | 内容 | 预计 |
|---|---|---|
| **M5.1** | BFF 云数据面：SQLite 表 + cloudstore 模块 + 上述 6 个 API + 配额校验 + 冒烟测试 | 0.5-1 天 |
| **M5.2** | 前端 CloudStorage adapter：`cloudDocStorage`/`cloudMediaStorage` + KV/blob 读写 + 本地缓存回填 | 1-1.5 天 |
| **M5.3** | 接入现有存储调用点：workflow store / media.ts / generationHistory；media 引用支持 `cloud:` | 1 天 |
| **M5.4** | 同步引擎：防抖保存 / 乐观锁 409 / 离线队列重放 / 登录后 syncPull 恢复 | 1 天 |
| **M5.5** | 素材库/资产库 + keyVault(BYOK) 云化（hosted 模式下用户 key 仍由 BFF 提供，BYOK 走本地） | 0.5 天 |
| **M5.6** | 配额 UI（用量条）+ 冲突处理 UI + 全链路回归（跨设备验证） | 0.5 天 |

> 总工期约 4-6 天（不含联调返工）。

---

## 4. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 工作流内 media 引用形态多样（idb/dataURL/直链/云），漏改一路径 | 某些节点图加载不出来 | 统一收敛到 `media.ts` 单一加载入口，改一处生效；回归覆盖 image/video/audio 三类 |
| 乐观锁 409 频繁打断用户 | 保存失败体验差 | v1 默认**last-write-wins**（静默覆盖 + revision 推进），仅同一项目两设备同时改时提示「另存副本」|
| Blob 直接 http 下载大文件 | 首屏慢 | blob 懒加载（现状就是 objectURL 按需），列表不预热 |
| BYOK Key 云化 = 用户密钥明文上服务端 | 泄露面 | **hosted 模式下 BYOK 不云化**；平台 Key 由 BFF/new-api 签发，用户自添 key 仍只在本地 IndexedDB |
| SQLite 并发写（单进程 uvicorn） | 死锁/阻塞 | WAL + 单写连接池；仍保持单 worker 部署（与 hewapi 同哲学）|
| 清缓存即丢数据（本地优先遗留心智） | 用户困惑 | syncPull 登录即恢复 + 顶部「云同步✓」小标识 |

---

## 5. 需要飞哥拍板

1. **配额默认值**：v1 固定 2GB/人？还是按积分购买（每 100 积分 +512MB 之类）？
2. **多端同时编辑同一项目**：v1 直接 last-write-wins（后保存者覆盖）可接受？还是必须弹冲突？
3. **media 是否也懒同步**（本地留着，只同步 JSON 文档 + 引用 media_key 未上传时补传）——还是「素材必须全量在云」（跨设备立刻可见全部原图）？前者省流量，后者真全云端。
4. BFF 部署的 `data/` 目录是否需要独立挂载/备份策略（媒体文件会增长）？
