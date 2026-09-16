# frontend-storage（细节卷 · 从 MEMORY.md 拆出 2026-09-16）

> MEMORY.md 只留铁律摘要，血案全过程/完整表格见本文件。

## 🔴 前端本地保险库持久化铁律（keyVault 血案）
`utils/keyVault.ts` = 浏览器端 Key 加密库（PBKDF2 100k + AES-GCM，密文进 IndexedDB，实例名经 `ns()` 带 `__u<uid>`）
- ⛔ **禁用 `btoa(String.fromCharCode(...bytes))`**：展开运算符把每字节变**函数实参**，V8 超 **~128KB 抛 `RangeError: Maximum call stack size exceeded`**。须用 `bytesToBase64()`（分块 32KB + `apply(null, subarray)`）
- **极易命中**：`handleSaveKey` 会把端点**全部模型清单**（真实网关 743 个）写进 `customModels` → 序列化后远超阈值。小 key 能存、大 key 静默存不进
- ⛔ **`saveKeysEncrypted` 必须返回成败且把 `encryptKeys` 纳入 try**：历史上 fire-and-forget → **UI 提示「已保存」但实际没落盘**（「强刷新后服务消失」的原始现象）
- **排查手法**：`page.addInitScript` hook `IDBObjectStore.prototype.put/delete/clear` + `page.on('pageerror', e=>e.stack)`
- 回归 `tests/browserPersistence.test.ts`（4 例，含 200KB+ 大对象；改 keyVault 前必跑）

## 🎯 多账号本地存储隔离（已闭环，勿推翻）
`utils/storageNamespace.ts` 的 `ns()` 给 localforage 实例名追加 `__u<uid>`；14 个存储模块在**模块顶层**调用；登录/登出**整页 reload** 重建实例
- **真根因 = ES module import 求值顺序**：`index.tsx` 的 `import { RouterHost }` 会传递求值整条 storage 链 → `primeStorageNamespace()` 写在其后就**晚了**
- **修法（两条必须同时满足）**：① `storageNamespace.ts` **文件末尾裸调用** `primeStorageNamespace();` ② `index.tsx` 首行 `import './utils/storageNamespace';`
- **`bff_uid`**：`bff_session` 是 httponly 读不到 uid → BFF 额外种 `httponly=False` 的 `bff_uid`（**非凭据**，只挑 IndexedDB 库名后缀）
- **HashRouter 铁律**：凡依赖整页 reload 重建模块顶层单例者，**禁用 `location.assign(pathname#x)`** → 须 `window.location.hash = x; window.location.reload();`
- reload 防循环：`clearStorageReloadMarker`/`consumeStorageReloadMarker`(读后即焚)/`markStorageReload`
- ⛔ **运行时动态实例方案已回退，勿再引入**；回归 `tests/storageNamespaceIsolation.test.ts`（17 例）

