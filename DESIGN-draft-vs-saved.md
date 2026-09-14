# flovart · 持久化分层设计：草稿自动 + 正式手动（Draft / Saved）

> 飞哥 2026-09-14 拍板。前置文档：`DESIGN-cloud-persistence.md`（M5 全云端已落地）。  
> 本文只解决「自动同步已够用，但缺草稿/正式之分 + 误删无保护」这一层。

---

## 0. 一句话结论

**保留现有全自动同步作为「草稿层」，在其上叠加用户主动触发的「正式版本层」，并给正式版本加软删除回收站。**

- 草稿：维持现有 800ms 防抖自动同步（飞哥拍板不改频率），跨设备可续做。
- 正式：用户点「保存到云端」→ 生成带名字/时间的快照，**永不自动删除**。
- 删除：正式版本走软删除（`deleted_at`），可恢复；草稿可直接硬删。

---

## 1. 现状盘点（改造前的准确地图）

| 能力      | 现状                                                                              | 代码位置                           |
| ------- | ------------------------------------------------------------------------------- | ------------------------------ |
| 本地主存    | IndexedDB（zustand persist，按 uid 命名空间隔离）                                         | `workflow/store.ts`            |
| 工作流云同步  | **全自动**双向：登录 pullOnce 合并 + 改动 800ms 防抖 push                                     | `services/cloudSync.ts`        |
| 生成历史云镜像 | 整体覆盖 `scope=history/doc_key=generations`，最近 18 条                                | `services/historyCloudSync.ts` |
| 素材字节    | 上传 `cloud_media`，项目快照存 `cloudMediaKey`                                          | `cloudSync.ts` → `media.ts`    |
| BFF 存储  | `cloud_docs`（PG）`MAX_DOC_BYTES=5MB` + `cloud_media`（对象存储）`MAX_MEDIA_BYTES=50MB` | `app/cloudstore.py`            |
| BFF API | GET/PUT/DELETE/list，`base_revision` 乐观锁（409）                                    | `app/routers/cloud.py`         |
| 删除语义    | **硬删除**，直接 DELETE，多端同步消失，无回收站                                                   | `cloudSync.ts:83`              |

**核心缺口**：所有项目一视同仁，没有「这只是草稿」和「这是我的作品」的区别；删除不可逆。

---

## 2. 目标模型

### 2.1 两层结构

|         | 草稿层（Draft）                                | 正式层（Saved）                                   |
| ------- | ----------------------------------------- | -------------------------------------------- |
| 触发      | 自动（800ms 防抖，现状不变）                         | 用户点击「保存到云端」                                  |
| 云端位置    | `scope=projects`，doc_key = 项目 id（**同现状**） | `scope=projects`，doc_key = `<项目id>@v<序号>`（新） |
| 是否进正式列表 | ❌ 不进                                      | ✅ 进                                          |
| 自动删除    | 可被清理策略回收                                  | ❌ 永不自动删（仅软删除）                                |
| 换设备可见   | ✅ 可继续编辑                                   | ✅ 明确归档                                       |
| 命名      | 项目 title                                  | 显式命名（默认取 title + 时间戳）                        |

### 2.2 为什么正式版用独立 doc_key 而不是同 doc 加版本数组

`cloud_docs` 单文档上限 **5MB**，工作流快照含 nodes/connections/sessions，本来就不小。  
同 doc 内塞版本数组会**线性逼近上限**且每次 PUT 全量重写，不可持续。  
用独立 doc_key 天然享受：读单个版本只拉一份、删除独立、互不干扰。

### 2.3 数据形状

```jsonc
// 草稿（现状不变，doc_key = 项目 id）
// 新增一个可选字段标记它「已是从某正式版衍生的草稿」，用于 UI 提示
{
  "id": "proj-abc",
  "title": "海报设计",
  "nodes": [...],
  "draftOfVersion": "v3",        // 可选：当前草稿基于哪个正式版
  "updatedAt": "2026-09-14T..."
}

// 正式版本（新，doc_key = "proj-abc@v3"）
{
  "id": "proj-abc@v3",
  "sourceProjectId": "proj-abc",
  "version": 3,
  "label": "配色定稿",             // 用户命名
  "snapshot": { ...WorkflowProject 的完整快照 },
  "savedAt": "2026-09-14T...",
  "deletedAt": null               // 软删除标记
}
```

> 注：正式版 payload 里嵌 `snapshot` 而非平铺，是为了跟「草稿 doc 就是 WorkflowProject 本身」区分开，避免前端 `normalizeWorkflowProject` 误判。

---

## 3. BFF 改造

### 3.1 新增：软删除支持

现状 `doc_delete` 是硬删。正式版需要可恢复，两种做法：

- **方案 1（推荐，改动小）**：不动 `cloud_docs` 表，正式版**软删就在 payload 里写 `deletedAt`**，用 PUT 覆盖。
  - 列表接口默认过滤 `deletedAt != null`，加 query `?include_deleted=1` 返回回收站。
  - 优点：**零 BFF DDL 改动**，复用现有 API。
  - 缺点：回收站数据仍占配额；需要前端配合过滤。
