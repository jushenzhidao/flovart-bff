"""日志盲区修复测试（飞哥 2026-09-18「两个日志盲区一起修」）：

盲区① /api/tasks 409 下架拦截在落库前 raise → 失败无痕。
      现在拦截时落 status=failed + mode=blocked，管理台可查。
盲区② chat 对话完全不落库。
      现在提交落 submitted、流结束回写 succeeded/failed、断开 cancelled。

全部本地行为测试：不触网。
"""
import asyncio

import pytest

from app import cloudstore
from app.platform_catalog import ModelSuspendedError
from app.newapi_client import NewApiError
from app.routers import chat as chat_router
from app.routers import tasks as tasks_router


class _LogSpy:
    """记录 request_log_put / request_log_update 调用。"""

    def __init__(self, monkeypatch):
        self.puts, self.updates = [], []

        async def fake_put(uid, request_id, kind, provider, model,
                           payload, status, mode):
            self.puts.append({"uid": uid, "request_id": request_id, "kind": kind,
                              "provider": provider, "model": model,
                              "payload": payload, "status": status, "mode": mode})

        async def fake_update(request_id, status=None, task_id=None,
                              gateway_request_id=None, result=None):
            self.updates.append({"request_id": request_id, "status": status,
                                 "task_id": task_id, "result": result})

        monkeypatch.setattr(cloudstore, "request_log_put", fake_put)
        monkeypatch.setattr(cloudstore, "request_log_update", fake_update)


def _run(coro):
    return asyncio.run(coro)


# ---------- 盲区①：409 拦截落库 ----------

def test_blocked_task_logged_as_failed(monkeypatch):
    spy = _LogSpy(monkeypatch)

    async def fake_gate(model):
        raise ModelSuspendedError("m1")

    async def fake_submit(uid, kind, params):  # pragma: no cover —— 不应被走到
        raise AssertionError("submit 不应被执行")

    monkeypatch.setattr(tasks_router.platform_catalog, "assert_model_available", fake_gate)
    monkeypatch.setattr(tasks_router.tasks, "submit", fake_submit)

    with pytest.raises(ModelSuspendedError):
        _run(tasks_router.create_task(
            tasks_router.SubmitBody(type="image-gen",
                                    params={"model": "m1", "prompt": "x"}),
            {"uid": 7}))

    assert len(spy.puts) == 1
    row = spy.puts[0]
    assert row["uid"] == 7 and row["kind"] == "image-gen"
    assert row["model"] == "m1"
    assert row["status"] == "failed" and row["mode"] == "blocked"
    assert len(spy.updates) == 1
    assert spy.updates[0]["result"]["code"] == "MODEL_SUSPENDED"


def test_byok_gateway_not_blocked_nor_logged(monkeypatch):
    """带 `_gateway` 的 BYOK 请求不经过平台闸门，也不该留下平台日志行。"""
    spy = _LogSpy(monkeypatch)
    submits = []

    async def fake_submit(uid, kind, params):
        submits.append(uid)
        return {"id": "r", "status": "processing"}

    monkeypatch.setattr(tasks_router.tasks, "submit", fake_submit)

    res = _run(tasks_router.create_task(
        tasks_router.SubmitBody(type="image-gen",
                                params={"model": "m1", "prompt": "x",
                                        "_gateway": {"base_url": "https://x/v1"}}),
        {"uid": 7}))
    assert res["success"] is True
    assert submits == [7]
    assert spy.puts == [] and spy.updates == []


# ---------- 盲区②：chat 落库 ----------

class _FakeReq:
    def __init__(self, payload):
        self._p = payload

    async def json(self):
        return self._p


def test_chat_suspended_logged(monkeypatch):
    spy = _LogSpy(monkeypatch)

    async def fake_gate(model):
        raise ModelSuspendedError("m1")

    monkeypatch.setattr(chat_router.platform_catalog, "assert_model_available", fake_gate)

    with pytest.raises(ModelSuspendedError):
        _run(chat_router.chat_completions(
            _FakeReq({"model": "m1", "messages": [{"role": "user", "content": "hi"}]}),
            {"uid": 7, "pat": "p"}))

    assert len(spy.puts) == 1
    row = spy.puts[0]
    assert row["kind"] == "chat" and row["model"] == "m1"
    assert row["status"] == "failed" and row["mode"] == "blocked"
    assert spy.updates[0]["result"]["code"] == "MODEL_SUSPENDED"


