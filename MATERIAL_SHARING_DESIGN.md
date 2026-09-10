# 素材共享设计文档（Material Sharing）

> 方案：**混合**（Workspace 工作室为主 + 指定用户 + 公开链接补充）
> 拍板：飞哥 2026-09-09｜状态：设计稿（待评审后落地）

---

## 1. 背景与现状

探查结论（已读代码确认）：

- **BFF 完全没有共享概念**。`cloud_media(uid, media_key, kind, mime, size)` 与 `cloud_docs(uid, scope, doc_key)` 都是按 `uid` 严格私有隔离，无 `shared_with` / `workspace_id` 字段。
- **前端素材真正落在 BFF**。素材上传走 `uploadMediaToBff` → `cloud_media(uid=session.uid)`，字节在 OSS。所以"共享"可以直接基于现有 `cloud_media` 做授权视图，无需迁移。
- **`App.tsx:303` 的 `workflowSharedMedia` 不是真共享**——它只是把本地 `generationHistory` 桥接给画布拖拽，仅本机可见。
- **已有引用链路可复用**：`MentionList`（个人素材库分区）、`FlovartAgentPanel` 的 `referenceGroups.assets`（来自 `assetLibrary.items`）、`AssetLibraryBrowser`（素材库浏览）都走 `AssetItem` 数据源，共享素材只需并入这些数据源并加 `sourceType` 区分。

---

## 2. 目标 / 非目标

**目标**
- 用户可把自己素材共享给三类目标：① 整个工作室（Workspace）② 指定用户（uid）③ 公开链接（任意持链接者）。
- 共享粒度：单素材（MVP）→ 文件夹合集（P2）。
- 权限：查看·引用（拖入自己画布）/ 下载（保存到我的素材库）（MVP）→ 管理（撤销/改权限）（P2）。
- **字节不复制**：OSS 仍归 owner 的 key 空间，共享只存"授权视图"。

**非目标（本期不做）**
- 共享画布 / 共享项目（只共享素材，不共享 workflow project）。
- 协同实时编辑同一素材。
- 跨 flovart 实例（多业务隔离，每个业务独立 BFF/独立 OSS）。

---

## 3. 数据模型（PostgreSQL，复用 `app/db.py` asyncpg 池）

新增三张表（workspace 管理 + 共享授权）：

```sql
-- 工作室
CREATE TABLE cloud_workspaces (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        TEXT NOT NULL,
  owner_uid   TEXT NOT NULL,
  created_at  TIMESTAMPTZ DEFAULT now(),
  updated_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_ws_owner ON cloud_workspaces(owner_uid);

-- 工作室成员
CREATE TABLE cloud_workspace_members (
  ws_id   UUID NOT NULL REFERENCES cloud_workspaces(id) ON DELETE CASCADE,
  uid     TEXT NOT NULL,
  role    TEXT NOT NULL DEFAULT 'member',   -- owner | admin | member
  created_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (ws_id, uid)
);
CREATE INDEX idx_ws_members_uid ON cloud_workspace_members(uid);

-- 素材共享授权视图（不复制字节）
CREATE TABLE cloud_media_shares (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_uid   TEXT NOT NULL,
  media_key   TEXT NOT NULL,                    -- 指向 cloud_media.media_key
  target_type TEXT NOT NULL,                    -- workspace | user | link
  target_id   TEXT NOT NULL,                    -- ws_id | uid | share_link token
  perm        TEXT NOT NULL DEFAULT 'view',     -- view | download
  created_at  TIMESTAMPTZ DEFAULT now(),
  UNIQUE (target_type, target_id, media_key)
);
CREATE INDEX idx_shares_media ON cloud_media_shares(media_key);
CREATE INDEX idx_shares_target ON cloud_media_shares(target_type, target_id);

-- 公开链接（target_type='link' 时 target_id 指向此 token）
CREATE TABLE cloud_share_links (
  token       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_uid   TEXT NOT NULL,
  media_key   TEXT NOT NULL,
  perm        TEXT NOT NULL DEFAULT 'view',
  expires_at  TIMESTAMPTZ,                      -- NULL = 永不过期
  created_at  TIMESTAMPTZ DEFAULT now()
);
```

**本地兜底（SQLite，`app/cloudstore.py` 分支）**：同样补三张表建表语句（开发环境无 PG 时）。MVP 以 PG 为主。