- **方案 2**：`cloud_docs` 加 `deleted_at` 列 + 索引。
  - 优点：语义干净、配额可扣减。
  - 缺点：需要迁移脚本（PG + 本地 SQLite 两套后端都要改，`app/db.py` + `app/cloudstore.py`）。

> 建议 v1 用**方案 1**（零 DDL），等回收站真的被用起来、配额成为问题时再迁到方案 2。

### 3.2 新增端点（很小）

```
GET /api/me/docs/{scope}?include_deleted=1   # 回收站列表（复用现有接口加参数）
```

其余全部复用现有 GET/PUT/DELETE —— **正式版就是一个普通 doc**，不需要新表新接口。

### 3.3 配额

正式版本会持续占空间。现状已有 `GET /api/me/storage/overview` 返回 `bytes_used / quota_bytes`。

- v1 不做额外限制，仅在 UI 展示用量。
- 后续可加「正式版数量上限」或「回收站 30 天自动清理」。

---

## 4. 前端改造

### 4.1 云同步引擎（`services/cloudSync.ts`）

**不改草稿逻辑**（800ms / pullOnce / dirty 集合全部保留）。  
只做两件事：

1. **列表分区**：拉取 `scope=projects` 时，把 `doc_key` 含 `@v` 的识别为正式版，与草稿分开维护。
2. **正式版不参与走 dirty 自动推**：正式版创建走显式 API 调用，不进 `dirtyIds`。

新增导出函数：

```ts
// 把当前项目状态存为一个正式版本
export async function saveProjectVersion(projectId: string, label?: string): Promise<string>

// 列出某项目的所有正式版本
export async function listProjectVersions(projectId: string): Promise<ProjectVersion[]>

// 从某个正式版本恢复到当前草稿（覆盖草稿，需用户确认）
export async function restoreProjectVersion(versionDocKey: string): Promise<void>

// 软删除 / 恢复
export async function softDeleteProjectVersion(versionDocKey: string): Promise<void>
export async function restoreDeletedProjectVersion(versionDocKey: string): Promise<void>
```

### 4.2 UI 入口

| 位置      | 交互                                   |
| ------- | ------------------------------------ |
| 工作流顶栏   | 「保存到云端」按钮（现行是否有？如无则新增）→ 弹命名框 → 生成 vN |
| 项目列表    | 条目区分「草稿」/「已保存 N 个版本」徽标               |
| 版本面板（新） | 列出该项目所有正式版：时间 / 名称 / 恢复 / 删除         |
| 回收站（新）  | 设置页或项目列表底部入口，列出软删除的正式版，可恢复           |

### 4.3 换设备恢复路径（关键，别退化）

1. 登录 → `pullOnce` 照旧拉草稿（**跨设备续做能力不变**）。
2. 正式版列表单独拉（`GET scope=projects` 全量，前端按 `@v` 过滤）。
3. 打开某个正式版 → 只 GET 那一份 doc，展示只读预览 → 点「恢复为草稿」才覆盖本地。

---

## 5. 里程碑拆分

| 阶段     | 内容                                                                                                         | 预估      |
| ------ | ---------------------------------------------------------------------------------------------------------- | ------- |
| **P1** | 前端：正式版数据结构 + `saveProjectVersion` / `listProjectVersions` / `restoreProjectVersion`（纯前端 + 现有 API，零 BFF 改动） | 0.5 天   |
| **P2** | UI：顶栏保存按钮 + 版本面板 + 命名框                                                                                     | 0.5-1 天 |
| **P3** | 软删除：payload 内 `deletedAt` + 列表过滤 + 回收站 UI                                                                  | 0.5 天   |
| **P4** | 回归：跨设备验证（A 机保存 → B 机可见可恢复）+ 历史/素材路径不回归                                                                     | 0.5 天   |

> 合计约 **2-2.5 天**（零 BFF DDL，风险低）。

---

## 6. 明确不做（v1 范围外）

| 项               | 原因                    |
| --------------- | --------------------- |
| 生成历史上限调整        | 飞哥拍板 18 条够用           |
| 草稿同步降频          | 飞哥拍板维持 800ms          |
| 回收站自动清理策略       | 等用量成为真问题再加            |
| 正式版 diff / 版本对比 | 需要 diff 引擎，价值待验证      |
| 多端并发编辑冲突 UI     | 现状 last-write-wins 够用 |



---

## 7. 风险

| 风险                        | 影响    | 对策                                                          |
| ------------------------- | ----- | ----------------------------------------------------------- |
| 正式版持续占配额，用户无感涨满           | 保存失败  | UI 展示用量 + 空回收站提示；预留配额限制入口                                   |
| `@v` 后缀若被用户手工建成项目名，与正式版撞车 | 列表错乱  | doc_key 生成时校验：草稿 id 不含 `@`；`_SCOPE_KEY_RE` 已允许 `@`，但前端生成时规避 |
| 恢复正式版覆盖当前草稿，用户丢未保存改动      | 数据丢失  | 恢复前强制确认 + 弹「当前草稿有未保存改动，是否先存一版？」                             |
| 素材（cloud_media）与正式版快照不同步  | 恢复后图裂 | 正式版保存时复用 `ensureProjectMediaUploaded`，保证快照内引用可解析            |
