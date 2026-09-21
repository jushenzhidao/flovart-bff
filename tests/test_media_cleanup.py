"""媒体定期清理机制测试（2026-09-21「去配额上限、上清理机制」配套）。

覆盖：过期行筛选（新旧混存只删旧行）、字节+索引同步删除、dry-run 不动数据、
retention_days<=0 直接跳过。META 用真实 LocalMeta（tmp_path 隔离 DB），
BLOB 用桩（记录 delete 调用，不必真落盘）。
"""
import asyncio
import os

import pytest

from app import cloudstore, config
from app.cloudstore import LocalMeta, cleanup_expired_media


@pytest.fixture()
def local_meta(tmp_path, monkeypatch):
    """隔离的 LocalMeta + 桩 BLOB，避免碰全局单例与真实 data/ 目录。"""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    meta = LocalMeta()

    class _StubBlob:
        def __init__(self):
            self.objects = set()
            self.deletes = []

        async def put(self, uid, key, blob, mime, kind):
            self.objects.add(key)

        async def delete(self, uid, key):
            self.deletes.append(key)
            return key in self.objects

    stub = _StubBlob()
    monkeypatch.setattr(cloudstore, "META", meta)
    monkeypatch.setattr(cloudstore, "BLOB", stub)
    return meta, stub


def _seed(meta, uid, key, age_days, size=10):
    """直接插索引行，再把 created_at 改旧（media_index_put 只写当前时间）。"""
    async def _go():
        await meta.media_index_put(uid, key, "image", "image/png", size)
        def _backdate():
            import sqlite3
            cutoff = cloudstore.datetime.now(cloudstore.timezone.utc) - \
                cloudstore.timedelta(days=age_days)
            conn = sqlite3.connect(os.path.join(str(config.DATA_DIR), "flovart_cloud.db"))
            conn.execute("UPDATE cloud_media SET created_at=? WHERE media_key=?",
                         (cutoff.isoformat(timespec="seconds"), key))
            conn.commit()
            conn.close()
        await asyncio.to_thread(_backdate)
    asyncio.run(_go())


def test_cleanup_removes_only_expired(local_meta):
    meta, stub = local_meta
    _seed(meta, 1, "old", age_days=120)
    _seed(meta, 1, "new", age_days=1)
    result = asyncio.run(cleanup_expired_media(retention_days=90))
    assert result["deleted"] == 1 and result["errors"] == 0
    assert stub.deletes == ["old"]
    # 新媒体完好，旧行已删
    assert asyncio.run(meta.media_index_get(1, "new")) is not None
    assert asyncio.run(meta.media_index_get(1, "old")) is None


def test_cleanup_dry_run_touches_nothing(local_meta):
    meta, stub = local_meta
    _seed(meta, 2, "keeper", age_days=400)
    result = asyncio.run(cleanup_expired_media(retention_days=90, dry_run=True))
    assert result["dry_run"] is True and result["expired"] == 1
    assert stub.deletes == []
    assert asyncio.run(meta.media_index_get(2, "keeper")) is not None


def test_cleanup_disabled_when_retention_zero(local_meta):
    meta, stub = local_meta
    _seed(meta, 1, "anything", age_days=999)
    result = asyncio.run(cleanup_expired_media(retention_days=0))
    assert result.get("skipped") is True
    assert stub.deletes == []


def test_cleanup_counts_freed_bytes(local_meta):
    meta, _ = local_meta
    _seed(meta, 3, "big-old", age_days=100, size=1024)
    result = asyncio.run(cleanup_expired_media(retention_days=90))
    assert result["bytes"] == 1024


def test_quota_enforcement_default_off():
    """飞哥拍板：默认不限配额（ENFORCE=False），不再需要 .env 里设上限。"""
    assert config.OSS_ENFORCE_QUOTA is False


def test_cleanup_expires_across_uids(local_meta):
    """全局过期扫描不区分 uid：清理是全局的，各用户旧行都该入清单。"""
    meta, _ = local_meta
    _seed(meta, 7, "u7-old", age_days=200)
    _seed(meta, 9, "u9-old", age_days=200)
    result = asyncio.run(cleanup_expired_media(retention_days=90))
    assert result["deleted"] == 2
