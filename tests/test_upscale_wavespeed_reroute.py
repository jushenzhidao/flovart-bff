"""高清放大改道 wavespeed 直连（2026-10-21）。

背景：网关 images 通道对 gemini 系模型（渠道 type=24 Gemini 原生）的
images/generations 转换只支持 imagen → 平台链路 upscale 一律
convert_request_failed；gpt-image 渠道上游 chatfire 也时好时坏。
故平台模式 upscale 改道 wavespeed 直连（wavespeed-ai/image-upscaler）。

锁定行为：
1. image-gen + task=upscale（平台模式）→ 重写为 kind=upscale + source_media_key；
2. 直接 type=upscale + dataURL → 同样收编；
3. BYOK（带 _gateway）→ **同样改道**（2026-10-21 二轮：外部 new-api 网关不认识
   task='upscale' 非标语义，images 通道对 upscale 是必死路，实测 oneapis workbuddy
   convert_request_failed）；
4. WAVESPEED_API_KEY 未配置 → 不改道（回退网关路径）；
5. wavespeed.submit_upscale 请求体与 prediction id 解析。
"""

import asyncio

import app.tasks as tasks
from app import config

PNG_A = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def _enable_wavespeed(monkeypatch):
    monkeypatch.setattr(config, "WAVESPEED_ENABLED", True)
    monkeypatch.setattr(tasks.config, "WAVESPEED_ENABLED", True)


def _stub_media_put(monkeypatch, captured):
    import app.cloudstore as cloudstore

    async def fake_media_put(uid, kind, mime, blob, **k):
        captured["media_put"] = (uid, kind, mime, len(blob))
        return {"media_key": "mk-123", "size": len(blob), "mime": mime, "url": "/api/me/media/mk-123"}

    monkeypatch.setattr(cloudstore, "media_put", fake_media_put)


def _stub_thirdparty(monkeypatch, captured):
    async def fake_submit_thirdparty(uid, request_id, kind, spec, params):
        captured["thirdparty"] = (uid, kind, dict(params))
        return {"id": request_id, "taskId": "pred-1", "kind": kind, "mode": "async", "status": "processing"}

    monkeypatch.setattr(tasks, "_submit_thirdparty", fake_submit_thirdparty)


def _stub_sync(monkeypatch, captured):
    async def fake_run_sync(uid, request_id, kind, path, params, pat=None):
        captured["sync"] = (kind, path, dict(params))
        return {"status": "succeeded", "result": {"data": []}}

    monkeypatch.setattr(tasks, "_run_sync", fake_run_sync)

    import app.cloudstore as cloudstore

    async def fake_log(*a, **k):
        return None

    monkeypatch.setattr(cloudstore, "request_log_put", fake_log)
    monkeypatch.setattr(tasks.cloudstore, "request_log_put", fake_log)
    monkeypatch.setattr(tasks.cloudstore, "request_log_update", fake_log)


def test_platform_image_gen_upscale_reroutes_to_wavespeed(monkeypatch):
    captured: dict = {}
    _enable_wavespeed(monkeypatch)
    _stub_media_put(monkeypatch, captured)
    _stub_thirdparty(monkeypatch, captured)

    res = asyncio.run(tasks.submit(7, "image-gen", {
        "model": "gemini-3.1-flash-image-preview", "task": "upscale",
        "image": [PNG_A], "options": {},
    }))

    uid, kind, params = captured["thirdparty"]
    assert kind == "upscale"
    assert params["source_media_key"] == "mk-123"
    assert "image" not in params and "model" not in params
    assert res["status"] == "processing"
    # 源图确实解码落盘（1x1 png = 70 字节左右，>0 即可）
    assert captured["media_put"][2] == "image/png"
    assert captured["media_put"][3] > 0


def test_direct_upscale_kind_also_reroutes(monkeypatch):
    captured: dict = {}
    _enable_wavespeed(monkeypatch)
    _stub_media_put(monkeypatch, captured)
    _stub_thirdparty(monkeypatch, captured)

    asyncio.run(tasks.submit(7, "upscale", {"image": [PNG_A]}))
    _, kind, params = captured["thirdparty"]
    assert kind == "upscale"
    assert params["source_media_key"] == "mk-123"


