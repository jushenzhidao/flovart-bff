"""Chatfire 风格「真异步」生图分支（2026-09-20 接入，Apifox 文档 oneapis/515490684e0）。

协议与 new-api 统一 tasks 端点不同：
- POST async/v1/images/generations  body={model,prompt,image[]} → 202 {task_id,status=QUEUED}
- GET  async/v1/images/generations/{task_id}
    处理中 → 202 {status:QUEUED|IN_PROGRESS,...}（无 data）
    完成   → 200 {data:[{url|b64_json}],created,usage}（**无 status 字段**，以 data 出现为终态）

本文件守护：模型前缀判定、提交载荷形状、轮询终态判定（data→succeeded /
IN_PROGRESS→processing / error→failed / 空 data→failed 无产物）、
submit() 按「kind + 模型前缀 + mode=async」的路由分流。
"""
import asyncio

import pytest

from app import cloudstore, tasks


# ---------------------------------------------------------------------------
# 1) 模型前缀判定
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("model,expected", [
    ("doubao-seedream-5-0-pro-260628", True),
    ("doubao-seedream-4-0", True),
    ("Doubao-Seedream-5-0", True),  # 大小写不敏感
    ("doubao-seedance-1-0-pro-250528", False),  # seedance 是视频，不匹配 seedream 前缀
    ("gpt-image-2", False),
    ("gemini-3-pro-image-preview", False),
    ("", False),
])
def test_is_chatfire_async_image_model(model, expected):
    assert tasks._is_chatfire_async_image_model(model) is expected


# ---------------------------------------------------------------------------
# 2) 测试基架：mock 日志 + 网关 + 落盘
# ---------------------------------------------------------------------------
def _install_fakes(monkeypatch, captured, gw_raw):
    logs: dict[str, dict] = {}

    async def fake_put(uid, request_id, kind, provider, model, params,
                       status="submitted", mode="sync"):
        logs[request_id] = {"status": status, "provider": provider, "mode": mode,
                            "kind": kind, "task_id": None}
        return logs[request_id]

    async def fake_update(request_id, **fields):
        logs.setdefault(request_id, {}).update(fields)
        return logs[request_id]

    async def fake_gw(method, path, uid, *, json=None, params=None, client=None):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = json
        if isinstance(gw_raw, Exception):
            raise gw_raw
        return gw_raw

    async def fake_persist(task, task_id, uid, kind="image", request_id=None):
        captured["persisted"] = (task_id, kind)
        return task

    monkeypatch.setattr(cloudstore, "request_log_put", fake_put)
    monkeypatch.setattr(cloudstore, "request_log_update", fake_update)
    monkeypatch.setattr(tasks, "_gw_call", fake_gw)
    monkeypatch.setattr(tasks, "_persist_outputs", fake_persist)
    monkeypatch.setattr(tasks, "_proxy_client", lambda: None)
    return logs


# ---------------------------------------------------------------------------
# 3) 提交：载荷形状 + 返回视图
# ---------------------------------------------------------------------------
def test_submit_chatfire_async_body_shape(monkeypatch):
    captured: dict = {}
    gw_raw = {"task_id": "doubao_seedream_abc", "status": "QUEUED",
              "created_at": 1789, "scheduled_at": 0, "replayed": False}
    logs = _install_fakes(monkeypatch, captured, gw_raw)

    view = asyncio.run(tasks._submit_chatfire_async_image(
        7, "req-cf-1",
        {"model": "doubao-seedream-5-0-pro-260628", "prompt": "飞上天",
         "image": [], "size": "1024x1024"}))

    # 只透传文档三字段；size 不外发（避免上游拒收）
    assert captured["method"] == "POST"
    assert captured["path"] == "async/v1/images/generations"
    assert captured["json"] == {"model": "doubao-seedream-5-0-pro-260628",
                                "prompt": "飞上天", "image": []}
    assert logs["req-cf-1"]["provider"] == "gateway-async-image"
    assert logs["req-cf-1"]["status"] == "processing"
    assert logs["req-cf-1"]["task_id"] == "doubao_seedream_abc"
    assert view["id"] == "req-cf-1"
    assert view["taskId"] == "doubao_seedream_abc"
    assert view["mode"] == "async"
    assert view["status"] == "QUEUED"


def test_submit_chatfire_async_image_list_required(monkeypatch):
    """image 非列表时兜底为 []（文档 image[] 为必填，避免上游 400）。"""
    captured: dict = {}
    gw_raw = {"task_id": "t1", "status": "QUEUED"}
    _install_fakes(monkeypatch, captured, gw_raw)

    asyncio.run(tasks._submit_chatfire_async_image(
        7, "req-cf-2", {"model": "doubao-seedream-5-0", "prompt": "x", "image": None}))
    assert captured["json"]["image"] == []


# ---------------------------------------------------------------------------
# 4) submit() 路由分流：chatfire 模型 + mode=async → 新分支
# ---------------------------------------------------------------------------
def test_submit_routes_doubao_to_chatfire_branch(monkeypatch):
    captured: dict = {}
    gw_raw = {"task_id": "doubao_seedream_route1", "status": "QUEUED"}
    logs = _install_fakes(monkeypatch, captured, gw_raw)

    view = asyncio.run(tasks.submit(
        7, "image-gen",
        {"model": "doubao-seedream-5-0-pro-260628", "prompt": "路由测试",
         "image": [], "mode": "async"}))

    assert captured["path"] == "async/v1/images/generations"
    assert view["taskId"] == "doubao_seedream_route1"


