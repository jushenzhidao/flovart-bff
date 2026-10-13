"""行为埋点测试。覆盖三层：

1. LocalMeta events_* 五个方法：批量插入 / 按事件聚合 / 按天聚合 / 明细过滤 / 活跃数。
2. 路由 POST /api/events（TestClient）：
   - 匿名可报（登录事件发生在登录前）+ 带 session 自动关联 uid
   - 批量上限 / event 名校验 / props 体积上限 / 滑窗限速
   - BFF_EVENTS_ENABLED=false → 202 丢弃
3. 管理台查询接口：require_admin 拦截 + 正常返回。

注意：FastAPI TestClient 依赖 httpx；本仓既有测试已用（见 conftest/其他路由测试）。
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import cloudstore, config, security
from app.cloudstore import LocalMeta
from app.main import app


@pytest.fixture()
def local_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    meta = LocalMeta()
    monkeypatch.setattr(cloudstore, "META", meta)
    return meta


@pytest.fixture()
def client(local_meta, monkeypatch):
    # 限速重置：每用例独立桶
    import app.routers.events as ev
    monkeypatch.setattr(ev, "_rate_buckets", defaultdict_reset())
    monkeypatch.setattr(config, "EVENTS_ENABLED", True)
    monkeypatch.setattr(config, "EVENTS_RATE_PER_MIN", 1000)
    return TestClient(app, raise_server_exceptions=False)


def defaultdict_reset():
    from collections import defaultdict, deque
    return defaultdict(deque)


def _session_cookie(uid: int, role: int = 1) -> dict:
    """造一个合法会话 Cookie（AES 加密，与 security.set_session 同构）。

    载荷必须含 uid/username/pat（security._decrypt 的结构校验），role 决定管理权限。
    """
    from app.security import set_session
    from fastapi import Response
    resp = Response()
    set_session(resp, {"uid": uid, "username": f"u{uid}", "pat": "sk-test", "role": role})
    return {"Authorization": "Bearer x", "Cookie": resp.headers["set-cookie"].split(";")[0]}


# ---------------- LocalMeta ----------------

def test_events_insert_and_summary(local_meta):
    items = [
        {"uid": 1, "client_id": "c1", "session_id": "s1", "event": "generate_submit",
         "props": {"model": "flovart:seedance-2", "mode": "video"}, "path": "/#/app", "ua": "UA"},
        {"uid": None, "client_id": "c2", "session_id": "s2", "event": "login_success",
         "props": {}, "path": "/", "ua": ""},
        {"uid": 1, "client_id": "c1", "session_id": "s1", "event": "generate_submit",
         "props": {"model": "flovart:gpt-image-2", "mode": "image"}, "path": "/#/app", "ua": "UA"},
    ]
    asyncio.run(cloudstore.events_insert(items))
    summary = asyncio.run(cloudstore.events_summary(30))
    by_event = {r["event"]: r["count"] for r in summary}
    assert by_event["generate_submit"] == 2 and by_event["login_success"] == 1
    recent = asyncio.run(cloudstore.events_recent(10))
    assert len(recent) == 3
    top = recent[0]
    assert top["event"] == "generate_submit" and top["props"]["model"] == "flovart:seedance-2"
    assert top["uid"] == 1 and top["client_id"] == "c1"


def test_events_recent_filters(local_meta):
    asyncio.run(cloudstore.events_insert([
        {"uid": 1, "client_id": "c1", "session_id": "s", "event": "a", "props": {}, "path": "", "ua": ""},
        {"uid": 2, "client_id": "c2", "session_id": "s", "event": "b", "props": {}, "path": "", "ua": ""},
    ]))
    assert len(asyncio.run(cloudstore.events_recent(10, event="a"))) == 1
    assert all(r["uid"] == 2 for r in asyncio.run(cloudstore.events_recent(10, uid=2)))


def test_events_daily_and_active(local_meta):
    asyncio.run(cloudstore.events_insert([
        {"uid": 1, "client_id": "c1", "session_id": "s", "event": "e", "props": {}, "path": "", "ua": ""},
        {"uid": None, "client_id": "c2", "session_id": "s", "event": "e", "props": {}, "path": "", "ua": ""},
        {"uid": None, "client_id": "c2", "session_id": "s", "event": "e", "props": {}, "path": "", "ua": ""},
    ]))
    daily = asyncio.run(cloudstore.events_daily(7))
    assert len(daily) == 1 and daily[0]["count"] == 3
    active = asyncio.run(cloudstore.events_active_clients(7))
    assert active == {"clients": 2, "users": 1}


def test_events_bad_props_json_survives(local_meta):
    """坏 props_json 不炸查询（_json_load 容错）。"""
    asyncio.run(cloudstore.events_insert([
        {"uid": 1, "client_id": "c", "session_id": "s", "event": "e", "props": {"k": "v"}, "path": "", "ua": ""},
    ]))
    # 手工写一条坏 JSON 模拟历史脏数据
    def _():
        meta = local_meta
        conn = meta._connect()
        conn.execute("INSERT INTO cloud_events(uid, client_id, session_id, event, props_json, path, ua, created_at)"
                     " VALUES(1,'c','s','e2','{bad json','', '', '2026-10-13T00:00:00+00:00')")
        conn.commit()
    asyncio.run(asyncio.to_thread(_))
    rows = asyncio.run(cloudstore.events_recent(10))
    bad = [r for r in rows if r["event"] == "e2"][0]
    assert bad["props"] == {}


# ---------------- POST /api/events ----------------

def test_post_events_anonymous_and_with_session(client):
    r = client.post("/api/events", json={"events": [
        {"event": "login_success", "client_id": "c1", "session_id": "s1", "props": {"method": "password"}},
        {"event": "page_view", "client_id": "c1", "session_id": "s1"},
    ]})
    assert r.status_code == 200 and r.json()["data"]["accepted"] == 2
    rows = asyncio.run(cloudstore.events_recent(10))
    assert all(r["uid"] is None for r in rows)

    cookie = _session_cookie(42)
    r2 = client.post("/api/events", headers=cookie, json={"events": [
        {"event": "generate_submit", "client_id": "c1", "props": {"model": "m"}},
    ]})
    assert r2.json()["data"]["accepted"] == 1
    rows = asyncio.run(cloudstore.events_recent(10, event="generate_submit"))
    assert rows[0]["uid"] == 42


def test_post_events_rejects_bad_names_and_oversize_batch(client):
    r = client.post("/api/events", json={"events": [
        {"event": "Bad-Name!", "client_id": "c"},          # 字符集不合法
        {"event": "x" * 65, "client_id": "c"},             # 超长
        {"event": "ok_event", "client_id": "c"},           # 合法
    ]})
    data = r.json()["data"]
    assert data["accepted"] == 1 and data["rejected"] == 2

    r2 = client.post("/api/events", json={"events": [
        {"event": "ok", "client_id": "c"}] * (config.EVENTS_BATCH_MAX + 1)})
    assert r2.status_code == 200
    assert r2.json()["success"] is False  # 批量超限整体拒绝


def test_post_events_oversize_props_rejected(client):
    big = {"blob": "x" * (config.EVENTS_MAX_PROPS_BYTES + 100)}
    r = client.post("/api/events", json={"events": [
        {"event": "ok_event", "client_id": "c", "props": big},
    ]})
    assert r.json()["data"]["rejected"] == 1


def test_post_events_rate_limit(client, monkeypatch):
    monkeypatch.setattr(config, "EVENTS_RATE_PER_MIN", 3)
    for i in range(3):
        r = client.post("/api/events", json={"events": [{"event": "e", "client_id": "rl"}]})
        assert r.json()["data"]["accepted"] == 1
    r = client.post("/api/events", json={"events": [{"event": "e", "client_id": "rl"}]})
    assert r.json()["data"]["rejected"] == 1  # 第 4 条被限速丢弃


def test_post_events_disabled(client, monkeypatch):
    monkeypatch.setattr(config, "EVENTS_ENABLED", False)
    r = client.post("/api/events", json={"events": [{"event": "e", "client_id": "c"}]})
    assert r.json()["data"]["accepted"] == 0
    assert asyncio.run(cloudstore.events_recent(10)) == []


# ---------------- 管理台查询 ----------------

def test_console_events_requires_admin(client):
    r = client.get("/api/console/events")
    assert r.status_code == 401  # 未登录

    cookie = _session_cookie(7)  # 普通用户（role 无）
    r2 = client.get("/api/console/events", headers=cookie)
    assert r2.status_code == 403


def test_console_events_admin_ok(client):
    asyncio.run(cloudstore.events_insert([
        {"uid": 1, "client_id": "c1", "session_id": "s", "event": "generate_submit",
         "props": {"model": "m"}, "path": "/x", "ua": ""},
    ]))
    cookie = _session_cookie(1, role=10)  # require_admin：role>=10
    r = client.get("/api/console/events", headers=cookie)
    assert r.status_code == 200 and r.json()["data"]["items"][0]["event"] == "generate_submit"
    r2 = client.get("/api/console/events/summary?days=7", headers=cookie)
    body = r2.json()["data"]
    assert body["summary"][0]["event"] == "generate_submit"
    assert "daily" in body and "active" in body
