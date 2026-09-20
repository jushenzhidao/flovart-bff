"""云端持久化数据面（v2）：元数据落 PostgreSQL、字节落外部对象存储。

设计（2026-09-07 拍板）：
- 字节层（图片/视频/音频二进制）→ 外部对象存储 OSS/COS/S3（app/oss，boto3 S3 协议）。
  BFF 联网读写远端，不落本地磁盘；前端读取走 presigned URL（/api/me/media/{key} 307 重定向）。
- 元数据层（KV 文档 + 媒体索引 + 配额）→ PostgreSQL（app/db，asyncpg）。
  多副本可共享同一 PG，无需本地文件锁（替代原 SQLite WAL 方案）。
- 兜底（开发/未配置云）：USE_PG=False → 本地 SQLite 元数据；OSS_ENABLED=False → 本地文件字节。
  两套后端通过 META / BLOB 两个抽象切换，调用方（routers/cloud.py、tasks.py）只认本模块异步 API。

公开函数（全部 async）：
  doc_get / doc_put / doc_delete / doc_list   （KV JSON）
  media_put / media_get / media_delete        （字节 + 索引）
  storage_overview
"""
import asyncio
import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import config
from . import db
from . import oss

logger = logging.getLogger("bff.cloudstore")

# 字节 / 文档上限（与 OSS / 本地一致）
MAX_DOC_BYTES = 5 * 1024 * 1024
MAX_MEDIA_BYTES = 50 * 1024 * 1024


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(dt) -> str:
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


# ===========================================================================
# 元数据后端：PgMeta（PostgreSQL） / LocalMeta（本地 SQLite 兜底）
# ===========================================================================
class _Meta:
    async def doc_get(self, uid, scope, doc_key) -> Optional[dict]:
        raise NotImplementedError

    async def doc_put(self, uid, scope, doc_key, payload) -> dict:
        raise NotImplementedError

    async def doc_delete(self, uid, scope, doc_key) -> bool:
        raise NotImplementedError

    async def doc_list(self, uid, scope) -> list:
        raise NotImplementedError

    async def media_index_put(self, uid, key, kind, mime, size,
                              source_request_id=None, source_kind=None) -> None:
        raise NotImplementedError

    async def media_index_get(self, uid, key) -> Optional[dict]:
        raise NotImplementedError

    async def media_index_delete(self, uid, key) -> bool:
        raise NotImplementedError

    async def media_bytes_used(self, uid) -> int:
        raise NotImplementedError

    async def overview(self, uid) -> dict:
        raise NotImplementedError

    async def reqlog_put(self, uid, request_id, kind, provider, model, payload, status, mode) -> None:
        raise NotImplementedError

    async def reqlog_update(self, request_id, status=None, task_id=None,
                            gateway_request_id=None, result=None) -> None:
        raise NotImplementedError

    async def reqlog_get(self, request_id) -> Optional[dict]:
        raise NotImplementedError

    async def reqlog_list(self, uid, limit, offset) -> list:
        raise NotImplementedError

    async def reqlog_fail_stale(self, hours: int) -> int:
        """启动兜底：把「submitted 且超过 hours」的行标 failed（进程重启丢在途后台任务）。返回条数。"""
        raise NotImplementedError

    # ---- 素材共享（A 点对点：指定 new-api 用户）----
    async def share_put_batch(self, owner_uid, target_uid, perm, name_map, keys) -> list:
        raise NotImplementedError

    async def share_list_by_owner(self, owner_uid) -> list:
        raise NotImplementedError

    async def share_list_for_viewer(self, viewer_uid) -> list:
        raise NotImplementedError

    async def share_get(self, share_id) -> Optional[dict]:
        raise NotImplementedError

    async def share_delete(self, share_id, owner_uid) -> bool:
        raise NotImplementedError

    async def media_index_get_by_key(self, key) -> Optional[dict]:
        raise NotImplementedError

    async def media_get_shared(self, key, viewer_uid) -> Optional[dict]:
        raise NotImplementedError


