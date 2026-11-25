"""外部网关图片异步分支（2026-11-25）。

🔴 背景（渠道 674 血案定论）：
BYOK 服务若指向 new-api「高级自定义异步」渠道（type 8001，如 jieapi.com异步/674）,
同步端点 `/v1/images/generations` 被渠道类型**架构级永久拒绝**
（源码 `IsAdvancedAsyncReservedSyncPath` 保留 + 路径过滤判 `request_path` 400），
唯一可用入口是 `POST /async/v1/images/generations`（提交）+ `GET 同路径/{task_id}`（轮询）。
而 `_run_external` 此前只有一次性同步 POST，对这类渠道必死。

本文件锁定：
1. 分流条件（**只认前端「请求方式」= params.mode**；模型名前缀不再隐式改道）；
2. 提交 URL 拼接（base 带 /v1 必须去重）；
3. 请求体协议（只发 model/prompt/image[]，mask 并入 image[]）；
4. 终态判定（data[] 出现=完成；FAILED/error=失败；无 task_id 立即 failed）。
"""
import asyncio
import json

import pytest

from app import cloudstore, tasks


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _install_fake_log(monkeypatch):
    logs: dict[str, dict] = {}

    async def fake_put(uid, request_id, kind, provider, model, params,
                       status="submitted", mode="sync", source="web"):
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


def _fake_sleep(monkeypatch):
    """轮询 sleep 立即返回，测试不真等 6~10s。"""
    async def instant(_delay):
        return None

    monkeypatch.setattr(tasks.asyncio, "sleep", instant)


def _gw_params(model="gpt-image-2.5-flare", base="https://newapi-bff.oneapis.cn", **extra):
    p = {"model": model, "prompt": "一只橘子", "_gateway": {"base_url": base, "api_key": "sk-x-674"}}
    p.update(extra)
    return p


def _run_flow(monkeypatch, responses, *, model="gpt-image-2.5-flare", request_id="req-a-1"):
    """驱动 _external_async_image_and_record：responses 依序返回（POST + N 次 GET）。

    ⚠️ 调用方需先 _install_fake_log：本函数只驱动「后台执行」，不重复安装日志桩
    （重复安装会换掉 logs dict，导致断言读到空表）。
    """
    _fake_sleep(monkeypatch)
    calls = []
    queue = list(responses)

    async def fake_ext(method, url, *, api_key, json=None, client=None):
        calls.append((method, url, json))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def fake_persist(task, task_id, uid, kind="image", request_id=None):
        assert kind == "image-gen"
        return task

    monkeypatch.setattr(tasks.na, "request_external", fake_ext)
    monkeypatch.setattr(tasks, "_persist_outputs", fake_persist)
    asyncio.run(tasks._external_async_image_and_record(
        7, request_id, "https://newapi-bff.oneapis.cn/async/v1/images/generations",
        "sk-x-674",
        tasks._external_async_image_body({"model": model, "prompt": "一只橘子"})))
    return calls


# ---------------------------------------------------------------------------
# 1) 分流条件
# ---------------------------------------------------------------------------
def test_routes_to_async_branch_when_mode_async(monkeypatch):
    hit = {}

    async def fake(uid, request_id, params):
        hit["called"] = request_id
        return {"routed": "async-image"}

    monkeypatch.setattr(tasks, "_run_external_async_image", fake)
    out = asyncio.run(tasks._run_external(
        7, "req-m-1", "image-gen", {}, _gw_params(model="any-model"), mode="async"))
    assert out == {"routed": "async-image"}
    assert hit["called"] == "req-m-1"


