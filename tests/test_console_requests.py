"""管理员请求日志接口（/api/console/requests*）本地行为测试：不触网。

覆盖：
- 列表筛选参数透传（uid/kind/status/model）
- username → uid 解析（走 new-api 管理搜索）
- username 映射失败时展示降级（不 500）
- uid → username 回填
- 详情 b64 剥离 + 404
- 两条路由必须挂 require_admin
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import cloudstore
from app.routers import console
from app.main import app


def _run(coro):
    return asyncio.run(coro)


# ---------- 鉴权检查（行为级：未登录必须 401）----------

def _client():
    return TestClient(app)


@pytest.mark.parametrize("path", [
    "/api/console/requests",
    "/api/console/requests/whatever-id",
])
def test_console_request_routes_require_admin(path):
    with _client() as c:
        r = c.get(path)
    assert r.status_code == 401, f"{path} 未登录必须 401（require_admin 缺失？）"


# ---------- 列表 ----------

def test_console_requests_filters_passthrough(monkeypatch):
    captured = {}

    async def fake_list(uid, kind, status, model, limit, offset):
        captured.update(uid=uid, kind=kind, status=status,
                        model=model, limit=limit, offset=offset)
        return ([{"request_id": "r1", "uid": 7, "kind": "image", "provider": "gw",
                  "model": "gemini-3-pro-image-preview", "status": "failed",
                  "task_id": None, "gateway_request_id": None, "mode": "sync",
                  "created_at": "2026-09-18T10:00:00+00:00",
                  "updated_at": "2026-09-18T10:00:01+00:00"}], 1)

    async def fake_users(keyword="", page=1, page_size=10):
        return {"items": [{"id": 7, "username": "fly"}], "total": 1}

    monkeypatch.setattr(cloudstore, "request_log_admin_list", fake_list)
    monkeypatch.setattr(console.na, "admin_list_users", fake_users)

    body = _run(console.console_requests(
        username="", uid=7, kind="image", status="failed",
        model="gemini", limit=50, offset=0, _s={}))
    assert captured == {"uid": 7, "kind": "image", "status": "failed",
                        "model": "gemini", "limit": 50, "offset": 0}
    assert body["success"] is True
    data = body["data"]
    assert data["total"] == 1
    assert data["items"][0]["username"] == "fly"


def test_console_requests_resolves_username(monkeypatch):
    captured = {}

    async def fake_resolve(username):
        captured["username"] = username
        return 42

    async def fake_list(uid, kind, status, model, limit, offset):
        captured["uid"] = uid
        return ([], 0)

    async def fake_users(keyword="", page=1, page_size=10):
        return {"items": [], "total": 0}

    monkeypatch.setattr(console.na, "admin_resolve_uid_by_username", fake_resolve)
    monkeypatch.setattr(cloudstore, "request_log_admin_list", fake_list)
    monkeypatch.setattr(console.na, "admin_list_users", fake_users)

    _run(console.console_requests(
        username="Fly", uid=0, kind="", status="", model="",
        limit=50, offset=0, _s={}))
    assert captured == {"username": "Fly", "uid": 42}


def test_console_requests_username_map_failure_degrades(monkeypatch):
    """uid→username 解析失败（上游限流等）只降级展示，不能 500。"""

    async def fake_list(uid, kind, status, model, limit, offset):
        return ([{"request_id": "r2", "uid": 9, "kind": "video", "provider": "gw",
                  "model": "m", "status": "succeeded", "task_id": None,
                  "gateway_request_id": None, "mode": "async",
                  "created_at": "t", "updated_at": "t"}], 1)

    async def boom(*a, **kw):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(cloudstore, "request_log_admin_list", fake_list)
    monkeypatch.setattr(console.na, "admin_list_users", boom)

    body = _run(console.console_requests(
        username="", uid=0, kind="", status="", model="",
        limit=50, offset=0, _s={}))
    assert body["data"]["items"][0]["username"] == ""


# ---------- 详情 ----------

def test_console_request_detail_strips_b64(monkeypatch):
    row = {"request_id": "r1", "uid": 7, "kind": "image", "provider": "gw",
           "model": "m", "payload": {"prompt": "p"},
           "task_id": None, "gateway_request_id": None,
           "status": "succeeded", "mode": "sync",
           "result": {"data": [{"b64_json": "A" * 1000}, {"url": "https://x/y.png"}]},
           "created_at": "t", "updated_at": "t"}

    async def fake_get(request_id):
        return dict(row)

    monkeypatch.setattr(cloudstore, "request_log_get", fake_get)
    data = _run(console.console_request_detail("r1", _s={}))["data"]
    img = data["result"]["data"][0]
    assert img["b64_json"] == f"<base64:{1000} chars>"
    assert data["result"]["data"][1]["url"] == "https://x/y.png"
    assert data["payload"] == {"prompt": "p"}


def test_console_request_detail_404(monkeypatch):
    async def fake_get(request_id):
        return None

    monkeypatch.setattr(cloudstore, "request_log_get", fake_get)
    resp = _run(console.console_request_detail("nope", _s={}))
    assert resp.status_code == 404
