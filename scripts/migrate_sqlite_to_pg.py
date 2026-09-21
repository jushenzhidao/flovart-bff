#!/usr/bin/env python3
"""一次性搬迁：服务器旧 SQLite（flovart_cloud.db）→ PostgreSQL。

在 BFF 容器内执行（镜像自带 sqlite3 与 asyncpg，且同一 compose 网络可达 PG）：

    # 正式迁移（可安全重复执行）：
    docker compose run --rm bff python scripts/migrate_sqlite_to_pg.py

    # 先试跑（只统计，不写入）：
    docker compose run --rm bff python scripts/migrate_sqlite_to_pg.py --dry-run

原则：
- SQLite 打开后立即 PRAGMA query_only=ON，全程对源库零写入。
- PG 侧 INSERT ... ON CONFLICT DO NOTHING：主键/唯一键冲突时保留 PG 现有行
  （保护切换 PG 之后产生的新数据），因此重复执行安全。
- 列取「SQLite 实际拥有的列 ∩ PG 列」：旧 SQLite 没有 2026-09-20 的血缘列也能迁。
- 时间列是 ISO 字符串（LocalMeta 的 _utcnow()），转 datetime 再给 asyncpg
  （timestamptz 参数不接受字符串）。
- 媒体「字节」不用迁：仍在 flovart-bff-data 卷 /data/media，BFF 挂载不变继续读。
"""
import argparse
import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timezone

# 仓库内直接跑：脚本上一级 = 项目根（app/ 所在）；容器挂载跑（如 /m.py）：
# script dir 是 /，app 包在镜像 WORKDIR /app 下 → 必须补 cwd。
sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from app import config, db  # noqa: E402

# (表名, PG 列——顺序即建表口径)。SQLite 侧按 PRAGMA 实际拥有取交集。
TABLES = [
    ("cloud_docs", ["uid", "scope", "doc_key", "payload", "revision", "updated_at"]),
    ("cloud_media", ["uid", "media_key", "kind", "mime", "size", "source_request_id",
                     "source_kind", "width", "height", "thumb_key", "deleted_at",
                     "created_at", "updated_at"]),
    ("cloud_media_shares", ["id", "owner_uid", "media_key", "target_uid", "perm",
                            "name", "created_at"]),
    ("cloud_request_log", ["request_id", "uid", "kind", "provider", "model",
                           "payload_json", "task_id", "gateway_request_id",
                           "status", "mode", "result_json", "created_at", "updated_at"]),
]
TIME_COLS = {"created_at", "updated_at", "deleted_at"}
BATCH = 500


def to_dt(v):
    """SQLite 里的 ISO 字符串 → 带时区 datetime（asyncpg timestamptz 要求）。"""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).strip()
    if " " in s and "T" not in s:  # 容错空格分隔
        s = s.replace(" ", "T", 1)
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# PG NOT NULL 列的兜底默认值：早期版本写入过 NULL（如实测 payload_json），
# 迁入时补默认值，避免 NotNullViolationError 中断整表。
NOT_NULL_FALLBACK = {
    "payload": "{}", "revision": 1,
    "kind": "media", "mime": "application/octet-stream", "size": 0,
    "perm": "view", "name": "",
    "payload_json": "{}", "status": "submitted", "mode": "async",
}
# 主键/必需列本身为 NULL 的脏行无法迁，跳过并计数（比整表崩掉强）
REQUIRED_COLS = {"uid", "media_key", "request_id", "doc_key", "scope",
                 "id", "owner_uid", "target_uid"}


