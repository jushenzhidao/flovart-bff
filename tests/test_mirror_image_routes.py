"""方案 B：生图请求「镜像路由」（飞哥 2026-09-23 拍板）。

守护四件事：
1. sync 镜像提交 POST /api/v1/images/generations → 强制 mode=sync（路径即语义）；
2. async 镜像提交 POST /api/async/v1/images/generations → 强制 mode=async；
3. 路径与 params.mode 冲突 → 以路径为准 + warning 日志；
4. 旧 POST /api/tasks / GET /api/tasks/{id} 回归：行为完全不变。
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import cloudstore, platform_catalog as catalog
from app.routers import tasks as tasks_router
from app.security import require_session


# ---------------------------------------------------------------------------
# 基架：mock 下架闸门放行 + 捕获 tasks.submit 的 (uid, kind, params)
# ---------------------------------------------------------------------------
@pytest.fixture()
def catalog_open(monkeypatch):
    async def allow(model_name):
        return None

    monkeypatch.setattr(catalog, "assert_model_available", allow)


def _mirror_submit(app, path, params):
    captured: dict = {}

    async def fake_submit(uid, kind, params_):
        captured["uid"] = uid
        captured["kind"] = kind
        captured["params"] = params_
        return {"id": "req-mirror-1", "taskId": None, "kind": kind,
                "mode": params_.get("mode"), "status": "processing", "gatewayTask": {}}

    app.dependency_overrides[require_session] = lambda: {"uid": 7, "username": "tester"}
    try:
        tasks_router.tasks.submit, orig = fake_submit, tasks_router.tasks.submit
        try:
            with TestClient(app) as c:
                r = c.post(path, json={"type": "image-gen", "params": params})
        finally:
            tasks_router.tasks.submit = orig
    finally:
        app.dependency_overrides.pop(require_session, None)
    assert r.status_code == 200
    return r.json()["data"], captured


# ---------------------------------------------------------------------------
# 1) sync 镜像提交
# ---------------------------------------------------------------------------
def test_mirror_sync_submit_forces_sync_mode():
    from app.main import app

    view, captured = _mirror_submit(app, "/api/v1/images/generations",
                                    {"model": "gpt-image-2", "prompt": "x"})
    assert captured["kind"] == "image-gen"
    assert captured["uid"] == 7
    assert captured["params"]["mode"] == "sync"


# ---------------------------------------------------------------------------
# 2) async 镜像提交
# ---------------------------------------------------------------------------
def test_mirror_async_submit_forces_async_mode():
    from app.main import app

    view, captured = _mirror_submit(app, "/api/async/v1/images/generations",
                                    {"model": "gpt-image-2", "prompt": "x"})
    assert captured["kind"] == "image-gen"
    assert captured["params"]["mode"] == "async"


# ---------------------------------------------------------------------------
# 3) 路径与 params.mode 冲突 → 以路径为准 + warning
# ---------------------------------------------------------------------------
def _mirror_submit_with_logger_spy(app, path, params):
    """同 _mirror_submit，额外 spy 路由模块 logger.warning（caplog 在 TestClient 线程下不可靠）。"""
    warnings: list[tuple] = []
    orig_warn = tasks_router.logger.warning
    tasks_router.logger.warning = lambda *a, **k: warnings.append(a)
    try:
        view, captured = _mirror_submit(app, path, params)
    finally:
        tasks_router.logger.warning = orig_warn
    return view, captured, warnings


def test_mirror_path_wins_over_conflicting_params_mode():
    from app.main import app

    view, captured, warnings = _mirror_submit_with_logger_spy(
        app, "/api/async/v1/images/generations",
        {"model": "gpt-image-2", "prompt": "x", "mode": "sync"})
    assert captured["params"]["mode"] == "async"
    assert any("冲突" in str(a) or any("冲突" in str(x) for x in a) for a in warnings), \
        "路径与 params.mode 冲突必须打 warning 日志"


def test_mirror_no_warning_when_modes_agree():
    from app.main import app

    _, _, warnings = _mirror_submit_with_logger_spy(
        app, "/api/v1/images/generations",
        {"model": "gpt-image-2", "prompt": "x", "mode": "sync"})
    assert not warnings


# ---------------------------------------------------------------------------
# 4) 镜像路由仅 image-gen（D4）
# ---------------------------------------------------------------------------
def test_mirror_rejects_non_image_gen():
    from app.main import app

    app.dependency_overrides[require_session] = lambda: {"uid": 7, "username": "tester"}
    try:
        with TestClient(app) as c:
            r = c.post("/api/async/v1/images/generations",
                       json={"type": "upscale", "params": {"image": "data:image/png;base64,AA"}})
    finally:
        app.dependency_overrides.pop(require_session, None)
    assert r.status_code == 400
    body = r.json()
    text = body.get("message") or body.get("detail") or ""
    assert "仅支持" in text


# ---------------------------------------------------------------------------
# 5) 镜像轮询 GET：与 GET /api/tasks/{id} 完全等价（同一 handler 逻辑）
# ---------------------------------------------------------------------------
def test_mirror_poll_equivalent_to_tasks_poll(monkeypatch):
    from app.main import app

    view = {"id": "req-9", "status": "succeeded", "result": {"images": [{"url": "u"}]}}

    async def fake_get(request_id, uid):
        assert (request_id, uid) == ("req-9", 7)
        return view

    monkeypatch.setattr(tasks_router.tasks, "get_task", fake_get)
    app.dependency_overrides[require_session] = lambda: {"uid": 7, "username": "tester"}
    try:
        with TestClient(app) as c:
            r1 = c.get("/api/v1/images/generations/req-9")
            r2 = c.get("/api/async/v1/images/generations/req-9")
            r3 = c.get("/api/tasks/req-9")
    finally:
        app.dependency_overrides.pop(require_session, None)
    assert r1.status_code == r2.status_code == r3.status_code == 200
    assert r1.json() == r2.json() == r3.json() == {"success": True, "message": "", "data": view}


# ---------------------------------------------------------------------------
# 6) 旧 POST /api/tasks 回归：params.mode 原样保留（不注入路径 mode）
# ---------------------------------------------------------------------------
def test_legacy_tasks_submit_untouched():
    from app.main import app

    calls: list[dict] = []

    async def fake_submit(uid, kind, params):
        calls.append({"uid": uid, "kind": kind, "params": params})
        return {"id": "req-1", "kind": kind, "status": "processing"}

    app.dependency_overrides[require_session] = lambda: {"uid": 7, "username": "tester"}
    orig = tasks_router.tasks.submit
    tasks_router.tasks.submit = fake_submit
    try:
        with TestClient(app) as c:
            r1 = c.post("/api/tasks", json={
                "type": "image-gen",
                "params": {"model": "gpt-image-2", "prompt": "x", "mode": "async"}})
            r2 = c.post("/api/tasks", json={
                "type": "image-gen", "params": {"model": "gpt-image-2", "prompt": "x"}})
    finally:
        tasks_router.tasks.submit = orig
        app.dependency_overrides.pop(require_session, None)
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(calls) == 2
    assert calls[0]["params"]["mode"] == "async", "旧路由 params.mode 原样透传"
    assert "mode" not in calls[1]["params"], "旧路由不得给无 mode 的请求注入 mode"


def test_legacy_tasks_suspension_gate_still_applies(monkeypatch):
    """旧路由的下架闸门回归：被下架模型仍 409（重构 _create_task_common 后不丢）。"""
    from app.platform_catalog import ModelSuspendedError

    async def deny(model_name):
        raise ModelSuspendedError(model_name, "SvcA", "removed")

    monkeypatch.setattr(catalog, "assert_model_available", deny)
    body = tasks_router.SubmitBody(type="image-gen",
                                   params={"model": "m-suspended", "prompt": "x"})
    with pytest.raises(ModelSuspendedError):
        asyncio.run(tasks_router.create_task(body, {"uid": 7}))
