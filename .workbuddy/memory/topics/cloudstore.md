# cloudstore（细节卷 · 从 MEMORY.md 拆出 2026-09-16）

> MEMORY.md 只留铁律摘要，血案全过程/完整表格见本文件。

## ⭐ 云同步 / 素材 / 删除一致性
- **铁律：任何「删除」都必须立即落远端**（`flushWorkflowCloudDeletions()` 绕过 800ms 防抖立即 DELETE；曾因挂在防抖 → 「删掉的工作流又回来」）
- 纵深防御：`cloudIdsOwnerUid` 归属戳（`pullOnce` 写入、登出清 null），uid 不匹配**跳过不发**；门控：`storageAligned` false 时禁启 `startWorkflowCloudSync`
- **工作流素材**：项目元数据走 cloudSync→`cloud_docs`；**素材字节**经 `media.ts` 的 `ensureProjectMediaUploaded` 上传 `/api/me/media`，cloudMediaKey 回写节点 metadata（节点/封面/分层各一份）
- 读取兜底：`loadWorkflowMediaBlob` 本地 miss → `restoreWorkflowMediaFromCloud`；索引 `registerWorkflowCloudMediaIndex`（storageKey→cloudMediaKey）
- **铁律：上传失败只 warn 不抛（下次 push 重试）；回写 metadata 不改 `updatedAt`，否则 schedulePush 死循环**
- 生成历史镜像到 `cloud_docs` 的 `history` scope（doc_key=generations，整体覆盖+revision 乐观锁）；本地 IndexedDB 仍为主存，云端负责跨设备恢复
- **素材共享（点对点）**：`admin_resolve_uid_by_username`；`cloud_media_shares`(owner+target+media_key+perm)；字节不复制，`/api/shared/media/{key}` 代理校验。共享前先 `ensureCloudKey` 上传拿 cloudMediaKey
- **后端隔离已验证**：`cloud.py` 全部 `_uid(session)` 隔离无问题；🔒 跨账号误删结构上不可能（`cloud_docs` 主键 `(uid,scope,doc_key)`）
- ⚠️ **认知陷阱**：A 对 B 的 doc_key 发 DELETE/PUT/GET 返回 **200 而非 404** → **判断越权必须看「B 端数据是否变化」**

