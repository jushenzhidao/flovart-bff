"""管理员 PAT 多应用共存自愈机制（2026-09-20 根治互踢）。

背景：flovart-bff 与 newapi-bff(hewapi) 共用同一管理员账号 + 同一 PAT，连同一台
new-api 网关。旧逻辑 401 时盲目 `_admin_login()`（GET /api/user/token 会**轮换**
PAT）→ 两边互相作废对方 PAT → 乒乓循环 → 50 会话/100 签发双限打爆。

根治三件套（本文件守护）：
1. 401 先重读共享凭据文件 `_reload_admin_cred()` —— 对端可能已轮换并落盘，直接采纳；
2. 确需轮换时锁内单飞（并发 401 只 login 一次）+ 新 PAT 强制落盘（env 直供也写）；
3. `NEWAPI_ADMIN_LOGIN_FALLBACK=0` 可禁用自动轮换，401 直接报错转人工。
"""
import asyncio
import json
import os
import time

import pytest

from app import config, newapi_client as nc


@pytest.fixture(autouse=True)
def _reset_cache(tmp_path, monkeypatch):
    """每个用例独立的缓存/凭据文件/配置。"""
    nc._admin_cache["pat"] = None
    nc._admin_cache["uid"] = None
    monkeypatch.setattr(config, "ADMIN_CRED_FILE", str(tmp_path / "admin_cred.json"))
    monkeypatch.setattr(config, "NEWAPI_ADMIN_PAT", "")
    monkeypatch.setattr(config, "NEWAPI_ADMIN_UID", 0)
    monkeypatch.setattr(config, "NEWAPI_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "NEWAPI_ADMIN_PASSWORD", "pw")
    monkeypatch.setattr(config, "NEWAPI_ADMIN_LOGIN_FALLBACK", True)
    monkeypatch.setattr(config, "NEWAPI_ADMIN_ROTATE_COOLDOWN", 900)
    nc._LAST_ADMIN_ROTATE = None
    yield
    nc._admin_cache["pat"] = None
    nc._admin_cache["uid"] = None
    nc._LAST_ADMIN_ROTATE = None


def _write_cred(path, pat, uid=1):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pat": pat, "uid": uid}, f)


# ---------------------------------------------------------------------------
# 1) _reload_admin_cred：磁盘新值采纳 / 无新值返回 False
# ---------------------------------------------------------------------------
def test_reload_adopts_new_pat_from_file():
    nc._admin_cache.update(pat="old-pat", uid=1)
    _write_cred(config.ADMIN_CRED_FILE, "new-pat", 1)
    assert nc._reload_admin_cred() is True
    assert nc._admin_cache["pat"] == "new-pat"


def test_reload_no_change_returns_false(tmp_path):
    nc._admin_cache.update(pat="same-pat", uid=1)
    _write_cred(config.ADMIN_CRED_FILE, "same-pat", 1)
    assert nc._reload_admin_cred() is False


def test_reload_missing_file_returns_false(tmp_path):
    nc._admin_cache.update(pat="x", uid=1)
    assert nc._reload_admin_cred() is False


# ---------------------------------------------------------------------------
# 2) _save_admin_cred：轮换强制落盘（env 直供也必须写）
# ---------------------------------------------------------------------------
def test_save_force_writes_even_with_env_pat(monkeypatch):
    monkeypatch.setattr(config, "NEWAPI_ADMIN_PAT", "env-pat")
    nc._admin_cache.update(pat="rotated-pat", uid=1)
    nc._save_admin_cred(force=True)
    with open(config.ADMIN_CRED_FILE, encoding="utf-8") as f:
        assert json.load(f)["pat"] == "rotated-pat"


def test_save_not_force_skips_when_env_pat(monkeypatch):
    monkeypatch.setattr(config, "NEWAPI_ADMIN_PAT", "env-pat")
    nc._admin_cache.update(pat="whatever", uid=1)
    nc._save_admin_cred()
    assert not os.path.exists(config.ADMIN_CRED_FILE)  # 冷启冗余保存不写文件


# ---------------------------------------------------------------------------
# 3) _self_heal_admin_cred：三段式
# ---------------------------------------------------------------------------
def test_self_heal_adopts_file_pat_without_login(monkeypatch):
    """对端已轮换并落盘 → 直接采纳，绝不 login。"""
    nc._admin_cache.update(pat="stale-pat", uid=1)
    _write_cred(config.ADMIN_CRED_FILE, "peer-rotated-pat", 1)
    login_calls: list = []

    async def fake_login():
        login_calls.append(1)

    monkeypatch.setattr(nc, "_admin_login", fake_login)
    asyncio.run(nc._self_heal_admin_cred())
    assert nc._admin_cache["pat"] == "peer-rotated-pat"
    assert login_calls == []


def test_self_heal_falls_back_to_login_once(monkeypatch):
    """文件无新值 → 兜底 login 一次；login 内部会强制落盘（此处 mock）。"""
    nc._admin_cache.update(pat="stale-pat", uid=1)
    login_calls: list = []

    async def fake_login():
        login_calls.append(1)
        nc._admin_cache["pat"] = "freshly-rotated"

    monkeypatch.setattr(nc, "_admin_login", fake_login)
    asyncio.run(nc._self_heal_admin_cred())
    assert login_calls == [1]
    assert nc._admin_cache["pat"] == "freshly-rotated"


def test_self_heal_disabled_raises_without_login(monkeypatch):
    monkeypatch.setattr(config, "NEWAPI_ADMIN_LOGIN_FALLBACK", False)
    nc._admin_cache.update(pat="stale-pat", uid=1)
    login_calls: list = []

    async def fake_login():
        login_calls.append(1)

    monkeypatch.setattr(nc, "_admin_login", fake_login)
    with pytest.raises(nc.NewApiError):
        asyncio.run(nc._self_heal_admin_cred())
    assert login_calls == []


def test_self_heal_concurrent_401s_login_only_once(monkeypatch):
    """并发 401：单飞锁保证只 login 一次，其余协程双检后复用结果。"""
    nc._admin_cache.update(pat="stale-pat", uid=1)
    login_calls: list = []

    async def fake_login():
        login_calls.append(1)
        await asyncio.sleep(0.01)  # 模拟登录耗时，放大竞态窗口
        nc._admin_cache["pat"] = "freshly-rotated"
        _write_cred(config.ADMIN_CRED_FILE, "freshly-rotated", 1)

    monkeypatch.setattr(nc, "_admin_login", fake_login)

    async def _main():
        await asyncio.gather(*[nc._self_heal_admin_cred() for _ in range(5)])

    asyncio.run(_main())
    assert len(login_calls) == 1
    assert nc._admin_cache["pat"] == "freshly-rotated"


# ---------------------------------------------------------------------------
# 3.5) 轮换冷静期熔断（2026-09-22 三应用共用 uid=1 互踢血案）
# ---------------------------------------------------------------------------
def test_cooldown_blocks_second_rotation(monkeypatch):
    """冷静期内已轮换过仍 401 → 拒绝再轮换、抛 503（防三应用乒乓烧额度）。"""
    nc._admin_cache.update(pat="stale-pat", uid=1)
    nc._LAST_ADMIN_ROTATE = time.monotonic() - 60  # 60s 前刚轮换过，冷静期 900s
    login_calls: list = []

    async def fake_login():
        login_calls.append(1)

    monkeypatch.setattr(nc, "_admin_login", fake_login)
    with pytest.raises(nc.NewApiError) as ei:
        asyncio.run(nc._self_heal_admin_cred())
    assert ei.value.status_code == 503
    assert login_calls == []


def test_cooldown_expired_allows_rotation(monkeypatch):
    """冷静期过后再次 401 → 允许兜底轮换（自愈能力不丢，只是限频）。"""
    nc._admin_cache.update(pat="stale-pat", uid=1)
    nc._LAST_ADMIN_ROTATE = time.monotonic() - 901  # 恰好超出 900s 冷静期
    login_calls: list = []

    async def fake_login():
        login_calls.append(1)
        nc._admin_cache["pat"] = "freshly-rotated"

    monkeypatch.setattr(nc, "_admin_login", fake_login)
    asyncio.run(nc._self_heal_admin_cred())
    assert login_calls == [1]
    assert nc._admin_cache["pat"] == "freshly-rotated"


def test_cooldown_disabled_allows_immediate_rotation(monkeypatch):
    """NEWAPI_ADMIN_ROTATE_COOLDOWN=0（单应用独占账号）→ 行为与旧版一致。"""
    monkeypatch.setattr(config, "NEWAPI_ADMIN_ROTATE_COOLDOWN", 0)
    nc._admin_cache.update(pat="stale-pat", uid=1)
    nc._LAST_ADMIN_ROTATE = time.monotonic()  # 刚轮换过也不拦
    login_calls: list = []

    async def fake_login():
        login_calls.append(1)
        nc._admin_cache["pat"] = "freshly-rotated"

    monkeypatch.setattr(nc, "_admin_login", fake_login)
    asyncio.run(nc._self_heal_admin_cred())
    assert login_calls == [1]


def test_cooldown_not_triggered_by_file_adoption(monkeypatch):
    """冷静期内但对端已落盘新值 → 采纳文件即可，不触发熔断也不 login。"""
    nc._admin_cache.update(pat="stale-pat", uid=1)
    nc._LAST_ADMIN_ROTATE = time.monotonic() - 10
    _write_cred(config.ADMIN_CRED_FILE, "peer-rotated-pat", 1)
    login_calls: list = []

    async def fake_login():
        login_calls.append(1)

    monkeypatch.setattr(nc, "_admin_login", fake_login)
    asyncio.run(nc._self_heal_admin_cred())
    assert nc._admin_cache["pat"] == "peer-rotated-pat"
    assert login_calls == []


def test_fresh_process_never_rotated_not_blocked(monkeypatch):
    """全新进程从未轮换过（_LAST_ADMIN_ROTATE=None）→ 不熔断，正常兜底轮换。

    回归 CI 血案（2026-09-22）：旧实现初始化成 0.0，`monotonic() - 0.0` 在
    开机几十秒的全新 runner/容器上被误判成「47s 前刚轮换过」→ 全部用例被
    熔断 503。None 哨兵 + 本用例双重防回归。
    """
    nc._admin_cache.update(pat="stale-pat", uid=1)
    assert nc._LAST_ADMIN_ROTATE is None  # fixture 保证：从未轮换
    login_calls: list = []

    async def fake_login():
        login_calls.append(1)
        nc._admin_cache["pat"] = "freshly-rotated"

    monkeypatch.setattr(nc, "_admin_login", fake_login)
    asyncio.run(nc._self_heal_admin_cred())
    assert login_calls == [1]
    assert nc._admin_cache["pat"] == "freshly-rotated"


# ---------------------------------------------------------------------------
# 3.6) 401 语义分流（2026-09-22）：账号状态类 401 绝不轮换（互踢点火源之一）
# ---------------------------------------------------------------------------
def test_auth_code_helper_parses_detail():
    e = nc.NewApiError("x", 401, detail="upstream_auth_code=AUTH_USER_DISABLED")
    assert nc._auth_code_from_error(e) == "AUTH_USER_DISABLED"
    assert nc._auth_code_from_error(nc.NewApiError("x", 401)) == ""
    assert nc._auth_code_from_error(nc.NewApiError("x", 401, detail="乱写")) == ""


def test_no_rotate_code_rejects_rotation(monkeypatch):
    """上游明确说 401 是账号状态问题（封禁等）→ 第 0 步即 503，绝不 login/轮换。"""
    nc._admin_cache.update(pat="stale-pat", uid=1)
    _write_cred(config.ADMIN_CRED_FILE, "peer-rotated-pat", 1)  # 连文件采纳也不该做
    login_calls: list = []

    async def fake_login():
        login_calls.append(1)

    monkeypatch.setattr(nc, "_admin_login", fake_login)
    with pytest.raises(nc.NewApiError) as ei:
        asyncio.run(nc._self_heal_admin_cred("AUTH_USER_DISABLED"))
    assert ei.value.status_code == 503
    assert login_calls == []
    assert nc._admin_cache["pat"] == "stale-pat"  # 缓存未被改动


def test_token_invalid_code_still_rotates(monkeypatch):
    """AUTH_UNAUTHORIZED（令牌值真的错了）→ 不受分流影响，正常走自愈兜底轮换。"""
    nc._admin_cache.update(pat="stale-pat", uid=1)
    login_calls: list = []

    async def fake_login():
        login_calls.append(1)
        nc._admin_cache["pat"] = "freshly-rotated"

    monkeypatch.setattr(nc, "_admin_login", fake_login)
    asyncio.run(nc._self_heal_admin_cred("AUTH_UNAUTHORIZED"))
    assert login_calls == [1]
    assert nc._admin_cache["pat"] == "freshly-rotated"


def test_unknown_code_keeps_legacy_behavior(monkeypatch):
    """旧版上游 401 无结构化 code → 维持旧行为（尝试自愈），由冷静期限频兜底。"""
    nc._admin_cache.update(pat="stale-pat", uid=1)
    login_calls: list = []

    async def fake_login():
        login_calls.append(1)
        nc._admin_cache["pat"] = "freshly-rotated"

    monkeypatch.setattr(nc, "_admin_login", fake_login)
    asyncio.run(nc._self_heal_admin_cred(""))
    assert login_calls == [1]


def test_admin_request_401_account_disabled_no_rotation(monkeypatch):
    """全链路：401 body 带 AUTH_USER_DISABLED → admin_request 抛 503，全程未轮换。"""
    nc._admin_cache.update(pat="stale-env-pat", uid=1)
    monkeypatch.setattr(config, "NEWAPI_ADMIN_PAT", "stale-env-pat")
    login_calls: list = []

    async def fake_login():
        login_calls.append(1)

    async def fake_request(method, path, *, headers, json=None, params=None, client=None,
                           client_ip=None):
        raise nc.NewApiError("unauthorized", 401, detail="upstream_auth_code=AUTH_USER_DISABLED")

    monkeypatch.setattr(nc, "_admin_login", fake_login)
    monkeypatch.setattr(nc, "request", fake_request)
    with pytest.raises(nc.NewApiError) as ei:
        asyncio.run(nc.admin_request("GET", "/api/user/self"))
    assert ei.value.status_code == 503
    assert login_calls == []



def test_admin_request_401_recovers_from_shared_file(monkeypatch):
    """模拟：env PAT 失效，但对端 BFF 已轮换并落盘 —— 不 login、直接用新 PAT 重试。"""
    nc._admin_cache.update(pat="stale-env-pat", uid=1)
    monkeypatch.setattr(config, "NEWAPI_ADMIN_PAT", "stale-env-pat")
    _write_cred(config.ADMIN_CRED_FILE, "peer-rotated-pat", 1)

    seen_headers: list = []

    async def fake_request(method, path, *, headers, json=None, params=None, client=None):
        auth = headers.get("Authorization", "")
        seen_headers.append(auth)
        if auth == "Bearer stale-env-pat":
            raise nc.NewApiError("unauthorized", 401)
        return {"ok": True}

    monkeypatch.setattr(nc, "request", fake_request)
    result = asyncio.run(nc.admin_request("GET", "/api/user/self"))
    assert result == {"ok": True}
    assert seen_headers == ["Bearer stale-env-pat", "Bearer peer-rotated-pat"]


def test_admin_request_401_rotates_and_persists(monkeypatch):
    """文件也无新值 → 锁内 login 轮换 → 新 PAT 强制落盘（env 直供也写）。"""
    nc._admin_cache.update(pat="stale-env-pat", uid=1)
    monkeypatch.setattr(config, "NEWAPI_ADMIN_PAT", "stale-env-pat")
    monkeypatch.setattr(config, "NEWAPI_ADMIN_UID", 1)

    async def fake_request(method, path, *, headers, json=None, params=None, client=None,
                           client_ip=None):
        auth = headers.get("Authorization", "")
        if path == "/api/user/login":
            return {"data": {"user": {"id": 1}, "access_token": "at",
                             "session": {"sid": "s1"}}}
        if path == "/api/user/token":
            return {"data": "brand-new-pat"}
        if path == "/api/user/sessions/s1":
            return {}
        if auth == "Bearer stale-env-pat":
            raise nc.NewApiError("unauthorized", 401)
        return {"ok": True}

    monkeypatch.setattr(nc, "request", fake_request)
    result = asyncio.run(nc.admin_request("GET", "/api/user/self"))
    assert result == {"ok": True}
    with open(config.ADMIN_CRED_FILE, encoding="utf-8") as f:
        assert json.load(f)["pat"] == "brand-new-pat"


# ---------------------------------------------------------------------------
# 5) 读回恢复（跨服务器部署的核心，2026-09-20）：读 access_token 而非轮换
# ---------------------------------------------------------------------------
def _fake_readback_request(token_calls, *, self_token="read-back-pat", self_path_seen=None):
    """构造读回场景的 request mock：login → self（读回）→ sessions 归还。

    /api/user/token（轮换端点）一旦被调用就记录 —— 读回机制下它绝不能出现。
    """
    async def fake_request(method, path, *, headers, json=None, params=None, client=None):
        if path == "/api/user/login":
            return {"data": {"user": {"id": 1}, "access_token": "sess-at",
                             "session": {"sid": "s1"}}}
        if path == "/api/user/self":
            if self_path_seen is not None:
                self_path_seen.append(headers.get("Authorization", ""))
            return {"data": {"id": 1, "access_token": self_token}}
        if path == "/api/user/sessions/s1":
            return {}
        if path == "/api/user/token":
            token_calls.append(1)  # 轮换端点被调用 = 违背读回初衷
            return {"data": "rotated-pat"}
        if headers.get("Authorization", "") == "Bearer stale-env-pat":
            raise nc.NewApiError("unauthorized", 401)
        return {"ok": True}
    return fake_request


def test_self_heal_readback_recovers_without_rotation(monkeypatch, tmp_path):
    """跨机场景：文件无新值 → login 后从 /api/user/self 读回现行 PAT，绝不轮换。"""
    monkeypatch.setattr(config, "NEWAPI_ADMIN_PAT_READBACK", True)
    monkeypatch.setattr(config, "NEWAPI_ADMIN_UID", 1)
    nc._admin_cache.update(pat="stale-env-pat", uid=1)
    token_calls: list = []

    monkeypatch.setattr(nc, "request",
                        _fake_readback_request(token_calls, self_token="read-back-pat"))
    asyncio.run(nc._self_heal_admin_cred())
    assert nc._admin_cache["pat"] == "read-back-pat"
    assert nc._admin_cache["uid"] == 1
    assert token_calls == []  # 全程未触碰轮换端点
    # 读回的 PAT 也强制落盘（同机其他实例可重读自愈）
    with open(config.ADMIN_CRED_FILE, encoding="utf-8") as f:
        assert json.load(f)["pat"] == "read-back-pat"


def test_self_heal_readback_empty_falls_to_rotation(monkeypatch):
    """账号从未设置过 access_token（读回为空）→ 才走轮换兜底。"""
    monkeypatch.setattr(config, "NEWAPI_ADMIN_PAT_READBACK", True)
    monkeypatch.setattr(config, "NEWAPI_ADMIN_UID", 1)
    nc._admin_cache.update(pat="stale-env-pat", uid=1)
    token_calls: list = []

    async def fake_login():
        nc._admin_cache["pat"] = "rotated-pat"

    monkeypatch.setattr(nc, "_admin_login", fake_login)
    monkeypatch.setattr(nc, "request",
                        _fake_readback_request(token_calls, self_token=""))
    asyncio.run(nc._self_heal_admin_cred())
    assert nc._admin_cache["pat"] == "rotated-pat"  # 兜底轮换生效
    assert token_calls == []  # 轮换发生在 mock 的 _admin_login 内，request 层未触达


def test_self_heal_readback_failure_falls_to_rotation(monkeypatch):
    """读回中途抛错（如 self 接口 401/网络错误）→ 不炸，回落轮换兜底。"""
    monkeypatch.setattr(config, "NEWAPI_ADMIN_PAT_READBACK", True)
    monkeypatch.setattr(config, "NEWAPI_ADMIN_UID", 1)
    nc._admin_cache.update(pat="stale-env-pat", uid=1)
    token_calls: list = []

    async def fake_login():
        nc._admin_cache["pat"] = "rotated-pat"

    async def broken_request(method, path, *, headers, json=None, params=None, client=None):
        if path == "/api/user/login":
            return {"data": {"user": {"id": 1}, "access_token": "sess-at",
                             "session": {"sid": "s1"}}}
        if path == "/api/user/self":
            raise nc.NewApiError("boom", 500)
        if path == "/api/user/sessions/s1":
            return {}
        if path == "/api/user/token":
            token_calls.append(1)
            return {"data": "rotated-pat"}
        raise nc.NewApiError("unauthorized", 401)

    monkeypatch.setattr(nc, "_admin_login", fake_login)
    monkeypatch.setattr(nc, "request", broken_request)
    asyncio.run(nc._self_heal_admin_cred())
    assert nc._admin_cache["pat"] == "rotated-pat"


def test_readback_releases_session_even_on_failure(monkeypatch):
    """读回成败都要归还登录会话（会话是稀缺资源，不能泄漏）。"""
    monkeypatch.setattr(config, "NEWAPI_ADMIN_PAT_READBACK", True)
    monkeypatch.setattr(config, "NEWAPI_ADMIN_UID", 1)
    nc._admin_cache.update(pat="stale-env-pat", uid=1)
    released: list = []

    async def fake_request(method, path, *, headers, json=None, params=None, client=None):
        if path == "/api/user/login":
            return {"data": {"user": {"id": 1}, "access_token": "sess-at",
                             "session": {"sid": "s1"}}}
        if path == "/api/user/self":
            return {"data": {"access_token": "read-back-pat"}}
        if path.startswith("/api/user/sessions/"):
            released.append(path)
            return {}
        raise nc.NewApiError("unauthorized", 401)

    monkeypatch.setattr(nc, "request", fake_request)
    asyncio.run(nc._self_heal_admin_cred())
    assert released == ["/api/user/sessions/s1"]
