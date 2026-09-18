"""启动清扫 stale submitted（进程重启丢在途后台同步任务 → 日志僵尸行）。"""
import asyncio
from datetime import datetime, timedelta, timezone

from app import cloudstore
from app.cloudstore import LocalMeta


def _backdate(meta, request_id: str, hours_ago: int) -> None:
    def _():
        conn = meta._connect()
        old = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        conn.execute("UPDATE cloud_request_log SET created_at=? WHERE request_id=?",
                     (old, request_id))
        conn.commit()
    asyncio.run(asyncio.to_thread(_))


def test_local_fail_stale_sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(cloudstore.config, "DATA_DIR", str(tmp_path))
    meta = LocalMeta()
    # r1: 老 submitted（应被清扫）；r2: 新 submitted（保留）；r3: 老 failed（不动）
    asyncio.run(meta.reqlog_put(7, "r1", "image-gen", "gateway", "m", {}, "submitted", "sync"))
    asyncio.run(meta.reqlog_put(7, "r2", "image-gen", "gateway", "m", {}, "submitted", "sync"))
    asyncio.run(meta.reqlog_put(7, "r3", "image-gen", "gateway", "m", {}, "failed", "sync"))
    _backdate(meta, "r1", 7)
    _backdate(meta, "r3", 7)

    swept = asyncio.run(meta.reqlog_fail_stale(6))
    assert swept == 1

    r1 = asyncio.run(meta.reqlog_get("r1"))
    assert r1["status"] == "failed"
    assert r1["result"]["error"]["type"] == "stale_submitted"
    assert "服务重启" in r1["result"]["error"]["message"]

    r2 = asyncio.run(meta.reqlog_get("r2"))
    assert r2["status"] == "submitted"  # 6 小时内的不动

    r3 = asyncio.run(meta.reqlog_get("r3"))
    assert r3["status"] == "failed"  # 已终态的不碰
