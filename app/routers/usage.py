"""调用日志与用量统计（用户态）。全部积分口径，不泄露裸 quota。"""
import asyncio
import logging
import time

from fastapi import APIRouter, Depends

from .. import config, newapi_client as na
from ..newapi_client import NewApiError
from ..resp import ok
from ..security import require_session

router = APIRouter()
logger = logging.getLogger("bff.usage")

P = config.quota_to_points
PX = config.quota_to_points_exact  # 单条明细保留小数，避免「总额有值、每条都空」

# stat 计算代价较高（上游需聚合全量日志），按 uid 短缓存 30s，翻页不再重复触发。
_STAT_CACHE_TTL = 30
_stat_cache: dict[int, tuple[dict, float]] = {}


def _log_payload(l: dict) -> dict:
    return {
        "id": l["id"], "type": l["type"], "model_name": l.get("model_name", ""),
        "token_name": l.get("token_name", ""),
        "prompt_tokens": l.get("prompt_tokens", 0),
        "completion_tokens": l.get("completion_tokens", 0),
        "points": PX(l.get("quota", 0)), "content": l.get("content", ""),
        "created_at": l["created_at"],
    }


@router.get("/api/log/self")
async def log_self(p: int = 1, page_size: int = 10,
                   session: dict = Depends(require_session)):
    p = max(1, p)
    page_size = min(max(1, page_size), 100)
    uid = session["uid"]

    # stat 缓存：同一用户 30s 内翻页不再重复触发上游聚合。
    now = time.time()
    cached = _stat_cache.get(uid)
    if cached and now - cached[1] < _STAT_CACHE_TTL:
        st = cached[0]
        fetch_stat = False
    else:
        st = None
        fetch_stat = True

    # 列表与统计并发拉取，避免串行等待；统计失败时降級为空值继续返回列表。
    try:
        if fetch_stat:
            d, st_new = await asyncio.gather(
                na.get_logs(session["pat"], uid, p, page_size),
                na.get_log_stat(session["pat"], uid),
            )
            st = st_new
            _stat_cache[uid] = (st, now)
        else:
            d = await na.get_logs(session["pat"], uid, p, page_size)
    except NewApiError as e:
        if e.status_code == 401:
            # 用户个人 PAT 失效：降级为管理员按 uid 拉日志；
            # 统计无对应用户级管理员端点，降级为空值（不阻断列表展示）。
            logger.warning("用户 PAT 拉日志 401，降级管理员查 uid=%s", uid)
            try:
                d = await na.admin_get_user_logs(uid, p, page_size)
            except NewApiError:
                d = {"items": [], "total": 0}
            st = None
        elif e.status_code == 403:
            raise
        else:
            logger.warning("get_logs 或 get_log_stat 失败 uid=%s: %s", uid, e.message)
            d = {"items": [], "total": 0}

    items = [_log_payload(l) for l in d.get("items", [])]
    stat = {
        "request_count": d.get("total", 0),
        "points": P(st.get("quota", 0)) if st else 0,
        "rpm": st.get("rpm", 0) if st else 0,
        "tpm": st.get("tpm", 0) if st else 0,
        "prompt_tokens": sum(i["prompt_tokens"] for i in items),
        "completion_tokens": sum(i["completion_tokens"] for i in items),
    }
    return ok({"items": items, "total": d.get("total", 0), "page": p,
               "page_size": page_size, "stat": stat})
