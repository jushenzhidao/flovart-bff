"""rc.37 安全验证 proof 适配测试（2026-09-20 登录血案根因修复）。

new-api v1.0.0-rc.37 起 GET /api/user/token 无条件要求 X-Security-Proof
（scope=access_token.generate，无 2FA/Passkey 账号唯一方式是 password）。
缺失即 403「需要安全验证」——这正是两台 BFF 所有用户登录/注册全灭的根因。
"""

import asyncio
import json

import pytest

from app import config
from app import newapi_client as nc


# ---------------------------------------------------------------------------
# 工具：构造 rc.37 风格的登录响应
# ---------------------------------------------------------------------------
def _rc37_login_payload():
    return {"data": {"access_token": "sess-jwt",
                     "user": {"id": 64, "username": "adam"},
                     "session": {"sid": "s1"}}}


def test_login_mints_pat_with_proof(monkeypatch):
    """rc.37 网关：login → POST /api/verify 拿 proof → 带 X-Security-Proof 换 PAT。"""
    seen = {}

    async def fake_request(method, path, *, headers, json=None, params=None,
                           client=None, client_ip=None):
        seen[path] = {"method": method, "headers": headers, "json": json}
        if path == "/api/user/login":
            return _rc37_login_payload()
        if path == "/api/verify":
            assert json == {"method": "password",
                            "scope": "access_token.generate",
                            "password": "pw-123"}
            assert headers.get("Authorization") == "Bearer sess-jwt"
            assert headers.get("New-Api-User") == "64"
            return {"data": {"proof_token": "proof-tok", "scope":
                             "access_token.generate", "method": "password"}}
        if path == "/api/user/token":
            seen["proof_header"] = headers.get("X-Security-Proof", "")
            return {"data": "minted-pat"}
        if path == "/api/user/sessions/s1":
            return {}
        raise nc.NewApiError(f"unexpected {path}", 500)

    monkeypatch.setattr(nc, "request", fake_request)
    out = asyncio.run(nc.login("adam", "pw-123"))
    assert out["pat"] == "minted-pat"
    assert out["uid"] == 64
    assert seen["proof_header"] == "proof-tok"  # 换 PAT 必须带 proof


def test_login_old_gateway_without_verify_endpoint(monkeypatch):
    """旧网关无 /api/verify（未知路由兜底 SPA → 502）：跳过 proof 照常换 PAT。"""

    async def fake_request(method, path, *, headers, json=None, params=None,
                           client=None, client_ip=None):
        if path == "/api/user/login":
            return _rc37_login_payload()
        if path == "/api/verify":
            raise nc.NewApiError("网关未实现该端点", 502)
        if path == "/api/user/token":
            seen_headers = headers
            assert "X-Security-Proof" not in seen_headers
            return {"data": "legacy-pat"}
        if path == "/api/user/sessions/s1":
            return {}
        raise nc.NewApiError(f"unexpected {path}", 500)

    monkeypatch.setattr(nc, "request", fake_request)
    out = asyncio.run(nc.login("adam", "pw-123"))
    assert out["pat"] == "legacy-pat"


def test_login_verify_hard_failure_no_retry(monkeypatch):
    """验证接口非 404/502 失败（如密码校验失败 400）→ 原样抛出，绝不重试。"""
    calls = {"verify": 0}

    async def fake_request(method, path, *, headers, json=None, params=None,
                           client=None, client_ip=None):
        if path == "/api/user/login":
            return _rc37_login_payload()
        if path == "/api/verify":
            calls["verify"] += 1
            raise nc.NewApiError("验证失败", 400)
        raise nc.NewApiError(f"unexpected {path}", 500)

    monkeypatch.setattr(nc, "request", fake_request)
    with pytest.raises(nc.NewApiError) as ei:
        asyncio.run(nc.login("adam", "wrong-pw"))
    assert calls["verify"] == 1  # 只试一次（该端点有失败计数，重试会锁号）
    assert ei.value.status_code == 400


def test_login_releases_session_after_mint(monkeypatch):
    """换完 PAT 必须归还登录会话（会话是稀缺资源）。"""
    released = []

    async def fake_request(method, path, *, headers, json=None, params=None,
                           client=None, client_ip=None):
        if path == "/api/user/login":
            return _rc37_login_payload()
        if path == "/api/verify":
            return {"data": {"proof_token": "proof-tok"}}
        if path == "/api/user/token":
            return {"data": "minted-pat"}
        if path == "/api/user/sessions/s1":
            released.append(1)
            return {}
        raise nc.NewApiError(f"unexpected {path}", 500)

    monkeypatch.setattr(nc, "request", fake_request)
    asyncio.run(nc.login("adam", "pw-123"))
    assert released == [1]


def test_admin_login_uses_proof_flow(monkeypatch, tmp_path):
    """管理员轮换链路同样走 proof（rc.37 下 _admin_login 不带 proof 必 403）。"""
    monkeypatch.setattr(config, "NEWAPI_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "NEWAPI_ADMIN_PASSWORD", "admin-pw")
    monkeypatch.setattr(config, "ADMIN_CRED_FILE", str(tmp_path / "cred.json"))
    proof_headers = {}

    async def fake_request(method, path, *, headers, json=None, params=None,
                           client=None, client_ip=None):
        if path == "/api/user/login":
            return {"data": {"user": {"id": 1}, "access_token": "sess-jwt",
                             "session": {"sid": "s1"}}}
        if path == "/api/verify":
            assert json["password"] == "admin-pw"
            return {"data": {"proof_token": "proof-tok"}}
        if path == "/api/user/token":
            proof_headers.update(headers)
            return {"data": "new-admin-pat"}
        if path == "/api/user/sessions/s1":
            return {}
        raise nc.NewApiError(f"unexpected {path}", 500)

    monkeypatch.setattr(nc, "request", fake_request)
    asyncio.run(nc._admin_login())
    assert nc._admin_cache["pat"] == "new-admin-pat"
    assert proof_headers.get("X-Security-Proof") == "proof-tok"
    with open(config.ADMIN_CRED_FILE, encoding="utf-8") as f:
        assert json.load(f)["pat"] == "new-admin-pat"
