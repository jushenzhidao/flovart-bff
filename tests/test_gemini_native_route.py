"""Gemini 图片模型走网关 /v1beta 原生端点（2026-09-18）。

背景（血案复盘）：
- chatfire 的 /v1/images/generations 只认「图片模型分类」，gemini-*-image 全部
  400 「images endpoint requires an image model」；
- workbuddy 网关侧已把 3 个 gemini 拆到 Gemini 类型渠道（原生 generateContent），
  实测 /v1beta 三模型全部 200 出图（inlineData jpeg）；
- BFF 必须对 gemini 图片模型改走 v1beta/models/{model}:generateContent，
  并把 Gemini 响应转回 OpenAI images 形状，复用既有 normalize/persist 管线。

本文件守护：模型判定、data-url → inlineData、Gemini→OpenAI 响应转换、
_run_sync 分流（path/payload/留痕），以及非 gemini 模型行为完全不变。
"""
import asyncio

import pytest

from app import cloudstore, tasks


# ---------------------------------------------------------------------------
# 1) 模型判定
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("model,expected", [
    ("gemini-3-pro-image-preview", True),
    ("gemini-3.1-flash-image-preview", True),
    ("gemini-3.1-flash-lite-image", True),
    ("Gemini-3-Pro-Image-Preview", True),  # 大小写不敏感
    ("gpt-image-2.5-flare", False),
    ("gpt-image-1", False),
    ("doubao-seedance-1.0", False),
    ("", False),
    (None, False),
])
def test_is_gemini_image_model(model, expected):
    assert tasks._is_gemini_image_model(model or "") is expected


# ---------------------------------------------------------------------------
# 2) image[] → inlineData parts
# ---------------------------------------------------------------------------
def test_gemini_inline_parts_from_data_urls():
    async def main():
        return await tasks._gemini_inline_parts([
            "data:image/png;base64,AAAA",
            "data:image/jpeg;base64,BBBB",
            "", None, "not-a-url",
        ])
    parts = asyncio.run(main())
    assert parts == [
        {"inlineData": {"mimeType": "image/png", "data": "AAAA"}},
        {"inlineData": {"mimeType": "image/jpeg", "data": "BBBB"}},
    ]


def test_gemini_inline_parts_http_failure_skipped(monkeypatch):
    """http 参考图下载失败只跳过该张，不拖死整个请求。"""
    import httpx

    class _Boom:
        async def __aenter__(self):
            raise httpx.ConnectError("boom")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout: _Boom())

    async def main():
        return await tasks._gemini_inline_parts(
            ["https://example.com/a.png", "data:image/png;base64,KEEP"])

    parts = asyncio.run(main())
    assert parts == [{"inlineData": {"mimeType": "image/png", "data": "KEEP"}}]


# ---------------------------------------------------------------------------
# 3) Gemini 响应 → OpenAI images 形状
# ---------------------------------------------------------------------------
def test_gemini_response_converted_to_openai_shape():
    raw = {
        "candidates": [{"content": {"parts": [
            {"text": "给你图"},
            {"inlineData": {"mimeType": "image/jpeg", "data": "QQ=="}},
            {"inline_data": {"mime_type": "image/png", "data": "WQ=="}},
        ]}}],
        "usageMetadata": {"totalTokenCount": 10},
    }
    out = tasks._gemini_response_to_openai(raw)
    assert [d["b64_json"] for d in out["data"]] == ["QQ==", "WQ=="]
    assert out["gemini_text"] == "给你图"


def test_gemini_response_passthrough_non_gemini_shape():
    """错误体 / OpenAI 形状原样透传，不影响既有解析与报错链路。"""
    err_body = {"error": {"message": "boom", "type": "upstream_error"}}
    assert tasks._gemini_response_to_openai(err_body) is err_body
    openai_body = {"data": [{"b64_json": "AA"}]}
    assert tasks._gemini_response_to_openai(openai_body) is openai_body


