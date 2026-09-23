"""app/timeutil.iso_to_cn：UTC ISO → 东八区可读时间的边界用例。"""
from app.timeutil import iso_to_cn  # noqa: F401  (导入即验证模块名正确)


def test_iso_with_micro_and_offset():
    # 用户日志里的真实形态：7 位小数秒 + UTC 偏移
    assert iso_to_cn("2026-09-23T07:30:10.1635814+00:00") == "2026-09-23 15:30:10"


def test_iso_naive_treated_as_utc():
    assert iso_to_cn("2026-09-23T07:30:10") == "2026-09-23 15:30:10"


def test_iso_z_suffix():
    assert iso_to_cn("2026-09-23T07:30:10Z") == "2026-09-23 15:30:10"


def test_cross_day_conversion():
    # UTC 23 点 → 东八区次日
    assert iso_to_cn("2026-09-23T16:30:00+00:00") == "2026-09-24 00:30:00"


def test_garbage_returns_original():
    assert iso_to_cn("not-a-time") == "not-a-time"
    assert iso_to_cn(None) is None