**授权读取校验**（核心不变量）：
`session.uid` 能读某 `media_key` 的字节 ⇔
`uid == cloud_media.uid`（自己是 owner）**OR**
存在 `cloud_media_shares` 行满足 `(target_type='workspace' AND target_id IN 我的 ws 列表 AND media_key=?)` **OR**
`(target_type='user' AND target_id=uid AND media_key=?)` **OR**
`(target_type='link' AND target_id=link_token AND 未过期)`。

---

## 4. BFF 接口契约（新文件 `app/routers/shares.py`）

鉴权统一 `require_session`（uid 可信）。

### 4.1 Workspace 管理
| Method | Path | 说明 |
|--------|------|------|
| POST | `/api/workspaces` | 建工作室 `{name}` → 创建者自动 `role=owner` |
| GET | `/api/workspaces/mine` | 我创建/加入的工作室列表（含 role） |
| POST | `/api/workspaces/{id}/members` | 加成员 `{uid, role}`（仅 owner/admin） |
| DELETE | `/api/workspaces/{id}/members/{uid}` | 移除成员（owner/admin；不能移除自己若为唯一 owner） |
| GET | `/api/workspaces/{id}/members` | 成员列表 |

### 4.2 共享
| Method | Path | 说明 |
|--------|------|------|
| POST | `/api/shares` | 创建共享 `{media_keys:[], target_type, target_id?, perm}`。校验 owner 拥有这些 `media_key`（查 `cloud_media.uid==session.uid`），防越权共享他人素材 |
| DELETE | `/api/shares/{id}` | 撤销（owner 或 workspace admin） |
| GET | `/api/shares` | 「我发出的共享」列表（按 target 分组） |
| POST | `/api/shares/link` | 生成公开链接 `{media_key, perm, expires_at?}` → 返回 `token` |
| GET | `/api/shared/media` | 「共享给我的」素材精简索引（合并：我所在 ws 的共享 + 指定我的 + 我持有的 link token）。返回 `{media_key, owner_uid, name, mime, size, perm, source: 'workspace'\|'user'\|'link', shared_by}` |
| GET | `/api/shared/media/{key}` | 取字节：按 §3 不变量校验 → 代理读 OSS 字节返回（复用 `app/oss.py media_get`）。link 形态：`/api/shared/media/{key}?token=xxx` |

> **不复用 `/api/me/media/{key}`**：现有个人接口保持"仅 owner"语义不变，共享读取走独立的 `/api/shared/media/*`，隔离清晰、避免回归。

---

## 5. 前端改动点（`D:\code\flovart-web`，独立仓库）

| 文件 | 改动 |
|------|------|
| `types/index.ts` | `AssetItem` 加 `shared?: boolean; sharedBy?: string; sharePerm?: 'view'\|'download'; shareSource?`；新增 `Workspace`、`ShareTarget`、`SharedMediaItem` 类型 |
| `stores/useHostedStore.ts` | 加 `workspaces` 状态 + `fetchWorkspaces()`；加 `sharedMedia` 状态 + `fetchSharedMedia()`（调 `/api/shared/media`） |
| `utils/assetStorage.ts` | 本地 `assetLibrary` 不变；新增 `sharedMedia` 缓存（localforage `flovart:shared-media`，离线可读） |
| `components/studio/AssetLibraryBrowser.tsx` | 加「共享给我」Tab（渲染 `sharedMedia`，带"来自 X 工作室/用户"标记 + 缩略图 + 拖入画布）；素材项加「共享」按钮 |
| `components/studio/ShareModal.tsx`（新） | 分享弹窗：目标选择（workspace 下拉 / 指定 uid / 生成链接）+ 权限（查看引用 / 可下载）+ 链接复制 |
| `components/studio/AssetReferencePicker.tsx` + `MentionList.tsx` | 个人素材库分区合并「共享素材」：`sourceType='shared'`、`assetId={owner_uid, media_key}` |
| `components/agent/FlovartAgentPanel.tsx` | `referenceGroups.assets`（line 159）由 `assetLibrary.items` 合并 `sharedMedia` |
| `App.tsx` | `workflowSharedMedia`（line 303）改为从真共享源取（保留历史桥接 + 合并共享源），画布拖拽/引用走同一 `SharedMediaItem` |