# ---------------------------------------------------------------------------
# 4) _run_sync 分流：gemini → v1beta + 原生载荷；gpt → 原行为不变
# ---------------------------------------------------------------------------
def _install_fakes(monkeypatch, captured, gw_raw):
    logs: dict[str, dict] = {}

    async def fake_put(uid, request_id, kind, provider, model, params, status="submitted", mode="sync"):
        logs[request_id] = {"status": status, "result": None}
        return logs[request_id]

    async def fake_update(request_id, **fields):
        logs.setdefault(request_id, {}).update(fields)
        return logs[request_id]

    async def fake_gw(method, path, uid, *, json=None, params=None, client=None):
        captured["path"] = path
        captured["json"] = json
        return gw_raw

    async def fake_persist(task, task_id, uid, kind="image", request_id=None):
        return task

    monkeypatch.setattr(cloudstore, "request_log_put", fake_put)
    monkeypatch.setattr(cloudstore, "request_log_update", fake_update)
    monkeypatch.setattr(tasks, "_gw_call", fake_gw)
    monkeypatch.setattr(tasks, "_persist_outputs", fake_persist)
    monkeypatch.setattr(tasks, "_sync_client", lambda: None)
    return logs


def test_run_sync_gemini_routes_to_v1beta(monkeypatch):
    captured: dict = {}
    gw_raw = {"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "image/jpeg", "data": "QQ=="}}]}}]}
    logs = _install_fakes(monkeypatch, captured, gw_raw)

    params = {
        "model": "gemini-3.1-flash-lite-image",
        "prompt": "画一个红苹果",
        "image": ["data:image/png;base64,AAAA"],
        "size": "1024x1024",  # gemini 原生没有 size，应被忽略不报错
    }
    view = asyncio.run(tasks._run_sync(7, "req-gem-1", "image-gen", "v1/images/generations", params))

    assert captured["path"] == "v1beta/models/gemini-3.1-flash-lite-image:generateContent"
    parts = captured["json"]["contents"][0]["parts"]
    assert parts[0] == {"text": "画一个红苹果"}
    assert {"inlineData": {"mimeType": "image/png", "data": "AAAA"}} in parts
    assert logs["req-gem-1"]["status"] == "succeeded"
    assert view["status"] == "succeeded"


def test_run_sync_gpt_keeps_images_endpoint(monkeypatch):
    captured: dict = {}
    logs = _install_fakes(monkeypatch, captured, {"data": [{"b64_json": "QQ=="}]})

    params = {"model": "gpt-image-2.5-flare", "prompt": "a red apple", "size": "1024x1024"}
    asyncio.run(tasks._run_sync(7, "req-gpt-1", "image-gen", "v1/images/generations", params))

    assert captured["path"] == "v1/images/generations"
    assert captured["json"] is params  # 原样透传，不改装
    assert logs["req-gpt-1"]["status"] == "succeeded"


def test_run_sync_gemini_error_still_recorded(monkeypatch):
    """gemini 链路失败同样必须留痕（result.error 带真实原因）。"""
    from app.newapi_client import NewApiError

    captured: dict = {}
    logs = _install_fakes(monkeypatch, captured, {"candidates": []})

    async def boom(method, path, uid, *, json=None, params=None, client=None):
        captured["path"] = path
        raise NewApiError("上游服务暂时不可用，请稍后重试", 502)

    monkeypatch.setattr(tasks, "_gw_call", boom)

    params = {"model": "gemini-3-pro-image-preview", "prompt": "x"}
    with pytest.raises(NewApiError):
        asyncio.run(tasks._run_sync(7, "req-gem-2", "image-gen", "v1/images/generations", params))

    assert captured["path"] == "v1beta/models/gemini-3-pro-image-preview:generateContent"
    assert logs["req-gem-2"]["status"] == "failed"
    assert logs["req-gem-2"]["result"]["error"]["status"] == 502