def test_byok_gateway_upscale_also_reroutes(monkeypatch):
    """二轮修复（2026-10-21 14:45 实测）：BYOK 网关的 upscale 也必须改道——
    外部 new-api 网关不认识 task='upscale'，原路径必 convert_request_failed。"""
    captured: dict = {}
    _enable_wavespeed(monkeypatch)
    _stub_media_put(monkeypatch, captured)
    _stub_thirdparty(monkeypatch, captured)

    asyncio.run(tasks.submit(7, "image-gen", {
        "model": "m1", "task": "upscale", "image": [PNG_A],
        "mode": "sync",
        "_gateway": {"base_url": "https://my.own/v1", "api_key": "sk-mine"},
    }))

    uid, kind, params = captured["thirdparty"]
    assert kind == "upscale"
    assert params["source_media_key"] == "mk-123"
    assert "_gateway" not in params and "image" not in params
    assert "media_put" in captured


def test_no_wavespeed_key_falls_back_to_gateway(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(config, "WAVESPEED_ENABLED", False)
    monkeypatch.setattr(tasks.config, "WAVESPEED_ENABLED", False)
    _stub_media_put(monkeypatch, captured)
    _stub_sync(monkeypatch, captured)

    asyncio.run(tasks.submit(7, "image-gen", {
        "model": "m1", "task": "upscale", "image": [PNG_A],
    }))

    kind, _path, params = captured["sync"]
    assert kind == "image-gen"
    assert "media_put" not in captured


def test_upscale_bad_image_param_raises(monkeypatch):
    captured: dict = {}
    _enable_wavespeed(monkeypatch)
    _stub_media_put(monkeypatch, captured)
    _stub_thirdparty(monkeypatch, captured)

    try:
        asyncio.run(tasks.submit(7, "upscale", {"image": ["not-a-dataurl"]}))
    except ValueError as e:
        assert "dataURL" in str(e)
    else:
        raise AssertionError("应抛 ValueError")


def test_wavespeed_submit_upscale_body_and_id(monkeypatch):
    """适配器单测：提交体仅 image，prediction id 多键容错。"""
    from app.thirdparty import wavespeed as ws

    captured: dict = {}

    class _FakeResp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"data": {"id": "pred-abc"}, "success": True}

    class _FakeClient:
        def __init__(self):
            self.calls = []

        async def post(self, path, json=None):
            self.calls.append((path, json))
            return _FakeResp()

    fake = _FakeClient()

    async def fake_resolve(uid, key):
        return "https://cdn.example/src.png"

    monkeypatch.setattr(ws, "_resolve_source_url", fake_resolve)
    monkeypatch.setattr(ws, "_client", lambda: fake)

    pred = asyncio.run(ws.submit_upscale(7, "mk-1"))
    assert pred == "pred-abc"
    path, body = fake.calls[0]
    assert path == "/v3/" + config.WAVESPEED_UPSCALE_MODEL
    assert body == {"image": "https://cdn.example/src.png"}

    # 2026-10-21 透传：新字段直传（白名单校验），旧 targetLongEdge 兼容映射，非法值丢弃
    asyncio.run(ws.submit_upscale(7, "mk-1", options={"targetResolution": "8k", "outputFormat": "png"}))
    assert fake.calls[1][1] == {"image": "https://cdn.example/src.png", "target_resolution": "8k", "output_format": "png"}
    asyncio.run(ws.submit_upscale(7, "mk-1", options={"targetLongEdge": 4096}))
    assert fake.calls[2][1] == {"image": "https://cdn.example/src.png", "target_resolution": "4k"}
    asyncio.run(ws.submit_upscale(7, "mk-1", options={"targetResolution": "16k", "outputFormat": "bmp"}))
    assert fake.calls[3][1] == {"image": "https://cdn.example/src.png"}
    # real-esrgan 不吃这些参数（模型名守卫）
    asyncio.run(ws.submit_upscale(7, "mk-1", model="wavespeed-ai/real-esrgan", options={"targetResolution": "8k"}))
    assert fake.calls[4][1] == {"image": "https://cdn.example/src.png"}