**渲染解析**：节点 `metadata.sourceType='shared'` + `assetId` 时，`resolveNodeImageUrl` 走 `GET /api/shared/media/{key}`（带 owner 上下文）→ 字节返回 → 节点显示。用户在画布里是"引用"，不复制到其 `cloud_media`；点「保存到我的素材库」才复制（调 uploadMediaToBff 或新增 `/api/shared/media/{key}/copy`）。

---

## 6. 关键数据流时序

**场景：用户 A 把素材共享给工作室，用户 B 在画布引用**

```
A: 素材库点「共享」→ ShareModal 选工作室 W + 权限 view
  → POST /api/shares {media_keys:[k1], target_type:'workspace', target_id:W.id, perm:'view'}
  → BFF 校验 A 拥有 k1 → 写 cloud_media_shares 一行

B 登录刷新:
  → GET /api/shared/media → 合并(B 所在 ws 共享) → 返回 k1(标记来自工作室 W)
  → AssetLibraryBrowser「共享给我」Tab 显示 k1

B 拖 k1 到画布:
  → 节点 metadata {sourceType:'shared', assetId:{owner_uid:A, media_key:k1}}
  → 渲染时 resolveNodeImageUrl → GET /api/shared/media/k1 (BFF 校验 B∈W 成员→通过)
  → OSS 字节返回 → 节点显示（引用，不复制）
```

---

## 7. 权限与安全

- **所有共享读取走 BFF 鉴权**，前端永不见 owner 的 OSS 直链（公开 link 也经 BFF 代理读，token 即授权）。
- **创建共享校验 owner 拥有 media_key**（查 `cloud_media.uid==session.uid`），防越权共享他人素材。
- **workspace 成员管理**：member 不能把自己提为 owner/admin；移除成员自动失效其共享视图（共享行仍在，但该 uid 不再 ∈ ws 成员列表 → 读取校验失败）。
- **公开链接**：可设 `expires_at`；撤销即删 `cloud_share_links` 行，旧 token 立即失效。
- **权限语义**：`view` = 可读取字节用于画布渲染/预览/引用；`download` = 额外允许「保存到我的素材库 / 本地下载」。引用拖入画布只需 `view`（渲染必须读字节）。
- **配额**：共享不增 owner 存储占用（字节不复制）；被共享者「保存到我的素材库」才计入其配额。

---

## 8. 与现有引用链路整合

- `MentionList` 个人素材库分区 → 合并「我的 + 共享给我的」（`sourceType` 区分，UI 加"共享"角标）。
- `FlovartAgentPanel.referenceGroups.assets` → 合并 `sharedMedia`，agent 引用共享素材走 `/api/shared/media` 读取。
- 画布拖拽（`InfiniteWorkflow` 的 `addSharedMediaAt`）与 PromptBar 引用统一消费 `SharedMediaItem`。

---

## 9. 实施阶段

| 阶段 | 范围 | 交付 |
|------|------|------|
| **P1（MVP）** | Workspace 实体 + 单素材共享（workspace / 指定用户）+ 共享给我列表 + 画布引用 + 读取授权（不复制字节） | BFF 3 表 + `routers/shares.py` + 前端「共享给我」Tab + ShareModal(workspace/user) + 引用渲染 |
| **P2** | 文件夹合集共享 + 公开链接 + 权限细化(download) + 撤销/管理 UI + 保存到我的素材库 | ShareModal 补全 link 形态 + 管理列表 |
| **P3** | 共享通知（"X 共享了素材给你"）+ 共享素材搜索 + 跨工作室可见性 | 通知中心 + 搜索 |

---

## 10. 待确认 / 风险

1. **工作室 vs oneapis SSO/Casdoor org**：flovart 当前是独立 `uid` 体系，Workspace 是 flovart 内自建实体，**不与 Casdoor org 绑定**（除非飞哥要打通）。文档按"flovart 内自建"设计。
2. **素材是否已全量在 `cloud_media`**：前端素材上传走 `uploadMediaToBff`，应已落 `cloud_media(uid)`。需落地前确认（grep `uploadMediaToBff` 落库路径），若部分素材仅本地 IndexedDB 未上云，则共享前需先上传。
3. **5MB 历史限制不影响素材共享**：素材走独立 `cloud_media` 表，与 `cloud_docs` 的 `generations` 5MB 上限无关（该限制已在上一轮修复：推送云端前剥离原图 dataUrl）。
4. **多副本一致性**：共享授权在 PG，多 BFF 副本天然共享（无需文件锁），与现有架构一致。
