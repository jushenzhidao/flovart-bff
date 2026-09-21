"""HybridBlob（OSS 为主 / 本地磁盘回退）行为测试。

背景（2026-09-21 历史媒体 404）：本地环境先 LocalBlob 积累存量媒体，后开启
OSS_ENABLED —— 模块级单例 BLOB 切到 OssBlob 后，media_get 只查桶，存量对象
不在桶里全部 404。修复 = BLOB 换成 HybridBlob（读 OSS miss → 落本地磁盘）。
"""
import asyncio

from app.cloudstore import HybridBlob


class _StubBlob:
    def __init__(self, name, has):
        self.name = name
        self.has = set(has)
        self.puts = []
        self.deletes = []

    async def put(self, uid, key, blob, mime, kind):
        self.puts.append((uid, key))
        self.has.add(key)

    async def get(self, uid, key):
        if key not in self.has:
            return None
        return {"who": self.name, "size": 1}

    async def delete(self, uid, key):
        self.deletes.append(key)
        if key in self.has:
            self.has.discard(key)
            return True
        return False


def _hybrid():
    primary = _StubBlob("oss", {"oss_only"})
    fallback = _StubBlob("local", {"local_only"})
    return HybridBlob(primary, fallback), primary, fallback


def test_hybrid_get_prefers_primary():
    h, primary, _ = _hybrid()
    assert asyncio.run(h.get(1, "oss_only")) == {"who": "oss", "size": 1}
    assert primary.has == {"oss_only"}


def test_hybrid_get_falls_back_on_miss():
    """存量本地媒体：OSS 查无 → 回退本地磁盘，不再 404。"""
    h, _, _ = _hybrid()
    assert asyncio.run(h.get(1, "local_only")) == {"who": "local", "size": 1}


def test_hybrid_get_none_when_both_miss():
    h, _, _ = _hybrid()
    assert asyncio.run(h.get(1, "ghost")) is None


def test_hybrid_put_goes_to_primary_only():
    """新写入统一进 OSS，不落本地（避免双写漂移）。"""
    h, primary, fallback = _hybrid()
    asyncio.run(h.put(1, "k", b"x", "image/png", "image"))
    assert "k" in primary.has
    assert "k" not in fallback.has


def test_hybrid_delete_hits_both():
    """删除两边各试一次：任一存在即删干净（OSS 删了、本地残留也清）。"""
    h, primary, fallback = _hybrid()
    assert asyncio.run(h.delete(1, "oss_only")) is True
    assert asyncio.run(h.delete(1, "local_only")) is True
    assert asyncio.run(h.delete(1, "ghost")) is False
    assert "oss_only" in primary.deletes and "local_only" in fallback.deletes
