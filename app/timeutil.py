"""时间显示工具：日志时间是 UTC ISO 串（带微秒、+00:00），肉眼没法直接读。

提供统一转换：UTC → 东八区 ``YYYY-MM-DD HH:MM:SS``。产品用户在国内，
固定 +08:00 即可（比让前端各自格式化省事，F12/console.table 直接可读）。
解析失败时原样返回，绝不让日志查询因为时间格式挂掉。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_CN_TZ = timezone(timedelta(hours=8))
# Python 的 fromisoformat 只认 1-6 位小数秒，上游可能给 7 位（asyncpg 微秒+纳秒残留），
# 先截到 6 位再解析。
_TRAIL_MICRO = re.compile(r"\.(\d{6})\d+")


def iso_to_cn(value) -> str | None:
    """UTC ISO 时间 → 东八区 ``2026-09-23 15:30:10``。

    接受 datetime / ISO 字符串（带或不带时区，无时区按 UTC——与落库口径一致）。
    解析失败原样返回字符串，不抛异常。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(_CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    try:
        s = _TRAIL_MICRO.sub(r".\1", str(value).strip())
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return value if isinstance(value, str) else str(value)