def test_chat_key_failure_marks_failed(monkeypatch):
    """发放 sk- 失败（502）也不能留下永远 submitted 的僵尸行。"""
    spy = _LogSpy(monkeypatch)

    async def fake_gate(model):
        return None

    async def fake_resolve(uid, pat):
        raise NewApiError("mint failed", 502)

    monkeypatch.setattr(chat_router.platform_catalog, "assert_model_available", fake_gate)
    monkeypatch.setattr(chat_router, "resolve_user_key", fake_resolve)

    resp = _run(chat_router.chat_completions(
        _FakeReq({"model": "m1", "messages": [{"role": "user", "content": "hi"}]}),
        {"uid": 7, "pat": "p"}))
    assert resp.status_code == 502
    assert [r["status"] for r in spy.puts] == ["submitted"]
    assert spy.updates[-1]["status"] == "failed"


def test_chat_log_failure_never_breaks_gate(monkeypatch):
    """日志层炸了（如 DB 不可达）必须吞掉，409 照常抛给前端。"""

    async def fake_put(*a, **kw):
        raise RuntimeError("db down")

    async def fake_gate(model):
        raise ModelSuspendedError("m1")

    monkeypatch.setattr(cloudstore, "request_log_put", fake_put)
    monkeypatch.setattr(chat_router.platform_catalog, "assert_model_available", fake_gate)

    with pytest.raises(ModelSuspendedError):
        _run(chat_router.chat_completions(
            _FakeReq({"model": "m1", "messages": [{"role": "user", "content": "hi"}]}),
            {"uid": 7, "pat": "p"}))


def test_sse_extract_parses_deltas():
    acc = {"text_parts": [], "error": None, "done": False, "finish": None, "usage": None}
    buf = bytearray()
    chat_router._sse_extract(b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n', buf, acc)
    chat_router._sse_extract(b'data: {"choices":[{"delta":{"content":"llo"},'
                             b'"finish_reason":"stop"}]}\n\n', buf, acc)
    chat_router._sse_extract(b"data: [DONE]\n\n", buf, acc)
    assert "".join(acc["text_parts"]) == "hello"
    assert acc["finish"] == "stop" and acc["done"] is True and acc["error"] is None


def test_logged_stream_success(monkeypatch):
    spy = _LogSpy(monkeypatch)

    async def stream():
        yield b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"llo"},' \
              b'"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    async def collect():
        return [c async for c in chat_router._logged_stream(7, "r1", "m", stream())]

    chunks = _run(collect())
    assert len(chunks) == 3  # 透传字节一字不差
    assert spy.updates[-1]["status"] == "succeeded"
    assert spy.updates[-1]["result"]["reply"] == "hello"
    assert spy.updates[-1]["result"]["finish_reason"] == "stop"


def test_logged_stream_error_event(monkeypatch):
    spy = _LogSpy(monkeypatch)

    async def stream():
        yield ('data: {"error":{"message":"上游返回 502：boom",'
               '"type":"bff_proxy_error"}}\n\n').encode("utf-8")

    async def collect():
        return [c async for c in chat_router._logged_stream(7, "r1", "m", stream())]

    _run(collect())
    assert spy.updates[-1]["status"] == "failed"
    assert spy.updates[-1]["result"]["error"] == "上游返回 502：boom"
    assert spy.updates[-1]["result"]["bff_proxy"] is True


# ---------- strip_b64（共享后补充 data-uri 分支）----------

def test_strip_b64_data_uri():
    obj = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 500}},
        {"type": "text", "text": "看图"}]}],
        "x": {"b64_json": "B" * 300}}
    out = cloudstore.strip_b64(obj)
    url = out["messages"][0]["content"][0]["image_url"]["url"]
    assert url.startswith("<data-uri:") and url.endswith(" chars>")
    assert out["x"]["b64_json"] == "<base64:300 chars>"
    assert out["messages"][0]["content"][1]["text"] == "看图"
