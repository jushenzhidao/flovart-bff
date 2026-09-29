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


def test_gemini_inline_parts_http_url_downloads_to_inline(monkeypatch):
    """http(s) 参考图 → BFF 下载转 inlineData（rix 渠道不支持 fileData.fileUri）。"""
    import base64 as _b64
    import httpx

    # 最小 PNG（1x1 红点）
    tiny_png = _b64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tiny_png,
                              headers={"content-type": "image/png"})

    fake_dl = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(tasks, "_dl_client", lambda: fake_dl)

    async def main():
        return await tasks._gemini_inline_parts(
            ["https://example.com/a.png?X-Amz-Signature=x",
             "data:image/png;base64,KEEP"])

    parts = asyncio.run(main())
    assert parts[0]["inlineData"]["mimeType"] == "image/png"
    assert _b64.b64decode(parts[0]["inlineData"]["data"]) == tiny_png
    assert parts[1] == {"inlineData": {"mimeType": "image/png", "data": "KEEP"}}


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

    async def fake_gw(method, path, uid, *, json=None, params=None, client=None, pat=None):
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

    async def boom(method, path, uid, *, json=None, params=None, client=None, pat=None):
        captured["path"] = path
        raise NewApiError("上游服务暂时不可用，请稍后重试", 502)

    monkeypatch.setattr(tasks, "_gw_call", boom)

    params = {"model": "gemini-3-pro-image-preview", "prompt": "x"}
    with pytest.raises(NewApiError):
        asyncio.run(tasks._run_sync(7, "req-gem-2", "image-gen", "v1/images/generations", params))

    assert captured["path"] == "v1beta/models/gemini-3-pro-image-preview:generateContent"
    assert logs["req-gem-2"]["status"] == "failed"
    assert logs["req-gem-2"]["result"]["error"]["status"] == 502


# ---------------------------------------------------------------------------
# _no_media_error：上游 200 带 {error:{...}} 时必须透传真实原因（2026-09-26 uid=188）
# ---------------------------------------------------------------------------

def test_no_media_error_passes_through_upstream_error_body():
    from app.tasks import _no_media_error
    raw = {"error": {"code": "SAFETY_BLOCK", "message": "content blocked by provider"}}
    err = _no_media_error(raw)
    assert err["error"]["message"] == "content blocked by provider"
    assert err["error"]["code"] == "SAFETY_BLOCK"
    assert err["error"]["type"] == "upstream_error"


def test_no_media_error_fallback_without_error_key():
    from app.tasks import _no_media_error
    raw = {"created": 1, "usage": {"prompt_tokens": 5, "completion_tokens": 0}}
    err = _no_media_error(raw)
    assert err["error"]["type"] == "upstream_empty_result"
    assert err["error"]["usage"] == {"prompt_tokens": 5, "completion_tokens": 0}


# ---------------------------------------------------------------------------
# gemini 内联图片服务端压缩（2026-09-26：12MB 原图裸传 → 上游 200+error 无产物）
# ---------------------------------------------------------------------------

def _noise_png_bytes(w=1400, h=1400):
    import io as _io
    import os as _os
    from PIL import Image as _Image
    img = _Image.effect_noise((w, h), 64).convert("RGB")
    buf = _io.BytesIO()
    img.save(buf, "PNG", optimize=False)
    return buf.getvalue()


def test_shrink_gemini_inline_passes_small_image_through():
    import base64 as _b64
    from app.tasks import _shrink_gemini_inline
    raw = _noise_png_bytes(400, 400)
    out, mime = _shrink_gemini_inline(_b64.b64encode(raw).decode(), "image/png")
    assert out == _b64.b64encode(raw).decode() and mime == "image/png"


def test_shrink_gemini_inline_downscales_large_image():
    import base64 as _b64
    import io as _io
    from PIL import Image as _Image
    from app.tasks import _shrink_gemini_inline, _GEMINI_INLINE_MAX_SIDE
    raw = _noise_png_bytes(3000, 3000)  # 噪声 PNG 远超 4MB
    out, mime = _shrink_gemini_inline(_b64.b64encode(raw).decode(), "image/png")
    assert mime == "image/jpeg"
    img = _Image.open(_io.BytesIO(_b64.b64decode(out)))
    assert max(img.size) <= _GEMINI_INLINE_MAX_SIDE
    assert len(_b64.b64decode(out)) < len(raw) // 2


def test_shrink_gemini_inline_garbage_passes_through():
    from app.tasks import _shrink_gemini_inline
    garbage = "bm90LWFuLWltYWdl"  # not-an-image 的 base64
    out, mime = _shrink_gemini_inline(garbage, "image/png")
    assert out == garbage and mime == "image/png"


def test_gemini_inline_parts_shrinks_oversize_image():
    import asyncio as _aio
    import base64 as _b64
    from app.tasks import _gemini_inline_parts
    raw = _noise_png_bytes(3000, 3000)
    parts = _aio.run(_gemini_inline_parts(["data:image/png;base64," + _b64.b64encode(raw).decode()]))
    assert len(parts) == 1
    inline = parts[0]["inlineData"]
    assert inline["mimeType"] == "image/jpeg"
    assert len(inline["data"]) < len(_b64.b64encode(raw))