class PgMeta(_Meta):
    async def _c(self):
        return db.pool()

    async def doc_get(self, uid, scope, doc_key):
        pool = await self._c()
        row = await pool.fetchrow(
            "SELECT payload, revision, updated_at FROM cloud_docs WHERE uid=$1 AND scope=$2 AND doc_key=$3",
            uid, scope, doc_key)
        if not row:
            return None
        return {"payload": json.loads(row["payload"]), "revision": row["revision"],
                "updated_at": _iso(row["updated_at"])}

    async def doc_put(self, uid, scope, doc_key, payload):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(raw) > MAX_DOC_BYTES:
            raise ValueError(f"文档超过 {MAX_DOC_BYTES // (1024 * 1024)} MB 上限")
        pool = await self._c()
        row = await pool.fetchrow(
            """INSERT INTO cloud_docs(uid, scope, doc_key, payload, revision, updated_at)
               VALUES($1,$2,$3,$4,1,$5)
               ON CONFLICT(uid, scope, doc_key)
               DO UPDATE SET payload=EXCLUDED.payload,
                             revision=cloud_docs.revision+1,
                             updated_at=EXCLUDED.updated_at
               RETURNING revision, updated_at""",
            uid, scope, doc_key, raw, datetime.now(timezone.utc))
        return {"revision": row["revision"], "updated_at": _iso(row["updated_at"])}

    async def doc_delete(self, uid, scope, doc_key):
        pool = await self._c()
        row = await pool.execute(
            "DELETE FROM cloud_docs WHERE uid=$1 AND scope=$2 AND doc_key=$3",
            uid, scope, doc_key)
        return int(row.split()[1]) > 0

    async def doc_list(self, uid, scope):
        pool = await self._c()
        rows = await pool.fetch(
            "SELECT doc_key, revision, updated_at FROM cloud_docs WHERE uid=$1 AND scope=$2 ORDER BY updated_at DESC",
            uid, scope)
        return [{"doc_key": r["doc_key"], "revision": r["revision"], "updated_at": _iso(r["updated_at"])}
                for r in rows]

    async def media_index_put(self, uid, key, kind, mime, size,
                              source_request_id=None, source_kind=None):
        pool = await self._c()
        now = datetime.now(timezone.utc)
        await pool.execute(
            "INSERT INTO cloud_media(uid, media_key, kind, mime, size, "
            "source_request_id, source_kind, created_at, updated_at) "
            "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$8)",
            uid, key, kind or "media", mime or "application/octet-stream", size,
            source_request_id, source_kind, now)

    async def media_index_get(self, uid, key):
        pool = await self._c()
        row = await pool.fetchrow(
            "SELECT media_key, kind, mime, size, source_request_id, source_kind, created_at "
            "FROM cloud_media WHERE uid=$1 AND media_key=$2",
            uid, key)
        if not row:
            return None
        return {"media_key": row["media_key"], "kind": row["kind"], "mime": row["mime"],
                "size": row["size"],
                "source_request_id": row["source_request_id"],
                "source_kind": row["source_kind"],
                "created_at": _iso(row["created_at"])}

    async def media_index_delete(self, uid, key):
        pool = await self._c()
        row = await pool.execute("DELETE FROM cloud_media WHERE uid=$1 AND media_key=$2", uid, key)
        return int(row.split()[1]) > 0

    async def media_bytes_used(self, uid):
        pool = await self._c()
        return await pool.fetchval("SELECT COALESCE(SUM(size),0) FROM cloud_media WHERE uid=$1", uid) or 0

    async def overview(self, uid):
        pool = await self._c()
        doc_count = await pool.fetchval("SELECT COUNT(*) FROM cloud_docs WHERE uid=$1", uid)
        m = await pool.fetchrow(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS t FROM cloud_media WHERE uid=$1", uid)
        return {"doc_count": doc_count, "media_count": m["n"], "bytes_used": m["t"],
                "quota_bytes": config.OSS_QUOTA_BYTES, "media_limit_bytes": MAX_MEDIA_BYTES}

    async def reqlog_put(self, uid, request_id, kind, provider, model, payload, status, mode):
        pool = await self._c()
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        await pool.execute(
            """INSERT INTO cloud_request_log
               (request_id, uid, kind, provider, model, payload_json, status, mode, created_at, updated_at)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,now(),now())""",
            request_id, uid, kind, provider, model, raw, status, mode)

    async def reqlog_update(self, request_id, status=None, task_id=None,
                            gateway_request_id=None, result=None):
        pool = await self._c()
        sets, args = [], []
        if status is not None:
            sets.append("status=$" + str(len(args) + 1))
            args.append(status)
        if task_id is not None:
            sets.append("task_id=$" + str(len(args) + 1))
            args.append(task_id)
        if gateway_request_id is not None:
            sets.append("gateway_request_id=$" + str(len(args) + 1))
            args.append(gateway_request_id)
        if result is not None:
            sets.append("result_json=$" + str(len(args) + 1))
            args.append(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        if not sets:
            return
        sets.append("updated_at=now()")
        args.append(request_id)
        await pool.execute(
            f"UPDATE cloud_request_log SET {','.join(sets)} WHERE request_id=${len(args)}",
            *args)

    async def reqlog_get(self, request_id):
        pool = await self._c()
        row = await pool.fetchrow("SELECT * FROM cloud_request_log WHERE request_id=$1", request_id)
        return _pg_reqlog_row(row) if row else None

    async def reqlog_list(self, uid, limit, offset):
        pool = await self._c()
        rows = await pool.fetch(
            """SELECT request_id, kind, provider, model, status, task_id,
                      gateway_request_id, mode, created_at, updated_at
               FROM cloud_request_log WHERE uid=$1 ORDER BY created_at DESC LIMIT $2 OFFSET $3""",
            uid, limit, offset)
        return [_pg_reqlog_summary(r) for r in rows]

    async def reqlog_admin_list(self, uid, kind, status, model, limit, offset):
        """管理员视角查询请求日志（console）：filters 全部可选；返回 (items, total)。

        uid=None 不过滤用户；kind/status 精确匹配；model 子串匹配（ILIKE）。
        """
        pool = await self._c()
        where, args = [], []
        if uid is not None:
            args.append(uid)
            where.append(f"uid=${len(args)}")
        if kind:
            args.append(kind)
            where.append(f"kind=${len(args)}")
        if status:
            args.append(status)
            where.append(f"status=${len(args)}")
        if model:
            args.append(f"%{model}%")
            where.append(f"model ILIKE ${len(args)}")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = await pool.fetchval(
            f"SELECT COUNT(*) FROM cloud_request_log {clause}", *args) or 0
        rows = await pool.fetch(
            f"""SELECT request_id, uid, kind, provider, model, status, task_id,
                       gateway_request_id, mode, created_at, updated_at
                FROM cloud_request_log {clause}
                ORDER BY created_at DESC LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}""",
            *args, limit, offset)
        return [_pg_reqlog_summary(r) for r in rows], int(total)

    async def reqlog_history(self, uid, limit, offset, kind=""):
        """用户历史记录（含完整 params/result，供历史页 + 一键同款）。返回 (items, total)。"""
        pool = await self._c()
        where, args = ["uid=$1"], [uid]
        if kind:
            args.append(kind)
            where.append(f"kind=${len(args)}")
        clause = "WHERE " + " AND ".join(where)
        total = await pool.fetchval(
            f"SELECT COUNT(*) FROM cloud_request_log {clause}", *args) or 0
        rows = await pool.fetch(
            f"SELECT * FROM cloud_request_log {clause} "
            f"ORDER BY created_at DESC LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}",
            *args, limit, offset)
        return [_pg_reqlog_row(r) for r in rows], int(total)

    async def reqlog_fail_stale(self, hours: int) -> int:
        stale = json.dumps({"error": _STALE_SUBMITTED_ERROR}, ensure_ascii=False,
                           separators=(",", ":"))
        pool = await self._c()
        tag = await pool.execute(
            """UPDATE cloud_request_log
               SET status='failed', result=$1, updated_at=now()
               WHERE status='submitted' AND created_at < now() - make_interval(hours => $2)""",
            stale, hours)
        return int(tag.split()[-1]) if tag and tag.startswith("UPDATE") else 0

    # ---- 素材共享：A 点对点 ----
    async def share_put_batch(self, owner_uid, target_uid, perm, name_map, keys):
        pool = await self._c()
        now = datetime.now(timezone.utc)
        for k in keys:
            await pool.execute(
                """INSERT INTO cloud_media_shares(id, owner_uid, media_key, target_uid, perm, name, created_at)
                   VALUES($1,$2,$3,$4,$5,$6,$7)
                   ON CONFLICT (owner_uid, target_uid, media_key)
                   DO UPDATE SET perm=EXCLUDED.perm, name=EXCLUDED.name, created_at=EXCLUDED.created_at""",
                uuid.uuid4().hex, owner_uid, k, target_uid, perm, name_map.get(k) or "", now)
        return keys

    async def share_list_by_owner(self, owner_uid):
        pool = await self._c()
        rows = await pool.fetch(
            """SELECT s.id, s.owner_uid, s.media_key, s.target_uid, s.perm, s.name, s.created_at,
                      m.kind, m.mime, m.size
               FROM cloud_media_shares s
               JOIN cloud_media m ON m.media_key = s.media_key
               WHERE s.owner_uid=$1
               ORDER BY s.created_at DESC""", owner_uid)
        return [dict(r) for r in rows]

    async def share_list_for_viewer(self, viewer_uid):
        pool = await self._c()
        rows = await pool.fetch(
            """SELECT s.id, s.owner_uid, s.media_key, s.target_uid, s.perm, s.name, s.created_at,
                      m.kind, m.mime, m.size
               FROM cloud_media_shares s
               JOIN cloud_media m ON m.media_key = s.media_key
               WHERE s.target_uid=$1
               ORDER BY s.created_at DESC""", viewer_uid)
        return [dict(r) for r in rows]

    async def share_get(self, share_id):
        pool = await self._c()
        row = await pool.fetchrow(
            "SELECT id, owner_uid, media_key, target_uid, perm, name, created_at "
            "FROM cloud_media_shares WHERE id=$1", share_id)
        return dict(row) if row else None

    async def share_delete(self, share_id, owner_uid):
        pool = await self._c()
        cur = await pool.execute(
            "DELETE FROM cloud_media_shares WHERE id=$1 AND owner_uid=$2", share_id, owner_uid)
        return int(cur.split()[1]) > 0

    async def media_index_get_by_key(self, key):
        pool = await self._c()
        row = await pool.fetchrow(
            "SELECT uid, media_key, kind, mime, size, created_at FROM cloud_media WHERE media_key=$1", key)
        if not row:
            return None
        return {"uid": row["uid"], "media_key": row["media_key"], "kind": row["kind"],
                "mime": row["mime"], "size": row["size"], "created_at": _iso(row["created_at"])}

    async def media_get_shared(self, key, viewer_uid):
        pool = await self._c()
        row = await pool.fetchrow(
            "SELECT uid, kind, mime, size FROM cloud_media WHERE media_key=$1", key)
        if not row:
            return None
        owner = row["uid"]
        if owner != viewer_uid:
            perm = await pool.fetchrow(
                "SELECT perm FROM cloud_media_shares WHERE owner_uid=$1 AND media_key=$2 AND target_uid=$3",
                owner, key, viewer_uid)
            if not perm:
                return None
        blob = await BLOB.get(owner, key)
        if blob is None:
            return None
        return {"media_key": key, "url": blob.get("url"), "path": blob.get("path"),
                "mime": row["mime"] or blob.get("mime") or "application/octet-stream",
                "size": row["size"] or blob.get("size") or 0}


def _pg_reqlog_row(row) -> dict:
    return {
        "request_id": row["request_id"], "uid": row["uid"], "kind": row["kind"],
        "provider": row["provider"], "model": row["model"],
        "payload": json.loads(row["payload_json"]),
        "task_id": row["task_id"], "gateway_request_id": row["gateway_request_id"],
        "status": row["status"], "mode": row["mode"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "created_at": _iso(row["created_at"]), "updated_at": _iso(row["updated_at"]),
    }


def _pg_reqlog_summary(row) -> dict:
    return {
        "request_id": row["request_id"], "uid": row["uid"], "kind": row["kind"],
        "provider": row["provider"], "model": row["model"], "status": row["status"],
        "task_id": row["task_id"], "gateway_request_id": row["gateway_request_id"],
        "mode": row["mode"], "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


class LocalMeta(_Meta):
    """本地 SQLite 兜底（dev / 未配置 PG 时）。

    使用线程局部连接复用：每个工作线程只 open 一次 SQLite 连接，避免并发下
    反复 PRAGMA journal_mode=WAL + CREATE TABLE 导致的 database is locked。
    """
    def __init__(self):
        self._local = threading.local()

    def _connect(self):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(os.path.join(config.DATA_DIR, "flovart_cloud.db"),
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            # WAL 只需设一次；busy_timeout 设长一点扛住并发尖刺
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS cloud_docs(
                    uid INTEGER NOT NULL, scope TEXT NOT NULL, doc_key TEXT NOT NULL,
                    payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
                    PRIMARY KEY (uid, scope, doc_key));
                CREATE TABLE IF NOT EXISTS cloud_media(
                    uid INTEGER NOT NULL, media_key TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'media',
                    mime TEXT NOT NULL DEFAULT 'application/octet-stream', size INTEGER NOT NULL DEFAULT 0,
                    source_request_id TEXT, source_kind TEXT,
                    width INTEGER, height INTEGER, thumb_key TEXT, deleted_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_cloud_media_uid ON cloud_media(uid);
                CREATE TABLE IF NOT EXISTS cloud_request_log(
                    request_id TEXT PRIMARY KEY, uid INTEGER NOT NULL, kind TEXT NOT NULL,
                    provider TEXT, model TEXT, payload TEXT NOT NULL, task_id TEXT,
                    gateway_request_id TEXT, status TEXT NOT NULL DEFAULT 'submitted',
                    mode TEXT NOT NULL DEFAULT 'async', result TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_reqlog_uid_created ON cloud_request_log(uid, created_at);
                CREATE TABLE IF NOT EXISTS cloud_media_shares(
                    id TEXT PRIMARY KEY,
                    owner_uid INTEGER NOT NULL,
                    media_key TEXT NOT NULL,
                    target_uid INTEGER NOT NULL,
                    perm TEXT NOT NULL DEFAULT 'view',
                    name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE (owner_uid, target_uid, media_key));
                CREATE INDEX IF NOT EXISTS idx_shares_owner ON cloud_media_shares(owner_uid);
                CREATE INDEX IF NOT EXISTS idx_shares_target ON cloud_media_shares(target_uid);
            """)
            # 存量库补列：CREATE TABLE IF NOT EXISTS 不会给旧表加新列（血缘列 2026-09-20）
            existing = {r[1] for r in conn.execute("PRAGMA table_info(cloud_media)").fetchall()}
            for col, ddl in (("source_request_id", "TEXT"), ("source_kind", "TEXT"),
                             ("width", "INTEGER"), ("height", "INTEGER"),
                             ("thumb_key", "TEXT"), ("deleted_at", "TEXT")):
                if col not in existing:
                    conn.execute(f"ALTER TABLE cloud_media ADD COLUMN {col} {ddl}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cloud_media_src ON cloud_media(source_request_id)")
            conn.commit()
            self._local.conn = conn
        return conn

    async def doc_get(self, uid, scope, doc_key):
        def _():
            conn = self._connect()
            row = conn.execute(
                "SELECT payload, revision, updated_at FROM cloud_docs "
                "WHERE uid=? AND scope=? AND doc_key=?", (uid, scope, doc_key)).fetchone()
            if not row:
                return None
            return {"payload": json.loads(row["payload"]), "revision": row["revision"],
                    "updated_at": row["updated_at"]}
        return await asyncio.to_thread(_)

    async def doc_put(self, uid, scope, doc_key, payload):
        def _():
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if len(raw) > MAX_DOC_BYTES:
                raise ValueError(f"文档超过 {MAX_DOC_BYTES // (1024 * 1024)} MB 上限")
            conn = self._connect()
            now = _utcnow()
            ex = conn.execute(
                "SELECT revision FROM cloud_docs WHERE uid=? AND scope=? AND doc_key=?",
                (uid, scope, doc_key)).fetchone()
            rev = (ex["revision"] + 1) if ex else 1
            conn.execute(
                """INSERT INTO cloud_docs(uid, scope, doc_key, payload, revision, updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(uid,scope,doc_key)
                   DO UPDATE SET payload=excluded.payload, revision=excluded.revision,
                                 updated_at=excluded.updated_at""",
                (uid, scope, doc_key, raw, rev, now))
            conn.commit()
            return {"revision": rev, "updated_at": now}
        return await asyncio.to_thread(_)

    async def doc_delete(self, uid, scope, doc_key):
        def _():
            conn = self._connect()
            cur = conn.execute(
                "DELETE FROM cloud_docs WHERE uid=? AND scope=? AND doc_key=?",
                (uid, scope, doc_key))
            conn.commit()
            return cur.rowcount > 0
        return await asyncio.to_thread(_)

    async def doc_list(self, uid, scope):
        def _():
            conn = self._connect()
            rows = conn.execute(
                "SELECT doc_key, revision, updated_at FROM cloud_docs "
                "WHERE uid=? AND scope=? ORDER BY updated_at DESC", (uid, scope)).fetchall()
            return [dict(r) for r in rows]
        return await asyncio.to_thread(_)

    async def media_index_put(self, uid, key, kind, mime, size,
                              source_request_id=None, source_kind=None):
        def _():
            conn = self._connect()
            now = _utcnow()
            conn.execute(
                "INSERT INTO cloud_media(uid, media_key, kind, mime, size, "
                "source_request_id, source_kind, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (uid, key, kind or "media", mime or "application/octet-stream", size,
                 source_request_id, source_kind, now, now))
            conn.commit()
        return await asyncio.to_thread(_)

    async def media_index_get(self, uid, key):
        def _():
            conn = self._connect()
            row = conn.execute(
                "SELECT media_key, kind, mime, size, source_request_id, source_kind, created_at "
                "FROM cloud_media WHERE uid=? AND media_key=?", (uid, key)).fetchone()
            return dict(row) if row else None
        return await asyncio.to_thread(_)

    async def media_index_delete(self, uid, key):
        def _():
            conn = self._connect()
            cur = conn.execute(
                "DELETE FROM cloud_media WHERE uid=? AND media_key=?", (uid, key))
            conn.commit()
            return cur.rowcount > 0
        return await asyncio.to_thread(_)

    async def media_bytes_used(self, uid):
        def _():
            conn = self._connect()
            return conn.execute(
                "SELECT COALESCE(SUM(size),0) FROM cloud_media WHERE uid=?", (uid,)).fetchone()[0]
        return await asyncio.to_thread(_)

    async def overview(self, uid):
        def _():
            conn = self._connect()
            doc_count = conn.execute(
                "SELECT COUNT(*) FROM cloud_docs WHERE uid=?", (uid,)).fetchone()[0]
            m = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS t FROM cloud_media WHERE uid=?",
                (uid,)).fetchone()
            return {"doc_count": doc_count, "media_count": m["n"], "bytes_used": m["t"],
                    "quota_bytes": config.OSS_QUOTA_BYTES, "media_limit_bytes": MAX_MEDIA_BYTES}
        return await asyncio.to_thread(_)

    async def reqlog_put(self, uid, request_id, kind, provider, model, payload, status, mode):
        def _():
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            now = _utcnow()
            conn = self._connect()
            conn.execute(
                """INSERT INTO cloud_request_log
                   (request_id, uid, kind, provider, model, payload, status, mode, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (request_id, uid, kind, provider, model, raw, status, mode, now, now))
            conn.commit()
        return await asyncio.to_thread(_)

    async def reqlog_update(self, request_id, status=None, task_id=None,
                            gateway_request_id=None, result=None):
        def _():
            conn = self._connect()
            sets, args = [], []
            if status is not None:
                sets.append("status=?"); args.append(status)
            if task_id is not None:
                sets.append("task_id=?"); args.append(task_id)
            if gateway_request_id is not None:
                sets.append("gateway_request_id=?"); args.append(gateway_request_id)
            if result is not None:
                sets.append("result=?"); args.append(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            if not sets:
                return
            sets.append("updated_at=?")
            args.append(_utcnow())
            args.append(request_id)
            conn.execute(f"UPDATE cloud_request_log SET {','.join(sets)} WHERE request_id=?", args)
            conn.commit()
        return await asyncio.to_thread(_)

    async def reqlog_get(self, request_id):
        def _():
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM cloud_request_log WHERE request_id=?", (request_id,)).fetchone()
            return dict(row) if row else None
        row = await asyncio.to_thread(_)
        return _local_reqlog_row(row) if row else None

    async def reqlog_list(self, uid, limit, offset):
        def _():
            conn = self._connect()
            rows = conn.execute(
                """SELECT request_id, kind, provider, model, status, task_id,
                          gateway_request_id, mode, created_at, updated_at
                   FROM cloud_request_log WHERE uid=? ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (uid, limit, offset)).fetchall()
            return [dict(r) for r in rows]
        rows = await asyncio.to_thread(_)
        return [_local_reqlog_summary(r) for r in rows]

    async def reqlog_admin_list(self, uid, kind, status, model, limit, offset):
        """管理员视角查询请求日志（Local 兜底，与 PgMeta 同契约）。"""
        def _():
            conn = self._connect()
            where, args = [], []
            if uid is not None:
                where.append("uid=?"); args.append(uid)
            if kind:
                where.append("kind=?"); args.append(kind)
            if status:
                where.append("status=?"); args.append(status)
            if model:
                where.append("model LIKE ?"); args.append(f"%{model}%")
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            total = conn.execute(
                f"SELECT COUNT(*) FROM cloud_request_log {clause}", args).fetchone()[0]
            rows = conn.execute(
                f"""SELECT request_id, uid, kind, provider, model, status, task_id,
                           gateway_request_id, mode, created_at, updated_at
                    FROM cloud_request_log {clause}
                    ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                args + [limit, offset]).fetchall()
            return [dict(r) for r in rows], int(total)
        return await asyncio.to_thread(_)

    async def reqlog_history(self, uid, limit, offset, kind=""):
        def _():
            conn = self._connect()
            where, args = ["uid=?"], [uid]
            if kind:
                where.append("kind=?")
                args.append(kind)
            clause = "WHERE " + " AND ".join(where)
            total = conn.execute(
                f"SELECT COUNT(*) FROM cloud_request_log {clause}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM cloud_request_log {clause} "
                f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
                args + [limit, offset]).fetchall()
            return [_local_reqlog_row(r) for r in rows], int(total)
        return await asyncio.to_thread(_)

    async def reqlog_fail_stale(self, hours: int) -> int:
        def _():
            conn = self._connect()
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            stale = json.dumps({"error": _STALE_SUBMITTED_ERROR}, ensure_ascii=False,
                               separators=(",", ":"))
            rows = conn.execute(
                "SELECT request_id, created_at FROM cloud_request_log WHERE status='submitted'"
            ).fetchall()
            n = 0
            for r in rows:
                try:
                    created = datetime.fromisoformat(str(r["created_at"]))
                except ValueError:
                    continue
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created < cutoff:
                    conn.execute(
                        """UPDATE cloud_request_log
                           SET status='failed', result=?, updated_at=?
                           WHERE request_id=? AND status='submitted'""",
                        (stale, _utcnow(), r["request_id"]))
                    n += 1
            conn.commit()
            return n
        return await asyncio.to_thread(_)

    # ---- 素材共享（LocalMeta 补齐：之前漏实现导致 SQLite 回退后端共享 500）----
    async def media_index_get_by_key(self, key):
        def _():
            conn = self._connect()
            row = conn.execute(
                "SELECT uid, media_key, kind, mime, size, created_at FROM cloud_media WHERE media_key=?",
                (key,)).fetchone()
            if not row:
                return None
            return {"uid": row["uid"], "media_key": row["media_key"], "kind": row["kind"],
                    "mime": row["mime"], "size": row["size"], "created_at": row["created_at"]}
        return await asyncio.to_thread(_)

    async def media_get_shared(self, key, viewer_uid):
        def _():
            conn = self._connect()
            row = conn.execute(
                "SELECT uid, kind, mime, size FROM cloud_media WHERE media_key=?", (key,)).fetchone()
            if not row:
                return None
            owner = row["uid"]
            if owner != viewer_uid:
                sh = conn.execute(
                    "SELECT perm FROM cloud_media_shares WHERE owner_uid=? AND media_key=? AND target_uid=?",
                    (owner, key, viewer_uid)).fetchone()
                if not sh:
                    return None
            return {"uid": owner, "kind": row["kind"], "mime": row["mime"], "size": row["size"]}
        info = await asyncio.to_thread(_)
        if info is None:
            return None
        blob = await BLOB.get(info["uid"], key)
        if blob is None:
            return None
        return {"media_key": key, "url": blob.get("url"), "path": blob.get("path"),
                "mime": info["mime"] or blob.get("mime") or "application/octet-stream",
                "size": info["size"] or blob.get("size") or 0}

    async def share_put_batch(self, owner_uid, target_uid, perm, name_map, keys):
        def _():
            conn = self._connect()
            now = _utcnow()
            for k in keys:
                conn.execute(
                    """INSERT INTO cloud_media_shares(id, owner_uid, media_key, target_uid, perm, name, created_at)
                       VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(owner_uid, target_uid, media_key)
                       DO UPDATE SET perm=excluded.perm, name=excluded.name, created_at=excluded.created_at""",
                    (uuid.uuid4().hex, owner_uid, k, target_uid, perm, name_map.get(k) or "", now))
            conn.commit()
        await asyncio.to_thread(_)
        return keys

    async def share_list_by_owner(self, owner_uid):
        def _():
            conn = self._connect()
            rows = conn.execute(
                """SELECT s.id, s.owner_uid, s.media_key, s.target_uid, s.perm, s.name, s.created_at,
                          m.kind, m.mime, m.size
                   FROM cloud_media_shares s
                   JOIN cloud_media m ON m.media_key = s.media_key
                   WHERE s.owner_uid=?
                   ORDER BY s.created_at DESC""", (owner_uid,)).fetchall()
            return [dict(r) for r in rows]
        return await asyncio.to_thread(_)

    async def share_list_for_viewer(self, viewer_uid):
        def _():
            conn = self._connect()
            rows = conn.execute(
                """SELECT s.id, s.owner_uid, s.media_key, s.target_uid, s.perm, s.name, s.created_at,
                          m.kind, m.mime, m.size
                   FROM cloud_media_shares s
                   JOIN cloud_media m ON m.media_key = s.media_key
                   WHERE s.target_uid=?
                   ORDER BY s.created_at DESC""", (viewer_uid,)).fetchall()
            return [dict(r) for r in rows]
        return await asyncio.to_thread(_)

    async def share_get(self, share_id):
        def _():
            conn = self._connect()
            row = conn.execute(
                "SELECT id, owner_uid, media_key, target_uid, perm, name, created_at "
                "FROM cloud_media_shares WHERE id=?", (share_id,)).fetchone()
            return dict(row) if row else None
        return await asyncio.to_thread(_)

    async def share_delete(self, share_id, owner_uid):
        def _():
            conn = self._connect()
            cur = conn.execute(
                "DELETE FROM cloud_media_shares WHERE id=? AND owner_uid=?", (share_id, owner_uid))
            conn.commit()
            return cur.rowcount > 0
        return await asyncio.to_thread(_)


def _local_reqlog_row(row) -> dict:
    return {
        "request_id": row["request_id"], "uid": row["uid"], "kind": row["kind"],
        "provider": row["provider"], "model": row["model"],
        "payload": json.loads(row["payload"]),
        "task_id": row["task_id"], "gateway_request_id": row["gateway_request_id"],
        "status": row["status"], "mode": row["mode"],
        "result": json.loads(row["result"]) if row["result"] else None,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _local_reqlog_summary(row) -> dict:
    return {
        "request_id": row["request_id"], "uid": row["uid"], "kind": row["kind"],
        "provider": row["provider"], "model": row["model"], "status": row["status"],
        "task_id": row["task_id"], "gateway_request_id": row["gateway_request_id"],
        "mode": row["mode"], "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


# ===========================================================================
# 字节后端：OssBlob（外部对象存储） / LocalBlob（本地文件兜底）
# ===========================================================================
class _Blob:
    async def put(self, uid: int, key: str, blob: bytes, mime: str, kind: str) -> None:
        raise NotImplementedError

    async def get(self, uid: int, key: str) -> Optional[dict]:
        raise NotImplementedError

    async def delete(self, uid: int, key: str) -> bool:
        raise NotImplementedError


class OssBlob(_Blob):
    async def put(self, uid, key, blob, mime, kind):
        if not oss.is_configured():
            raise RuntimeError("OSS 未配置")
        oss.put_object(oss.media_key(uid, key), blob, mime, {"kind": kind})

    async def get(self, uid, key):
        if not oss.is_configured():
            return None
        obj_key = oss.media_key(uid, key)
        meta = oss.head_object_meta(obj_key)
        if meta is None:
            return None
        url = oss.presign_get(obj_key, config.OSS_PRESIGN_TTL)
        return {
            "url": url,
            "mime": meta.get("ContentType", "application/octet-stream"),
            "size": int(meta.get("ContentLength", 0)),
        }

    async def delete(self, uid, key):
        if not oss.is_configured():
            return False
        return oss.delete_object(oss.media_key(uid, key))


class LocalBlob(_Blob):
    def _path(self, uid: int, key: str) -> str:
        base = os.path.join(config.DATA_DIR, "media", str(uid), key[:2])
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, key)

    async def put(self, uid, key, blob, mime, kind):
        def _():
            path = self._path(uid, key)
            with open(path, "wb") as f:
                f.write(blob)
        await asyncio.to_thread(_)

    async def get(self, uid, key):
        def _():
            path = self._path(uid, key)
            if not os.path.exists(path):
                return None
            return {
                "path": path,
                "mime": "application/octet-stream",
                "size": os.path.getsize(path),
            }
        return await asyncio.to_thread(_)

    async def delete(self, uid, key):
        def _():
            path = self._path(uid, key)
            if not os.path.exists(path):
                return False
            os.remove(path)
            return True
        return await asyncio.to_thread(_)


# ===========================================================================
# 初始化：根据配置选后端
# ===========================================================================
META: _Meta = PgMeta() if config.USE_PG else LocalMeta()
BLOB: _Blob = OssBlob() if config.OSS_ENABLED else LocalBlob()


# ===========================================================================
# 公开 API（调用方只依赖这些函数）
# ===========================================================================
async def doc_get(uid: int, scope: str, doc_key: str) -> Optional[dict]:
    return await META.doc_get(uid, scope, doc_key)


async def doc_put(uid: int, scope: str, doc_key: str, payload: dict) -> dict:
    return await META.doc_put(uid, scope, doc_key, payload)


async def doc_delete(uid: int, scope: str, doc_key: str) -> bool:
    return await META.doc_delete(uid, scope, doc_key)


async def doc_list(uid: int, scope: str) -> list:
    return await META.doc_list(uid, scope)


async def media_put(uid: int, kind: str, mime: str, blob: bytes,
                    source_request_id: "str | None" = None,
                    source_kind: "str | None" = None) -> dict:
    if len(blob) > MAX_MEDIA_BYTES:
        raise ValueError(f"单文件超过 {MAX_MEDIA_BYTES // (1024 * 1024)} MB")
    if config.OSS_ENFORCE_QUOTA and config.OSS_QUOTA_BYTES:
        used = await META.media_bytes_used(uid)
        if used + len(blob) > config.OSS_QUOTA_BYTES:
            raise ValueError("云存储配额不足，请联系管理员扩容")
    key = uuid.uuid4().hex
    await BLOB.put(uid, key, blob, mime, kind)
    await META.media_index_put(uid, key, kind, mime, len(blob),
                               source_request_id=source_request_id,
                               source_kind=source_kind)
    return {"media_key": key, "size": len(blob), "mime": mime, "url": f"/api/me/media/{key}"}


async def media_get(uid: int, key: str) -> Optional[dict]:
    info = await META.media_index_get(uid, key)
    if not info:
        return None
    blob = await BLOB.get(uid, key)
    if blob is None:
        return None
    return {
        "media_key": key,
        "url": blob.get("url"),
        "path": blob.get("path"),
        "mime": info.get("mime") or blob.get("mime") or "application/octet-stream",
        "size": info.get("size") or blob.get("size") or 0,
    }


async def media_delete(uid: int, key: str) -> bool:
    ok = await META.media_index_delete(uid, key)
    if ok:
        try:
            await BLOB.delete(uid, key)
        except Exception as e:  # noqa: BLE001
            logger.warning("删除媒体字节失败 key=%s: %s", key, e)
    return ok


async def storage_overview(uid: int) -> dict:
    return await META.overview(uid)


# ---------------- 请求日志 ----------------
# 启动清扫的落库错误：进程重启会丢在途后台同步任务（asyncio.create_task），
# 对应行永远停在 submitted（2026-09-18 测试环境实测 3 条僵尸行），启动时统一标 failed。
_STALE_SUBMITTED_ERROR = {
    "message": "任务因服务重启丢失，请重试",
    "type": "stale_submitted",
    "status": 502,
    "stage": "startup_sweep",
}

# 注：strip_b64 是日志层公共设施（console 管理员详情 / chat 落库共用），
# 放这里而不是某个 router，避免跨 router import。
def strip_b64(obj, _max: int = 256):
    """递归把超长 base64 / data-uri 字段替换成占位符（日志只需诊断信息，不必存几 MB 图片）。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if (kl in ("b64_json", "b64", "image_base64", "base64")
                    and isinstance(v, str) and len(v) > _max):
                out[k] = f"<base64:{len(v)} chars>"
            elif (kl == "url" and isinstance(v, str) and v.startswith("data:")
                  and len(v) > _max):
                out[k] = f"<data-uri:{len(v)} chars>"
            else:
                out[k] = strip_b64(v, _max)
        return out
    if isinstance(obj, list):
        return [strip_b64(x, _max) for x in obj]
    return obj


async def request_log_put(uid: int, request_id: str, kind: str, provider: str, model: str,
                          payload: dict, status: str, mode: str) -> None:
    await META.reqlog_put(uid, request_id, kind, provider, model, payload, status, mode)


async def request_log_update(request_id: str, status=None, task_id=None,
                             gateway_request_id=None, result=None) -> None:
    await META.reqlog_update(request_id, status, task_id, gateway_request_id, result)


async def request_log_fail_stale(hours: int = 6) -> int:
    """启动兜底：把超时未终态的 submitted 行标 failed（进程重启丢在途后台任务）。返回条数。"""
    return await META.reqlog_fail_stale(hours)


async def request_log_get(request_id: str) -> Optional[dict]:
    return await META.reqlog_get(request_id)


async def request_log_list(uid: int, limit: int, offset: int) -> list:
    return await META.reqlog_list(uid, limit, offset)


async def request_log_history(uid: int, limit: int, offset: int, kind: str = "") -> dict:
    """历史记录（含完整 params/result）：b64/data-uri 剥离成占位符，防止单页几 MB。"""
    items, total = await META.reqlog_history(uid, limit, offset, kind)
    for it in items:
        it["payload"] = strip_b64(it.get("payload"))
        if it.get("result") is not None:
            it["result"] = strip_b64(it["result"])
    return {"items": items, "total": total}


async def request_log_admin_list(uid=None, kind: str = "", status: str = "",
                                 model: str = "", limit: int = 50,
                                 offset: int = 0) -> "tuple[list, int]":
    """管理员查询：uid=None 查全部；返回 (摘要列表, 总数)。"""
    return await META.reqlog_admin_list(uid, kind, status, model, limit, offset)


# ---------------- 素材共享（A 点对点：指定 new-api 用户）----------------

async def share_put_batch(owner_uid, target_uid, perm, name_map, keys) -> list:
    return await META.share_put_batch(owner_uid, target_uid, perm, name_map, keys)


async def share_list_by_owner(owner_uid) -> list:
    return await META.share_list_by_owner(owner_uid)


async def share_list_for_viewer(viewer_uid) -> list:
    return await META.share_list_for_viewer(viewer_uid)


async def share_get(share_id) -> Optional[dict]:
    return await META.share_get(share_id)


async def share_delete(share_id, owner_uid) -> bool:
    return await META.share_delete(share_id, owner_uid)


async def media_index_get_by_key(key) -> Optional[dict]:
    return await META.media_index_get_by_key(key)


async def media_get_shared(key, viewer_uid) -> Optional[dict]:
    return await META.media_get_shared(key, viewer_uid)