def test_submit_routes_gpt_async_keeps_generic_tasks_path(monkeypatch):
    """非 chatfire 模型的 async 行为不变：仍走统一 tasks 端点 {type,params} 包裹体。"""
    captured: dict = {}
    gw_raw = {"task_id": "gw-task-1", "status": "processing"}
    logs = _install_fakes(monkeypatch, captured, gw_raw)

    view = asyncio.run(tasks.submit(
        7, "image-gen",
        {"model": "gpt-image-2", "prompt": "路由测试", "image": [], "mode": "async"}))

    assert captured["path"] == tasks.TASK_TYPES["image-gen"]["async_path"]
    assert captured["json"] == {"type": "image-gen",
                                "params": {"model": "gpt-image-2", "prompt": "路由测试",
                                           "image": [], "mode": "async"}}
    assert view["taskId"] == "gw-task-1"


# ---------------------------------------------------------------------------
# 5) 轮询终态判定
# ---------------------------------------------------------------------------
def _poll_log(task_id="doubao_seedream_abc"):
    return {"kind": "image-gen", "mode": "async", "status": "processing",
            "task_id": task_id, "provider": "gateway-async-image"}


def test_poll_in_progress(monkeypatch):
    captured: dict = {}
    _install_fakes(monkeypatch, captured, {
        "task_id": "t", "status": "IN_PROGRESS", "created_at": 1,
        "scheduled_at": 0, "batch_key": "", "batch_state": "", "replayed": False})
    logs: dict = {}

    async def fake_update(request_id, **fields):
        logs.update(fields)
        return logs

    monkeypatch.setattr(cloudstore, "request_log_update", fake_update)
    view = asyncio.run(tasks._poll_chatfire_async_image("req-p1", _poll_log(), 7))
    assert view["status"] == "in_progress"
    assert logs["status"] == "processing"


def test_poll_final_b64_succeeded(monkeypatch):
    captured: dict = {}
    _install_fakes(monkeypatch, captured, {
        "data": [{"b64_json": "QQ==", "revised_prompt": "p"}],
        "created": 2, "usage": {"total_tokens": 100}})
    logs: dict = {}

    async def fake_update(request_id, **fields):
        logs.update(fields)
        return logs

    monkeypatch.setattr(cloudstore, "request_log_update", fake_update)
    view = asyncio.run(tasks._poll_chatfire_async_image("req-p2", _poll_log(), 7))
    assert view["status"] == "succeeded"
    assert logs["status"] == "succeeded"
    # data → images 归一化（前端只认 image/images/layers）
    assert view["result"]["images"][0]["b64_json"] == "QQ=="
    assert captured["persisted"] == ("doubao_seedream_abc", "image-gen")


def test_poll_final_url_succeeded(monkeypatch):
    captured: dict = {}
    _install_fakes(monkeypatch, captured, {
        "data": [{"url": "https://tos.example.com/x.jpeg?sig=1"}],
        "created": 3, "usage": {"total_tokens": 9}})
    logs: dict = {}

    async def fake_update(request_id, **fields):
        logs.update(fields)
        return logs

    monkeypatch.setattr(cloudstore, "request_log_update", fake_update)
    view = asyncio.run(tasks._poll_chatfire_async_image("req-p3", _poll_log(), 7))
    assert view["status"] == "succeeded"
    assert view["result"]["images"][0]["url"].startswith("https://tos.example.com/")


def test_poll_error_body_failed(monkeypatch):
    captured: dict = {}
    _install_fakes(monkeypatch, captured, {
        "error": {"message": "No available channel", "type": "new_api_error"}})
    logs: dict = {}

    async def fake_update(request_id, **fields):
        logs.update(fields)
        return logs

    monkeypatch.setattr(cloudstore, "request_log_update", fake_update)
    view = asyncio.run(tasks._poll_chatfire_async_image("req-p4", _poll_log(), 7))
    assert view["status"] == "failed"
    assert logs["status"] == "failed"
    assert logs["result"]["error"]["message"] == "No available channel"


def test_poll_failed_status_failed(monkeypatch):
    captured: dict = {}
    _install_fakes(monkeypatch, captured, {"task_id": "t", "status": "FAILED"})
    logs: dict = {}

    async def fake_update(request_id, **fields):
        logs.update(fields)
        return logs

    monkeypatch.setattr(cloudstore, "request_log_update", fake_update)
    view = asyncio.run(tasks._poll_chatfire_async_image("req-p5", _poll_log(), 7))
    assert view["status"] == "failed"


def test_poll_empty_data_failed_not_succeeded(monkeypatch):
    """200 但 data 为空：绝不记 succeeded（同 sync 无产物防线）。"""
    captured: dict = {}
    _install_fakes(monkeypatch, captured, {"data": [], "created": 4})
    logs: dict = {}

    async def fake_update(request_id, **fields):
        logs.update(fields)
        return logs

    monkeypatch.setattr(cloudstore, "request_log_update", fake_update)
    view = asyncio.run(tasks._poll_chatfire_async_image("req-p6", _poll_log(), 7))
    assert view["status"] == "failed"
    assert logs["status"] == "failed"


def test_poll_queued_processing(monkeypatch):
    captured: dict = {}
    _install_fakes(monkeypatch, captured, {"task_id": "t", "status": "QUEUED"})
    logs: dict = {}

    async def fake_update(request_id, **fields):
        logs.update(fields)
        return logs

    monkeypatch.setattr(cloudstore, "request_log_update", fake_update)
    view = asyncio.run(tasks._poll_chatfire_async_image("req-p7", _poll_log(), 7))
    assert view["status"] == "queued"