async def migrate(dry_run: bool) -> int:
    if not config.USE_PG:
        print("❌ POSTGRES 未配置（USE_PG=False），请在配置了 PG 的容器内运行本脚本。")
        return 2
    sqlite_path = os.path.join(config.DATA_DIR, "flovart_cloud.db")
    if not os.path.exists(sqlite_path):
        print(f"✅ 未找到 {sqlite_path}，源库不存在，无需迁移。")
        return 0

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    src.execute("PRAGMA query_only=ON")  # 对源库绝对零写入
    pool = await asyncpg.create_pool(dsn=config.POSTGRES_DSN_RESOLVED,
                                     min_size=1, max_size=3, command_timeout=60)
    total_new = 0
    try:
        async with pool.acquire() as conn:
            # PG 是空库时先建表（与 app/db.py 完全同口径，幂等）
            await conn.execute(db.SCHEMA)
            for col, ddl in (("source_request_id", "TEXT"), ("source_kind", "TEXT"),
                             ("width", "INTEGER"), ("height", "INTEGER"),
                             ("thumb_key", "TEXT"), ("deleted_at", "TIMESTAMPTZ")):
                await conn.execute(
                    f"ALTER TABLE cloud_media ADD COLUMN IF NOT EXISTS {col} {ddl}")
            for table, pg_cols in TABLES:
                sqlite_cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
                if not sqlite_cols:
                    print(f"{table}: 源库无此表，跳过")
                    continue
                # ⚠️ 极老版本源库可能缺 PG 后来加的列（实测：旧库 cloud_request_log
                # 无 payload_json 列）。取交集会把该列静默丢掉 → PG NOT NULL 无默认
                # → 全表插入失败。缺列但在兜底表里的，直接补默认值。
                sqlite_set = set(sqlite_cols)
                filled = [c for c in pg_cols if c not in sqlite_set and c in NOT_NULL_FALLBACK]
                cols = [c for c in pg_cols if c in sqlite_set or c in NOT_NULL_FALLBACK]

                def _val(c, r, _ss=sqlite_set):
                    if c not in _ss:
                        return NOT_NULL_FALLBACK[c]  # 源库缺列 → 兜底默认
                    v = r[c]
                    if v is None and c in NOT_NULL_FALLBACK:
                        return NOT_NULL_FALLBACK[c]  # 源库有列但为 NULL
                    return to_dt(v) if c in TIME_COLS else v

                rows = src.execute(f"SELECT {','.join(c for c in cols if c in sqlite_set)} FROM {table}").fetchall()
                before = await conn.fetchval(f"SELECT count(*) FROM {table}")
                note = "（dry-run，未写入）" if dry_run else ""
                fill_note = f"，源库缺列补默认 {filled}" if filled else ""
                print(f"{table}: 源 {len(rows)} 行 / PG 现有 {before} 行{fill_note}{note}")
                if dry_run or not rows:
                    continue
                col_sql = ", ".join(cols)
                ph = ", ".join(f"${i + 1}" for i in range(len(cols)))
                stmt = (f"INSERT INTO {table}({col_sql}) VALUES({ph}) "
                        "ON CONFLICT DO NOTHING")
                batch = []
                skipped = 0
                for r in rows:
                    if any(c in sqlite_set and r[c] is None for c in cols if c in REQUIRED_COLS):
                        skipped += 1
                        continue
                    batch.append([_val(c, r) for c in cols])
                    if len(batch) >= BATCH:
                        await conn.executemany(stmt, batch)
                        batch = []
                if batch:
                    await conn.executemany(stmt, batch)
                after = await conn.fetchval(f"SELECT count(*) FROM {table}")
                extra = f"，跳过脏行 {skipped}" if skipped else ""
                print(f"{table}: → 迁后 {after} 行（新增 {after - before}{extra}）")
                total_new += after - before
    finally:
        await pool.close()
        src.close()
    print(f"\n{'[dry-run] ' if dry_run else ''}完成：共新增 {total_new} 行。")
    if not dry_run:
        print("提示：媒体字节仍在 flovart-bff-data 卷 /data/media，无需搬迁；"
              "本脚本可安全重复执行（冲突行按「保留 PG 现值」跳过）。")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="SQLite → PostgreSQL 一次性搬迁")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写入")
    sys.exit(asyncio.run(migrate(ap.parse_args().dry_run)))