def test_model_prefix_alone_does_not_route_async(monkeypatch):
    """🔒 2026-11-25 飞哥拍板：分流只认界面「请求方式」，不认模型名。

    模型名命中 GATEWAY_ASYNC_IMAGE_MODELS（gpt-image）但 AI 服务没勾异步时，
    必须仍走同步路径 —— 否则用户界面上选的是同步、实际却打了异步入口。
    """
    monkeypatch.setattr(tasks.config, "GATEWAY_ASYNC_IMAGE_MODELS", ["gpt-image"])
    hit = {"async": False}

    async def fake_async(*a, **k):
        hit["async"] = True
        return {}

    async def fake_put(*a, **k):
        return {}

    monkeypatch.setattr(tasks, "_run_external_async_image", fake_async)
    monkeypatch.setattr(cloudstore, "request_log_put", fake_put)
    monkeypatch.setattr(tasks, "_spawn_external_call", lambda *a, **k: None)
    asyncio.run(tasks._run_external(
        7, "req-m-2", "image-gen", {}, _gw_params(model="gpt-image-2.5-flare")))
    assert hit["async"] is False


def test_explicit_mode_sync_wins_for_gpt_image_model(monkeypatch):
    """显式勾「同步」时，即使模型名是 gpt-image 系，也必须打同步端点。"""
    monkeypatch.setattr(tasks.config, "GATEWAY_ASYNC_IMAGE_MODELS", ["gpt-image"])
    hit = {"async": False}

    async def fake_async(*a, **k):
        hit["async"] = True
        return {}

    async def fake_put(*a, **k):
        return {}

    monkeypatch.setattr(tasks, "_run_external_async_image", fake_async)
    monkeypatch.setattr(cloudstore, "request_log_put", fake_put)
    monkeypatch.setattr(tasks, "_spawn_external_call", lambda *a, **k: None)
    asyncio.run(tasks._run_external(
        7, "req-m-3", "image-gen", {}, _gw_params(model="gpt-image-2.5-sunburst-jm"),
        mode="sync"))
    assert hit["async"] is False


# ---------------------------------------------------------------------------
# 2) 提交 URL 拼接
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("base", [
    "https://newapi-bff.oneapis.cn",
    "https://newapi-bff.oneapis.cn/v1",
    "https://newapi-bff.oneapis.cn/v1/",
])
def test_submit_url_dedupes_v1_prefix(monkeypatch, base):
    """base 带不带 /v1 都必须落到 /async/v1/images/generations（不许 /v1/async/v1/…）。"""
    _install_fake_log(monkeypatch)
    seen = []

    async def boom(method, url, *, api_key, json=None, client=None):
        seen.append(url)
        raise RuntimeError("stop here")

    monkeypatch.setattr(tasks.na, "request_external", boom)
    asyncio.run(tasks._run_external_async_image(
        7, "req-u-1", _gw_params(base=base)))
    assert seen == ["https://newapi-bff.oneapis.cn/async/v1/images/generations"]


def test_submit_url_with_trailing_slash_and_path_base(monkeypatch):
    """自定义上游（如 https://jieapi.com）同样拼到 /async/v1/images/generations。"""
    _install_fake_log(monkeypatch)
    seen = []

    async def boom(method, url, *, api_key, json=None, client=None):
        seen.append(url)
        raise RuntimeError("stop here")

    monkeypatch.setattr(tasks.na, "request_external", boom)
    asyncio.run(tasks._run_external_async_image(
        7, "req-u-2", _gw_params(base="https://jieapi.com/")))
    assert seen == ["https://jieapi.com/async/v1/images/generations"]


# ---------------------------------------------------------------------------
# 3) 请求体协议
# ---------------------------------------------------------------------------
def test_body_has_only_doc_three_fields():
    body = tasks._external_async_image_body(
        _gw_params(size="1024x1024", n=2, quality="high"))
    assert body["model"] == "gpt-image-2.5-flare"
    assert body["image"] == []
    # size/n/quality 留在 request_log 里，不外发（上游未必认，会 400 拒收）
    assert "size" not in body and "n" not in body and "quality" not in body
    assert "_gateway" not in body


def test_body_mask_appended_to_image_list():
    body = tasks._external_async_image_body(
        _gw_params(image=["https://cdn/a.png"], mask="https://cdn/m.png"))
    assert body["image"] == ["https://cdn/a.png", "https://cdn/m.png"]
    assert "蒙版" in body["prompt"]


def test_body_empty_prompt_gets_semantic_fallback():
    """纯图操作（remove-background 等）不带 prompt → 注入语义提示词，不空白提交。"""
    p = _gw_params(task="remove-background")
    p.pop("prompt")
    body = tasks._external_async_image_body(p)
    assert "背景" in body["prompt"]


