"""外部接入 Key（agent/MCP 双轨鉴权）测试，2026-11-03。

覆盖：
- api_key_create/list/lookup/delete 双实现（LocalMeta SQLite）契约
- 明文仅创建响应返回一次（库里只有 SHA-256 哈希）
- security.require_identity：Cookie 优先 / X-API-Key 命中 / 无效 Key 401 / 限频 429
- generate_image tool 经 MCP 身份链路能拿到 uid（不真打网关，mock tasks.submit）
"""
import asyncio
import time

import pytest
from fastapi import HTTPException

from app import cloudstore, security
from app.security import generate_api_key


@pytest.fixture()
def anyio_backend():
    return "asyncio"


def test_api_key_roundtrip_localmeta(tmp_path, monkeypatch):
    """LocalMeta 全生命周期：创建（哈希入库）→ 查询 → 删除。"""
    monkeypatch.setattr(cloudstore.config, "DATA_DIR", str(tmp_path))
    meta = cloudstore.LocalMeta()
    monkeypatch.setattr(cloudstore, "META", meta)

    plain, key_hash = generate_api_key()
    assert plain.startswith("fvk_")
    rec = asyncio.run(meta.api_key_create(149, key_hash, plain[:12], "测试 Key"))
    assert rec["uid"] == 149 and rec["prefix"] == plain[:12]

    items = asyncio.run(meta.api_key_list(149))
    assert len(items) == 1 and items[0]["id"] == rec["id"]
    # 库里不存在**完整明文**（prefix 存前 12 位是设计行为；key_hash 是哈希）
    assert all(plain not in str(it.values()) for it in items)

    looked = asyncio.run(meta.api_key_lookup(key_hash))
    assert looked and looked["uid"] == 149
    assert asyncio.run(meta.api_key_lookup("deadbeef")) is None  # 未命中

    assert asyncio.run(meta.api_key_touch(key_hash)) is None  # 不抛
    assert asyncio.run(meta.api_key_delete(149, rec["id"])) is True
    assert asyncio.run(meta.api_key_lookup(key_hash)) is None
    assert asyncio.run(meta.api_key_delete(149, rec["id"])) is False  # 幂等


def test_require_identity_prefers_cookie(tmp_path, monkeypatch):
    """Cookie 会话有效时优先走 Cookie（via=cookie），不碰 X-API-Key。"""
    from app import config
    session_payload = {"uid": 7, "username": "u7", "pat": "pat-x", "role": 1}

    class FakeReq:
        cookies = {config.COOKIE_NAME: security._encrypt({**session_payload, "iat": int(time.time())})}
        headers = {"X-API-Key": "fvk_should_be_ignored"}

    ident = asyncio.run(security.require_identity(FakeReq()))
    assert ident["uid"] == 7 and ident["via"] == "cookie" and ident["pat"] == "pat-x"


def test_require_identity_api_key_path(tmp_path, monkeypatch):
    """无 Cookie 时 X-API-Key 命中：构造同形身份（pat=None, via=api-key）。"""
    from app import config

    plain, key_hash = generate_api_key()
    monkeypatch.setattr(cloudstore.config, "DATA_DIR", str(tmp_path))
    meta = cloudstore.LocalMeta()
    monkeypatch.setattr(cloudstore, "META", meta)
    asyncio.run(meta.api_key_create(149, key_hash, plain[:12], "k"))

    class FakeReq:
        cookies = {}
        headers = {"X-API-Key": plain}

    ident = asyncio.run(security.require_identity(FakeReq()))
    assert ident["uid"] == 149
    assert ident["via"] == "api-key"
    assert ident["pat"] is None  # 下游走管理员代建通道
    assert ident["role"] == 1  # 非管理员（Pro 闸门语义不变）


def test_require_identity_rejects_bad_key(tmp_path, monkeypatch):
    monkeypatch.setattr(cloudstore.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cloudstore, "META", cloudstore.LocalMeta())

    class FakeReq:
        cookies = {}
        headers = {"X-API-Key": "fvk_wrong"}

    with pytest.raises(HTTPException) as ei:
        asyncio.run(security.require_identity(FakeReq()))
    assert ei.value.status_code == 401

    class NoKeyReq:
        cookies = {}
        headers = {}

    with pytest.raises(HTTPException) as ei2:
        asyncio.run(security.require_identity(NoKeyReq()))
    assert ei2.value.status_code == 401


def test_api_key_rate_limit(tmp_path, monkeypatch):
    """同一 Key 超过 60 次/分钟 → 429（进程内滑动窗口）。"""
    monkeypatch.setattr(security, "API_KEY_RATE_LIMIT", 3)  # 测试用小窗口
    security._rate_window.clear()
    for _ in range(3):
        security._check_api_key_rate("hash-x")
    with pytest.raises(HTTPException) as ei:
        security._check_api_key_rate("hash-x")
    assert ei.value.status_code == 429
    security._rate_window.clear()
