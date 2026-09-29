"""计费错账修复回归（2026-09-24，飞哥拍板方案 B）。

实锤背景：new-api 对 role-10 管理员 POST /api/token/ 忽略 body 里的 user_id
（探针：带 user_id:116 建出来 user_id:183）→ 管理员代建的 sk- 永远归属管理员，
计费错账。因此 tasks._user_api_key 的发放顺序必须是：
  ① 复用已存 sk-（不 mint）；
  ② 用户 PAT 自建（na.mint_user_api_key，归属正确）；
  ③ 管理员代建兜底（na.admin_mint_user_api_key，账落管理员）。
"""

import asyncio

import pytest

from app import tasks
from app import user_keys as user_keys_mod
from app.newapi_client import NewApiError

UID = 116


def _patch(monkeypatch, *, stored=None, mint_fail=False, admin_calls=None, mint_calls=None):
    """统一桩：记录两类发放通道的调用；stored 模拟已存 sk-。"""

    async def fake_mint(pat, uid):
        assert pat == "user-pat-abc", "自建必须用会话里的用户 PAT"
        if mint_calls is not None:
            mint_calls.append((pat, uid))
        if mint_fail:
            raise NewApiError("user PAT rejected", 401)
        return "sk-user-minted"

    async def fake_admin_mint(uid):
        if admin_calls is not None:
            admin_calls.append(uid)
        return "sk-admin-minted"

    monkeypatch.setattr(user_keys_mod, "get_key", lambda uid: stored)
    monkeypatch.setattr(user_keys_mod, "set_key", lambda uid, key: None)
    monkeypatch.setattr(user_keys_mod, "delete_key", lambda uid: None)
    monkeypatch.setattr(tasks.na, "mint_user_api_key", fake_mint)
    monkeypatch.setattr(tasks.na, "admin_mint_user_api_key", fake_admin_mint)


def test_stored_key_reused_without_mint(monkeypatch):
    """① 已存 sk- 直接复用，不触发任何发放（行为与修复前一致）。"""
    admin_calls, mint_calls = [], []
    _patch(monkeypatch, stored="sk-stored", admin_calls=admin_calls, mint_calls=mint_calls)
    key = asyncio.run(tasks._user_api_key(UID, pat="user-pat-abc"))
    assert key == "sk-stored"
    assert not admin_calls and not mint_calls


def test_user_pat_self_mint_preferred(monkeypatch):
    """② 有用户 PAT → 走用户自建，绝不落管理员代建（计费归属正确的主路径）。"""
    admin_calls, mint_calls = [], []
    _patch(monkeypatch, admin_calls=admin_calls, mint_calls=mint_calls)
    key = asyncio.run(tasks._user_api_key(UID, pat="user-pat-abc"))
    assert key == "sk-user-minted"
    assert mint_calls == [("user-pat-abc", UID)]
    assert not admin_calls


def test_user_pat_401_falls_back_to_admin(monkeypatch):
    """③ 用户 PAT 失效(401) → 回落管理员代建（账落管理员，属预期降级）。"""
    admin_calls, mint_calls = [], []
    _patch(monkeypatch, mint_fail=True, admin_calls=admin_calls, mint_calls=mint_calls)
    key = asyncio.run(tasks._user_api_key(UID, pat="user-pat-abc"))
    assert key == "sk-admin-minted"
    assert mint_calls == [("user-pat-abc", UID)]  # 先尝试过自建
    assert admin_calls == [UID]


def test_no_pat_goes_admin_directly(monkeypatch):
    """无 PAT（后台任务历史路径）→ 直接管理员代建。"""
    admin_calls, mint_calls = [], []
    _patch(monkeypatch, admin_calls=admin_calls, mint_calls=mint_calls)
    key = asyncio.run(tasks._user_api_key(UID, pat=None))
    assert key == "sk-admin-minted"
    assert admin_calls == [UID]
    assert not mint_calls


def test_gw_call_rotation_keeps_pat(monkeypatch):
    """_gw_call 401 轮换时同样走「自建优先」：rotate 后第二次发放仍用用户 PAT。"""
    calls: list = []

    async def fake_mint(pat, uid):
        calls.append(("user", pat, uid))
        return "sk-self"

    async def fake_admin_mint(uid):
        calls.append(("admin", uid))
        return "sk-admin"

    # 第一次 mint 返回坏 key（网关 401），轮换后再次 mint 仍应走用户通道
    mint_seq = {"n": 0}

    async def fake_mint_rotating(pat, uid):
        mint_seq["n"] += 1
        calls.append(("mint", pat))
        return f"sk-bad-{mint_seq['n']}"

    async def fake_request(method, path, *, headers, json=None, params=None, client=None):
        raise NewApiError("Invalid token", 401)

    monkeypatch.setattr(user_keys_mod, "get_key", lambda uid: None)
    monkeypatch.setattr(user_keys_mod, "set_key", lambda uid, key: None)
    monkeypatch.setattr(user_keys_mod, "delete_key", lambda uid: None)
    monkeypatch.setattr(tasks.na, "mint_user_api_key", fake_mint_rotating)
    monkeypatch.setattr(tasks.na, "admin_mint_user_api_key", fake_admin_mint)
    monkeypatch.setattr(tasks.na, "request", fake_request)

    with pytest.raises(NewApiError):
        asyncio.run(tasks._gw_call("POST", "v1/images/generations", UID, pat="user-pat-abc"))
    # 两次发放都是用户自建（初始 + 轮换），管理员通道从未被触碰
    mints = [c for c in calls if c[0] == "mint"]
    assert mints == [("mint", "user-pat-abc"), ("mint", "user-pat-abc")]
    assert not [c for c in calls if c[0] == "admin"]
