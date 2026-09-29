"""平台 Key 计费模式开关（飞哥 2026-09-28）。

## 需求原文

> 我现在需要的使用户计费，但是真实调用走的是管理员配置的AI服务，
> 所以用户不会真实扣费用啊，你做一个开关或者配置，
> 后续需要真实走用户自己的key扣费

## 守护的行为

1. shared（默认）＝现状：GET /api/platform/services 原样下发管理员配置的
   key/baseUrl（选 B 语义），真实调用走平台侧凭证，用户不真实扣费。
2. user（真实扣费）：普通用户的 GET 里，上架条目的 key 替换为【调用者自己】
   的平台 Key（keys._ensure_token_plain，扣用户自己配额），baseUrl 替换为
   网关基址；替换失败剥 key，绝不静默回退管理员 key（防漏费）。
3. 管理员会话**不替换**：管理端（发布/下架/重新上架）会回读条目再 PUT，
   替换会把管理员个人 key 存进共享配置造成污染。
4. 下架条目（suspended）在两种模式下都保持凭据剥除。
5. 云端覆盖（doc_key='config'）> env 初始默认；非法模式被拒绝。
"""
import asyncio

import pytest

from app import cloudstore, config
from app.routers import console as console_router
from app.routers import platform_services

ADMIN_KEY = "sk-admin-shared-key"
ADMIN_BASE = "https://api.chatfire.example/v1"
USER_KEY = "sk-user-own-key"


class _DocStore:
    """按 doc_key 分桶的内存版 cloud_docs（services 与 config 互不串）."""

    def __init__(self):
        self.docs: dict = {}

    async def doc_get(self, uid, scope, doc_key):
        return self.docs.get(doc_key)

    async def doc_put(self, uid, scope, doc_key, payload):
        old = self.docs.get(doc_key) or {}
        rev = int(old.get("revision", 0)) + 1
        doc = {"payload": payload, "revision": rev, "updated_at": "2026-09-28T00:00:00Z"}
        self.docs[doc_key] = doc
        return {"revision": rev, "updated_at": doc["updated_at"]}


def _seed_services(store: _DocStore) -> None:
    store.docs["services"] = {
        "payload": {
            "services": [
                {"id": "s1", "provider": "openai_compatible", "name": "平台模型",
                 "capabilities": ["image"], "models": ["gpt-image-2"],
                 "key": ADMIN_KEY, "baseUrl": ADMIN_BASE},
                {"id": "s2", "provider": "openai_compatible", "name": "已下架",
                 "capabilities": ["image"], "models": ["old-model"],
                 "suspended": True},
            ],
            "revoked": {},
        },
        "revision": 3,
        "updated_at": "2026-09-28T00:00:00Z",
    }


@pytest.fixture()
def store(monkeypatch):
    s = _DocStore()
    monkeypatch.setattr(cloudstore, "doc_get", s.doc_get)
    monkeypatch.setattr(cloudstore, "doc_put", s.doc_put)
    _seed_services(s)
    yield s


@pytest.fixture()
def fake_user_key(monkeypatch):
    """替换 keys._ensure_token_plain（_apply_user_billing 调用处动态 import）。"""
    from app.routers import keys as keys_router

    async def _fake(session: dict) -> dict:
        return {"id": 7, "name": "平台默认 Key", "key": USER_KEY, "status": 1}

    monkeypatch.setattr(keys_router, "_ensure_token_plain", _fake)


USER_SESSION = {"uid": 116, "username": "user116", "pat": "pat-u", "role": 1}
ADMIN_SESSION = {"uid": 1, "username": "admin", "pat": "pat-a", "role": 10}


def _get(session):
    return asyncio.run(platform_services.list_platform_services(session))["data"]


# ---------------------------------------------------------------------------
# 1) shared（默认）：原样下发管理员 key
# ---------------------------------------------------------------------------
def test_default_shared_keeps_admin_key(store, fake_user_key):
    out = _get(USER_SESSION)
    assert out["keyMode"] == "shared"
    live = next(i for i in out["items"] if i["id"] == "s1")
    assert live["key"] == ADMIN_KEY
    assert live["baseUrl"] == ADMIN_BASE
    assert live["keyPresent"] is True


