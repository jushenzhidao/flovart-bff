"""行为埋点路由（用户行为分析，2026-10-13 飞哥拍板自建）。

- POST /api/events   ：前端批量上报。**允许匿名**（登录/注册事件发生在登录前），
  有 session 则关联 uid。防刷三件套：批量上限 + 单条 props 体积上限 + 每 client 滑窗限速。
- GET  /api/console/events/summary 等管理台查询：require_admin。

设计取舍：
- 不存 IP（分析靠 client_id；少存一类敏感数据，合规面小）。
- event 名不搞白名单枚举（新增行为不用改后端），但限长 + 字符集校验防垃圾。
- 关闭开关（BFF_EVENTS_ENABLED=false）时返回 202 丢弃 —— 前端不发重试，不堆积。
"""
import logging
import re
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, Query, Request

from .. import cloudstore, config
from ..resp import ok
from ..security import read_session, require_admin

logger = logging.getLogger("bff.events")

router = APIRouter(prefix="/api")

# event 名约束：小写字母/数字/下划线/点，最长 64。前端命名规范见 services/analytics.ts。
_EVENT_RE = re.compile(r"^[a-z0-9_.]{1,64}$")

# 进程内滑窗限速：key=client_id（匿名也限），超限直接丢弃（埋点丢得起，别拖垮服务）。
_rate_buckets: dict[str, deque] = defaultdict(deque)
_rate_lock_held = False  # 单 worker（架构铁律），无锁竞争面；留标记防未来误改多 worker


def _rate_allow(key: str, limit: int, window: float = 60.0) -> bool:
    now = time.monotonic()
    bucket = _rate_buckets[key]
    while bucket and bucket[0] < now - window:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    # 防内存膨胀：桶太多时清最老的（正常流量下不会触发）
    if len(_rate_buckets) > 10000:
        oldest = sorted(_rate_buckets.items(), key=lambda kv: kv[1][0] if kv[1] else 0)[:2000]
        for k, _ in oldest:
            _rate_buckets.pop(k, None)
    return True


class _EventIn:
    __slots__ = ("event", "props", "session_id", "client_id", "path")

    def __init__(self, event: str, props: dict | None = None,
                 session_id: str = "", client_id: str = "", path: str = ""):
        self.event = event
        self.props = props if isinstance(props, dict) else {}
        self.session_id = session_id or ""
        self.client_id = client_id or ""
        self.path = path or ""


@router.post("/events")
async def post_events(request: Request, body: dict):
    """批量上报行为事件。匿名可写（登录/注册事件在会话建立前）。"""
    if not config.EVENTS_ENABLED:
        return {"success": True, "message": "disabled", "data": {"accepted": 0}}

    items = body.get("events") if isinstance(body, dict) else None
    if not isinstance(items, list) or not items:
        return ok({"accepted": 0})
    if len(items) > config.EVENTS_BATCH_MAX:
        return {"success": False, "message": f"单批最多 {config.EVENTS_BATCH_MAX} 条"}

    session = read_session(request)
    uid = session.get("uid") if session else None

    rows, rejected = [], 0
    ua = (request.headers.get("user-agent") or "")[:200]
    for item in items[: config.EVENTS_BATCH_MAX]:
        if not isinstance(item, dict):
            rejected += 1
            continue
        event = str(item.get("event") or "")
        if not _EVENT_RE.match(event):
            rejected += 1
            continue
        client_id = str(item.get("client_id") or "")[:64]
        if not _rate_allow(client_id or (f"uid:{uid}" if uid else "anon"), config.EVENTS_RATE_PER_MIN):
            rejected += 1
            continue
        props = item.get("props") if isinstance(item.get("props"), dict) else {}
        # props 序列化后超限 → 整条拒绝（前端不会发这么大的，超限即异常/滥用）
        rows.append({
            "uid": uid,
            "client_id": client_id,
            "session_id": str(item.get("session_id") or "")[:64],
            "event": event,
            "props": props,
            "path": str(item.get("path") or "")[:200],
            "ua": ua,
        })
    if rows:
        # 逐条体积校验放在入库前（props 已是 dict，序列化检查最直接）
        import json as _json
        final_rows = []
        for r in rows:
            try:
                if len(_json.dumps(r["props"], ensure_ascii=False)) > config.EVENTS_MAX_PROPS_BYTES:
                    rejected += 1
                    continue
            except (TypeError, ValueError):
                rejected += 1
                continue
            final_rows.append(r)
        if final_rows:
            try:
                await cloudstore.events_insert(final_rows)
            except Exception:  # noqa: BLE001 —— 埋点写失败绝不影响业务
                logger.exception("埋点入库失败（%s 条）", len(final_rows))
                return {"success": False, "message": "入库失败"}
    return ok({"accepted": len(rows), "rejected": rejected})


@router.get("/console/events/summary")
async def get_events_summary(days: int = Query(30, ge=1, le=365),
                             _session: dict = Depends(require_admin)):
    """管理台：按事件聚合 + 每日总量 + 活跃数。"""
    return ok({
        "days": days,
        "summary": await cloudstore.events_summary(days),
        "daily": await cloudstore.events_daily(min(days, 30)),
        "active": await cloudstore.events_active_clients(min(days, 30)),
    })


@router.get("/console/events")
async def get_events(limit: int = Query(100, ge=1, le=500),
                     offset: int = Query(0, ge=0),
                     event: str = Query(""),
                     uid: int | None = Query(None),
                     _session: dict = Depends(require_admin)):
    """管理台：最近事件明细（可按事件/用户过滤）。"""
    items = await cloudstore.events_recent(limit, event=event, uid=uid)
    return ok({"items": items, "limit": limit, "offset": offset})
