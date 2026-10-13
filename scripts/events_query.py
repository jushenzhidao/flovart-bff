#!/usr/bin/env python
"""用户行为埋点查询脚本（cloud_events）。双数据库方言：PG（生产容器）+ SQLite（本地）。

用法（在 flovart-bff 目录下）：
  本地：  C:/Users/81068/.workbuddy/binaries/python/envs/flovart-bff/Scripts/python.exe scripts/events_query.py --days 7
  生产：  docker exec flovart-bff python scripts/events_query.py --days 7
  只看明细： scripts/events_query.py --recent 20 --event generate_failed
  按 uid 过滤： scripts/events_query.py --uid 42 --days 30

数据库选择：优先 --db 指定 SQLite 路径；否则按 BFF 同款规则（POSTGRES_DSN/分项
环境变量 → PG；都没有 → 回退 data/flovart_cloud.db）。
⚠️ 2026-10-13 教训：初版 SQL 是纯 SQLite 方言（datetime('now',?)/json_extract/? 占位符），
本地验证过、上 PG 容器直接 PostgresSyntaxError。现按方言分别拼 SQL，PG 侧必须用
$n 占位符。改查询先想清楚两边方言。
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = None  # 由 main() 设置（SQLite 模式）


def _pg_dsn() -> str:
    dsn = os.environ.get("POSTGRES_DSN", "")
    if not dsn and os.environ.get("POSTGRES_HOST"):
        dsn = "postgres://{user}:{password}@{host}:{port}/{db}".format(
            user=os.environ.get("POSTGRES_USER", "flovart"),
            password=os.environ.get("POSTGRES_PASSWORD", ""),
            host=os.environ.get("POSTGRES_HOST", ""),
            port=os.environ.get("POSTGRES_PORT", "5432"),
            db=os.environ.get("POSTGRES_DB", "flovart"),
        )
    return dsn


def _q_rows(sql: str, args: tuple = ()) -> list[tuple]:
    """统一查询入口：PG 可用走 PG（asyncpg，$n 占位符），否则走本地 SQLite。"""
    dsn = _pg_dsn()
    if dsn:
        import asyncpg

        async def _pg():
            conn = await asyncpg.connect(dsn)
            try:
                return [tuple(r) for r in await conn.fetch(sql, *args)]
            finally:
                await conn.close()

        return asyncio.run(_pg())

    import sqlite3

    db_path = DB_PATH or "data/flovart_cloud.db"
    conn = sqlite3.connect(db_path)
    try:
        return [tuple(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


class _Bind:
    """按方言累计绑定参数：PG 生成 $1/$2…，SQLite 生成 ?。"""

    def __init__(self, pg: bool):
        self.pg = pg
        self.n = 0
        self.args: list = []

    def add(self, value):
        self.n += 1
        self.args.append(value)
        return f"${self.n}" if self.pg else "?"


def print_table(headers: list[str], rows: list[tuple]) -> None:
    if not rows:
        print("  （无数据）")
        return
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for r in rows:
        print(fmt.format(*[str(c) for c in r]))


def main() -> None:
    global DB_PATH
    p = argparse.ArgumentParser(description="cloud_events 行为埋点查询")
    p.add_argument("--days", type=int, default=7, help="统计最近 N 天（默认 7）")
    p.add_argument("--db", default=None, help="SQLite 路径（默认 data/flovart_cloud.db）")
    p.add_argument("--uid", type=int, default=None, help="只看某用户")
    p.add_argument("--event", default=None, help="只看某事件（如 generate_failed）")
    p.add_argument("--recent", type=int, default=0, help="显示最近 N 条明细")
    a = p.parse_args()
    DB_PATH = a.db

    pg = bool(_pg_dsn() and not a.db)

    def conds(b: _Bind) -> str:
        """WHERE 尾巴：时间窗在前，uid/event 在后（PG 占位符按出现顺序编号）。"""
        if pg:
            since = f"created_at >= now() - make_interval(days => {b.add(a.days)})"
        else:
            since = f"created_at >= datetime('now', {b.add(f'-{a.days} days')})"
        tail = ""
        if a.uid is not None:
            tail += f" AND uid = {b.add(a.uid)}"
        if a.event:
            tail += f" AND event = {b.add(a.event)}"
        return since + tail

    print(f"\n=== 行为埋点报表（最近 {a.days} 天 | {'PG' if pg else 'SQLite'}）===\n")

    b = _Bind(pg)
    rows = _q_rows(
        f"""SELECT event, COUNT(*) FROM cloud_events
            WHERE {conds(b)}
            GROUP BY event ORDER BY 2 DESC""",
        tuple(b.args),
    )
    print("[事件总量排行]")
    print_table(["event", "count"], rows)

    b = _Bind(pg)
    rows = _q_rows(
        f"""SELECT COUNT(DISTINCT client_id), COUNT(DISTINCT uid)
            FROM cloud_events
            WHERE {conds(b)}""",
        tuple(b.args),
    )
    print("\n[活跃度]  设备数 / 已登录用户数")
    print_table(["devices", "logged_in_users"], rows)

    b = _Bind(pg)
    rows = _q_rows(
        f"""SELECT COALESCE({'uid::text' if pg else 'uid'}, '0') AS uid,
                   COUNT(*) FILTER (WHERE event='generate_submit'),
                   COUNT(*) FILTER (WHERE event='generate_success'),
                   COUNT(*) FILTER (WHERE event='generate_failed'),
                   COUNT(*) FILTER (WHERE event='download')
            FROM cloud_events
            WHERE {conds(b)}
            GROUP BY uid ORDER BY 2 DESC LIMIT 20""",
        tuple(b.args),
    )
    print("\n[用户行为明细 Top20]  提交 / 成功 / 失败 / 下载（uid=0 为匿名/未登录）")
    print_table(["uid", "submit", "success", "failed", "download"], rows)

    model_expr = "props_json::jsonb->>'model'" if pg else "json_extract(props_json, '$.model')"
    b = _Bind(pg)
    rows = _q_rows(
        f"""SELECT {model_expr} AS model, COUNT(*),
                   COUNT(*) FILTER (WHERE event='generate_failed')
            FROM cloud_events
            WHERE event LIKE 'generate_%' AND {conds(b)}
            GROUP BY model ORDER BY 2 DESC LIMIT 15""",
        tuple(b.args),
    )
    print("\n[模型使用排行]  提交数 / 其中失败数")
    print_table(["model", "total", "failed"], rows)

    day_expr = "to_char(created_at, 'YYYY-MM-DD')" if pg else "date(created_at)"
    b = _Bind(pg)
    rows = _q_rows(
        f"""SELECT {day_expr} AS day, COUNT(DISTINCT client_id), COUNT(*)
            FROM cloud_events
            WHERE {conds(b)}
            GROUP BY day ORDER BY day DESC LIMIT {int(a.days)}""",
        tuple(b.args),
    )
    print("\n[每日趋势]  日期 / 活跃设备 / 事件量")
    print_table(["day", "devices", "events"], rows)

    if a.recent:
        b = _Bind(pg)
        # 最近明细不带时间窗：uid/event 条件在前，LIMIT 参数收尾。
        tail = ""
        if a.uid is not None:
            tail += f" AND uid = {b.add(a.uid)}"
        if a.event:
            tail += f" AND event = {b.add(a.event)}"
        rows = _q_rows(
            f"""SELECT id, COALESCE({'uid::text' if pg else 'uid'}, '-'), client_id,
                       event, props_json, path, created_at
                FROM cloud_events WHERE 1=1{tail}
                ORDER BY id DESC LIMIT {b.add(int(a.recent))}""",
            tuple(b.args),
        )
        print(f"\n[最近 {a.recent} 条明细]")
        print_table(["id", "uid", "client", "event", "props", "path", "time"], rows)

    print("\n提示：任务级请求明细（模型/耗时/状态/结果）在 cloud_request_log 表；"
          "网页报表在 管理台 → 行为报表 Tab。\n")


if __name__ == "__main__":
    main()