def test_get_key_mode_falls_back_to_env(store):
    assert asyncio.run(platform_services.get_key_mode()) == config.PLATFORM_KEY_MODE


# ---------------------------------------------------------------------------
# 2) user：普通用户的条目替换为自有 Key + 网关基址
# ---------------------------------------------------------------------------
def test_user_mode_substitutes_key_and_base_url(store, fake_user_key):
    asyncio.run(platform_services.set_key_mode("user"))
    out = _get(USER_SESSION)
    assert out["keyMode"] == "user"
    live = next(i for i in out["items"] if i["id"] == "s1")
    assert live["key"] == USER_KEY
    assert live["key"] != ADMIN_KEY
    assert live["baseUrl"] == config.API_BASE_URL
    assert live["keyPresent"] is True


def test_user_mode_keeps_admin_key_for_admin_session(store, fake_user_key):
    """管理员会话不替换：管理端回读-再发布不能把个人 key 写进共享配置。"""
    asyncio.run(platform_services.set_key_mode("user"))
    out = _get(ADMIN_SESSION)
    live = next(i for i in out["items"] if i["id"] == "s1")
    assert live["key"] == ADMIN_KEY
    assert live["baseUrl"] == ADMIN_BASE


def test_user_mode_suspended_entry_stays_stripped(store, fake_user_key):
    asyncio.run(platform_services.set_key_mode("user"))
    out = _get(USER_SESSION)
    dead = next(i for i in out["items"] if i["id"] == "s2")
    assert "key" not in dead and "baseUrl" not in dead
    assert dead["keyPresent"] is False


def test_user_mode_ensure_failure_strips_key_never_admin_key(store, monkeypatch):
    """拿不到用户 Key → 剥 key（宁可失败绝不静默回退管理员 key 防漏费）。"""
    from app.routers import keys as keys_router

    async def _boom(session: dict) -> dict:
        raise RuntimeError("new-api down")

    monkeypatch.setattr(keys_router, "_ensure_token_plain", _boom)
    asyncio.run(platform_services.set_key_mode("user"))
    out = _get(USER_SESSION)
    live = next(i for i in out["items"] if i["id"] == "s1")
    assert "key" not in live and "baseUrl" not in live
    assert live["keyPresent"] is False
    assert ADMIN_KEY not in {i.get("key") for i in out["items"] if isinstance(i, dict)}


# ---------------------------------------------------------------------------
# 3) 模式存取与校验
# ---------------------------------------------------------------------------
def test_set_key_mode_rejects_invalid(store):
    with pytest.raises(ValueError):
        asyncio.run(platform_services.set_key_mode("bogus"))
    # 拒绝后模式保持 env 默认，未被污染
    assert asyncio.run(platform_services.get_key_mode()) == config.PLATFORM_KEY_MODE


def test_set_key_mode_roundtrip(store):
    asyncio.run(platform_services.set_key_mode("user"))
    assert asyncio.run(platform_services.get_key_mode()) == "user"
    asyncio.run(platform_services.set_key_mode("shared"))
    assert asyncio.run(platform_services.get_key_mode()) == "shared"


# ---------------------------------------------------------------------------
# 4) 管理台接口
# ---------------------------------------------------------------------------
def test_console_get_and_put_key_mode(store):
    got = asyncio.run(console_router.console_platform_key_mode(ADMIN_SESSION))
    assert got["data"]["mode"] == config.PLATFORM_KEY_MODE

    put = asyncio.run(console_router.console_set_platform_key_mode(
        console_router.PlatformKeyModeBody(mode="user"), ADMIN_SESSION))
    assert put["success"] is True
    assert asyncio.run(platform_services.get_key_mode()) == "user"


def test_console_put_invalid_mode_returns_400(store):
    res = asyncio.run(console_router.console_set_platform_key_mode(
        console_router.PlatformKeyModeBody(mode="free"), ADMIN_SESSION))
    assert res.status_code == 400
