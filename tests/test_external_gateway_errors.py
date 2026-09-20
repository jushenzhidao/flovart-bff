"""外部网关失败留痕 + 401 文案语义（2026-09-11）。

背景（飞哥反馈）：普通用户走平台共享服务生图失败，DB 里 `cloud_request_log.status=failed`
但 `result` 为空 —— 查不出任何原因，只能靠翻 uvicorn 日志。根因有两处：

1. `_run_external` / `_run_sync` 只 `except NewApiError`，且失败时不写 `result`。
   非 NewApiError 的异常（超时 / 解析 / 落盘）会逃逸，外层只置 failed，原因彻底丢失。
2. `_parse_response` 把**所有** 401 都翻译成「凭证已失效，请重新登录」。
   但打**外部网关**（完整 URL）时 401 的含义是「该服务的 API Key 无效」，
   跟 BFF 登录态无关 —— 用户被误导去排查登录问题。

本文件守护这两点：失败必须有原因、401 文案必须区分场景。
"""
import asyncio
import json

import pytest

from app import cloudstore, tasks
from app.newapi_client import NewApiError, _parse_response


# ---------------------------------------------------------------------------
# 1) 401 文案分场景（_parse_response）
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._body

    @property
    def text(self):
        return json.dumps(self._body)


def test_401_on_external_gateway_says_api_key_invalid():
    """打外部网关（完整 URL）返回 401 → 提示「AI 服务的 API Key 无效」，不是「重新登录」。"""
    resp = _FakeResp(401)
    with pytest.raises(NewApiError) as ei:
        _parse_response(resp, "POST", "https://api.chatfire.cn/v1/images/generations")
    msg = str(ei.value.message)
    assert "API Key" in msg
    assert "重新登录" not in msg


def test_401_on_newapi_path_still_says_relogin():
    """打 new-api（相对 path）返回 401 → 仍提示「凭证已失效，请重新登录」。"""
    resp = _FakeResp(401)
    with pytest.raises(NewApiError) as ei:
        _parse_response(resp, "GET", "/api/user/self")
    assert "重新登录" in str(ei.value.message)


# ---------------------------------------------------------------------------
# 2) 外部网关失败必须留痕（_run_external）
# ---------------------------------------------------------------------------
def _install_fake_log(monkeypatch):
    """把 cloudstore 的请求日志读写换成内存版，断言不触网、不落盘。"""
    logs: dict[str, dict] = {}

    async def fake_put(uid, request_id, kind, provider, model, params, status="submitted", mode="sync"):
        logs[request_id] = {
            "uid": uid, "kind": kind, "provider": provider, "model": model,
            "payload": params, "status": status, "mode": mode, "result": None,
        }
        return logs[request_id]

    async def fake_update(request_id, **fields):
        logs.setdefault(request_id, {}).update(fields)
        return logs[request_id]

    monkeypatch.setattr(cloudstore, "request_log_put", fake_put)
    monkeypatch.setattr(cloudstore, "request_log_update", fake_update)
    return logs


def test_external_gateway_failure_records_reason(monkeypatch):
    """外部网关失败时 result 必须写入错误详情（message/status/url/stage）。

    注意（2026-09-15 修正）：`_run_external` 已重构为「非阻塞提交」——
    它只落 submitted 日志 + 起后台任务后**立即返回**，不再向上抛异常；
    真正调用与失败留痕在 `_external_call_and_record` 里完成。
    故此处直接驱动后台协程，断言其留痕行为（原断言 `pytest.raises` 已失效）。
    """
    logs = _install_fake_log(monkeypatch)

    async def boom(method, url, *, api_key, json=None, client=None):
        raise NewApiError("AI 服务的 API Key 无效或无权限，请在「AI 服务设置」中检查该服务的 Key", 401)

    monkeypatch.setattr(tasks.na, "request_external", boom)

    asyncio.run(tasks._external_call_and_record(
        7, "req-ext-1", "image-gen",
        "https://api.chatfire.cn/v1/images/generations", "sk-bad", {"model": "gpt-image-2"}))

    entry = logs["req-ext-1"]
    assert entry["status"] == "failed"
    assert entry["result"], "失败时 result 不能为空 —— 否则线上无法定位原因"
    err = entry["result"]["error"]
    assert "API Key" in err["message"]
    assert err["status"] == 401
    assert err["stage"] == "request"
    assert err["url"].endswith("images/generations")
    assert err["uid"] == 7


