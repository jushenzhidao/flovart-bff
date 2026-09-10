"""注册赠送（幂等）。

new-api 的 add_quota 接口**没有幂等键**（POST /api/user/manage），重复调用会
重复加钱。BFF 用本地状态文件（signup_bonus.json）保证每个 uid 只发一次：
「先占位写盘 → 发放 → 标记成功」，发放失败回滚占位。

账本结构：
    {
      "42": {"points": 20000, "at": "2026-09-03T17:00:00+08:00", "status": "granted"},
      ...
    }
    status: reserved | granted | failed
"""
import json
import logging
from datetime import datetime, timezone

from . import config, store
from .newapi_client import admin_add_quota

logger = logging.getLogger("bff.promo")


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bonus_state() -> dict:
    return store.load_json(config.SIGNUP_STATE_FILE, {})


async def grant_signup(uid: int) -> int:
    """给新注册用户发注册赠送，返回实发积分（0 = 未发）。

    幂等：同一 uid 只发一次；重复调用返回 0。
    """
    if not config.PROMO_SIGNUP_ENABLED:
        return 0
    points = int(config.PROMO_SIGNUP_POINTS or 0)
    if points <= 0:
        return 0
    uid_s = str(uid)
    state = _bonus_state()
    if uid_s in state and state[uid_s].get("status") == "granted":
        return 0  # 已发过，幂等

    # 先占位写盘（并发下第二个人会看到 reserved 也直接跳过）
    state[uid_s] = {"points": points, "at": _stamp(), "status": "reserved"}
    store.save_json_atomic(config.SIGNUP_STATE_FILE, state)

    try:
        await admin_add_quota(int(uid), config.points_to_quota(points))
    except Exception:
        logger.exception("注册赠送发放失败 uid=%s points=%s，回滚占位", uid, points)
        state = _bonus_state()
        if uid_s in state:
            del state[uid_s]  # 回滚占位，下次注册重试可重新发放
            store.save_json_atomic(config.SIGNUP_STATE_FILE, state)
        return 0

    state = _bonus_state()
    if uid_s in state:
        state[uid_s]["status"] = "granted"
        store.save_json_atomic(config.SIGNUP_STATE_FILE, state)
    logger.info("注册赠送发放成功 uid=%s points=%s", uid, points)
    return points


def signup_summary() -> dict:
    """管理台总览用：赠送总笔数 / 总积分 / 今日新增。"""
    state = _bonus_state()
    granted = [v for v in state.values() if v.get("status") == "granted"]
    total_points = sum(int(v.get("points", 0)) for v in granted)
    # 今日（UTC 天）新增赠送数
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_count = sum(1 for v in granted if (v.get("at") or "").startswith(today))
    return {
        "signup_granted_count": len(granted),
        "signup_granted_points": total_points,
        "signup_today_count": today_count,
        "bonus_enabled": config.PROMO_SIGNUP_ENABLED,
        "bonus_points": int(config.PROMO_SIGNUP_POINTS or 0),
    }