# ---------------------------------------------------------------------------
# 4) 终态判定
# ---------------------------------------------------------------------------
def test_happy_path_polls_until_data_appears(monkeypatch):
    logs = _install_fake_log(monkeypatch)
    calls = _run_flow(monkeypatch, [
        {"task_id": "task_abc"},
        {"task_id": "task_abc", "status": "queued"},
        {"task_id": "task_abc", "status": "in_progress"},
        {"task_id": "task_abc", "status": "succeeded",
         "data": [{"url": "https://cdn/o.png"}]},
    ])
    assert calls[0][0] == "POST"
    assert calls[1] == ("GET",
                        "https://newapi-bff.oneapis.cn/async/v1/images/generations/task_abc",
                        None)
    entry = logs["req-a-1"]
    assert entry["status"] == "succeeded"
    assert entry["task_id"] == "task_abc"
    assert entry["result"]["images"][0]["url"] == "https://cdn/o.png"


def test_submit_without_task_id_fails_immediately(monkeypatch):
    """202 但没 task_id → 无法轮询，立即 failed（不许永远 processing）。"""
    logs = _install_fake_log(monkeypatch)
    _run_flow(monkeypatch, [{"status": "QUEUED"}])
    entry = logs["req-a-1"]
    assert entry["status"] == "failed"
    assert entry["result"]["error"]["type"] == "external_async_image_no_task_id"


def test_submit_upstream_error_surfaces(monkeypatch):
    """上游 200/202 但带 error 体（渠道被拒）→ 直接透传上游错误原文。"""
    logs = _install_fake_log(monkeypatch)
    _run_flow(monkeypatch, [{
        "error": {"message": "No available channel for model gpt-image-2.5-flare (request_path)",
                  "type": "invalid_request"},
    }])
    entry = logs["req-a-1"]
    assert entry["status"] == "failed"
    assert "request_path" in entry["result"]["error"]["message"]


def test_poll_failed_status_records_reason(monkeypatch):
    logs = _install_fake_log(monkeypatch)
    _run_flow(monkeypatch, [
        {"task_id": "task_f"},
        {"task_id": "task_f", "status": "FAILED", "fail_reason": "内容审核未通过"},
    ])
    entry = logs["req-a-1"]
    assert entry["status"] == "failed"
    assert "FAILED" in json.dumps(entry["result"], ensure_ascii=False)


def test_poll_empty_data_is_failure(monkeypatch):
    """200 + data 空 → 无产物失败，绝不许记 succeeded。"""
    logs = _install_fake_log(monkeypatch)
    _run_flow(monkeypatch, [
        {"task_id": "task_e"},
        {"task_id": "task_e", "status": "succeeded", "data": []},
    ])
    entry = logs["req-a-1"]
    assert entry["status"] == "failed"
    assert entry["result"]["error"]["type"] == "external_async_image_no_media"


def test_poll_transient_errors_tolerated(monkeypatch):
    """轮询偶发网络错误在容忍上限内，恢复后照常成功。"""
    logs = _install_fake_log(monkeypatch)
    _run_flow(monkeypatch, [
        {"task_id": "task_t"},
        RuntimeError("connect timeout"),
        RuntimeError("connect timeout"),
        {"task_id": "task_t", "status": "succeeded",
         "data": [{"url": "https://cdn/ok.png"}]},
    ])
    entry = logs["req-a-1"]
    assert entry["status"] == "succeeded"
    assert entry["result"]["images"][0]["url"] == "https://cdn/ok.png"


def test_poll_continuous_errors_abort(monkeypatch):
    """连续网络错误达到上限 → failed，不留 processing 僵尸。"""
    logs = _install_fake_log(monkeypatch)
    _run_flow(monkeypatch, [
        {"task_id": "task_x"},
        *[RuntimeError("down") for _ in range(tasks._EXTERNAL_IMAGE_ASYNC_POLL_MAX_ERRORS)],
    ])
    entry = logs["req-a-1"]
    assert entry["status"] == "failed"
