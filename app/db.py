"""PostgreSQL 连接池 + 表结构迁移（元数据/索引层）。

创作产物的「字节」在对象存储（oss.py），这里的 PG 只存结构化元数据：
- cloud_docs：KV JSON 文档（projects / history / assets / settings）
- cloud_media：媒体索引（uid / key / mime / size，不含字节）

多副本可共享同一 PG，无需本地文件锁（替代原 SQLite WAL 方案）。
连接池用 asyncpg（纯异步，单写/多读均可）。
"""
import logging

import asyncpg

from . import config

logger = logging.getLogger("bff.db")

_POOL: "asyncpg.Pool | None" = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS cloud_docs(
    uid BIGINT NOT NULL,
    scope TEXT NOT NULL,
    doc_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (uid, scope, doc_key)
);
CREATE TABLE IF NOT EXISTS cloud_media(
    uid BIGINT NOT NULL,
    media_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'media',
    mime TEXT NOT NULL DEFAULT 'application/octet-stream',
    size BIGINT NOT NULL DEFAULT 0,
    source_request_id TEXT,
    source_kind TEXT,
    width INTEGER,
    height INTEGER,
    thumb_key TEXT,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cloud_media_uid ON cloud_media(uid);
CREATE INDEX IF NOT EXISTS idx_cloud_media_src ON cloud_media(source_request_id);
CREATE TABLE IF NOT EXISTS cloud_media_shares(
    id          TEXT PRIMARY KEY,
    owner_uid   BIGINT NOT NULL,
    media_key   TEXT NOT NULL,
    target_uid  BIGINT NOT NULL,
    perm        TEXT NOT NULL DEFAULT 'view',
    name        TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_uid, target_uid, media_key)
);
CREATE INDEX IF NOT EXISTS idx_shares_owner ON cloud_media_shares(owner_uid);
CREATE INDEX IF NOT EXISTS idx_shares_target ON cloud_media_shares(target_uid);
CREATE TABLE IF NOT EXISTS cloud_request_log(
    request_id TEXT PRIMARY KEY,
    uid BIGINT NOT NULL,
    kind TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    payload_json TEXT NOT NULL,
    task_id TEXT,
    gateway_request_id TEXT,
    status TEXT NOT NULL DEFAULT 'submitted',
    mode TEXT NOT NULL DEFAULT 'async',
    result_json TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reqlog_uid_created ON cloud_request_log(uid, created_at DESC);
"""


async def init_pool() -> None:
    """lifespan 启动：建池 + 建表。未配置 PG 则跳过（回落本地 SQLite）。"""
    global _POOL
    if not config.USE_PG:
        logger.info("POSTGRES 未配置，元数据回落本地 SQLite 后端")
        return
    _POOL = await asyncpg.create_pool(
        dsn=config.POSTGRES_DSN_RESOLVED,
        min_size=1,
        max_size=10,
        command_timeout=30,
    )
    await migrate()
    logger.info("PostgreSQL 连接池就绪：%s", _mask_dsn(config.POSTGRES_DSN_RESOLVED))


async def migrate() -> None:
    if _POOL is None:
        return
    async with _POOL.acquire() as conn:
        await conn.execute(SCHEMA)
        # 存量库补列（CREATE TABLE IF NOT EXISTS 不会给旧表加新列）。
        # 血缘列（2026-09-20）：media ↔ request_log 关联 + 预留（宽高/缩略图/软删）。
        for col, ddl in (
            ("source_request_id", "TEXT"),
            ("source_kind", "TEXT"),
            ("width", "INTEGER"),
            ("height", "INTEGER"),
            ("thumb_key", "TEXT"),
            ("deleted_at", "TIMESTAMPTZ"),
        ):
            await conn.execute(
                f"ALTER TABLE cloud_media ADD COLUMN IF NOT EXISTS {col} {ddl}")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cloud_media_src ON cloud_media(source_request_id)")


async def close_pool() -> None:
    global _POOL
    if _POOL is not None:
        await _POOL.close()
        _POOL = None


def pool() -> "asyncpg.Pool | None":
    return _POOL


async def ping() -> "tuple[bool, str]":
    if not config.USE_PG:
        return True, "ok(未启用)"
    if _POOL is None:
        return False, "连接池未初始化"
    try:
        async with _POOL.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, f"PostgreSQL 不可达: {e}"


def _mask_dsn(dsn: str) -> str:
    """日志脱敏：隐藏密码。"""
    try:
        from urllib.parse import urlparse

        p = urlparse(dsn)
        return dsn.replace(p.password or "", "***") if p.password else dsn
    except Exception:
        return dsn