def test_external_gateway_non_newapierror_also_recorded(monkeypatch):
    """非 NewApiError（如超时/连接错误）同样必须留痕 —— 这是此前彻底丢失原因的场景。

    同 test_external_gateway_failure_records_reason：改驱动后台协程
    （`_run_external` 不再抛异常，吞掉一切并留痕的职责已移入 `_external_call_and_record`）。
    """
    logs = _install_fake_log(monkeypatch)

    async def timeout_boom(method, url, *, api_key, json=None, client=None):
        raise TimeoutError("connection timed out")

    monkeypatch.setattr(tasks.na, "request_external", timeout_boom)

    asyncio.run(tasks._external_call_and_record(
        8, "req-ext-2", "image-gen",
        "https://api.chatfire.cn/v1/images/generations", "sk-x", {"model": "gpt-image-2"}))

    entry = logs["req-ext-2"]
    assert entry["status"] == "failed"
    assert entry["result"]["error"]["message"] == "connection timed out"
    assert entry["result"]["error"]["stage"] == "request"


def test_external_request_masks_api_key_before_persist(monkeypatch):
    """落库的 payload 里 _gateway.api_key 必须掩码，真实 key 绝不能进日志库。"""
    logs = _install_fake_log(monkeypatch)

    async def ok(method, url, *, api_key, json=None, client=None):
        return {"data": [{"b64_json": "AAAA"}]}

    monkeypatch.setattr(tasks.na, "request_external", ok)

    async def fake_persist(task, task_id, uid, kind="image", request_id=None):
        return task

    monkeypatch.setattr(tasks, "_persist_outputs", fake_persist)

    params = {
        "model": "gpt-image-2", "prompt": "一只猫",
        "_gateway": {"base_url": "https://api.chatfire.cn/v1", "api_key": "sk-super-secret"},
    }
    asyncio.run(tasks._run_external(9, "req-ext-3", "image-gen", {"sync_path": "images/generations"}, params))

    stored = logs["req-ext-3"]["payload"]
    assert stored["_gateway"]["api_key"] == "***"
    assert "sk-super-secret" not in json.dumps(stored)
    assert logs["req-ext-3"]["status"] == "succeeded"


# ---------------------------------------------------------------------------
# 3) 上游 HTTP 200 但无产物 → 必须记 failed（2026-09-16）
# ---------------------------------------------------------------------------
EMPTY_200_BODY = {
    "created": 1789541248,
    "usage": {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5},
}


def test_upstream_empty_result_is_failed_not_succeeded(monkeypatch):
    """上游 200 但正文只有 usage、没有图片 → 记 failed 并留痕原因。

    实测（2026-09-16，chatfire /v1/images/generations，gemini 系列）该形态偶发率约 1/4，
    且**直连同一把 key 可复现** —— 是模型侧空产出，不是 BFF 解析问题。
    此前它被 `_normalize_sync_result` 归一化成 `{"created","usage"}` 并按 `succeeded`
    落库，前端拿不到产物、只能显示兜底文案「任务未返回媒体」，
    上游空产出这个真相彻底丢失。本测试守护「不许再记 succeeded」。
    """
    logs = _install_fake_log(monkeypatch)

    async def empty_ok(method, url, *, api_key, json=None, client=None):
        return dict(EMPTY_200_BODY)

    monkeypatch.setattr(tasks.na, "request_external", empty_ok)

    async def fake_persist(task, task_id, uid, kind="image", request_id=None):
        return task

    monkeypatch.setattr(tasks, "_persist_outputs", fake_persist)

    asyncio.run(tasks._external_call_and_record(
        11, "req-empty-1", "image-gen",
        "https://api.chatfire.cn/v1/images/generations", "sk-x",
        {"model": "gemini-3.1-flash-image-preview"}))

    entry = logs["req-empty-1"]
    assert entry["status"] == "failed", "空产出绝不可能算成功"
    err = entry["result"]["error"]
    assert err["type"] == "upstream_empty_result"
    assert err["usage"]["completion_tokens"] == 0
    assert err["upstream_keys"] == ["created", "usage"]


def test_upstream_with_media_still_succeeds(monkeypatch):
    """对照组：带 url 的正常 200 必须仍记 succeeded —— 守卫不能误伤真产物。"""
    logs = _install_fake_log(monkeypatch)

    async def ok(method, url, *, api_key, json=None, client=None):
        return {"data": [{"url": "https://s3ai.cn/cdn/x.jpg"}]}

    monkeypatch.setattr(tasks.na, "request_external", ok)

    async def fake_persist(task, task_id, uid, kind="image", request_id=None):
        return task

    monkeypatch.setattr(tasks, "_persist_outputs", fake_persist)

    asyncio.run(tasks._external_call_and_record(
        12, "req-ok-1", "image-gen",
        "https://api.chatfire.cn/v1/images/generations", "sk-x",
        {"model": "gemini-3.1-flash-image-preview_2k"}))

    assert logs["req-ok-1"]["status"] == "succeeded"
