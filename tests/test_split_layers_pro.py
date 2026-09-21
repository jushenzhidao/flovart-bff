"""split-layers 双模型接入测试（2026-09-21 Seedream Pro 分层，仅管理员）。

覆盖三件事：
1. resolve_split_model：语义值 'pro' → Seedream 模型 id；缺省/未知值 → 默认 qwen。
2. build_split_layers_body：两模型的参数形态分支 —— Pro 必须带 output_format=png
   （jpeg 会丢透明通道，血案预备役）、不吃 num_layers；qwen 保持 num_layers。
3. 管理员闸门：普通用户带 model:'pro' 提交 split-layers → 403；管理员放行。
"""
import pytest

from app import config, tasks
from app.thirdparty import wavespeed as ws


# ---------- 1. 语义值 → 模型 id ----------
def test_resolve_split_model_default_is_qwen():
    assert tasks.resolve_split_model({}) == config.WAVESPEED_SPLIT_MODEL
    assert tasks.resolve_split_model({"model": ""}) == config.WAVESPEED_SPLIT_MODEL
    assert tasks.resolve_split_model({"model": "qwen"}) == config.WAVESPEED_SPLIT_MODEL


def test_resolve_split_model_pro_semantic_values():
    for v in ("pro", "PRO", "seedream", "seedream-pro"):
        assert tasks.resolve_split_model({"model": v}) == config.WAVESPEED_SPLIT_MODEL_PRO


# ---------- 2. 提交体参数形态 ----------
def test_split_body_qwen_keeps_num_layers():
    body = ws.build_split_layers_body(
        config.WAVESPEED_SPLIT_MODEL, "https://example.com/a.png", num_layers=5,
        prompt="人物/背景")
    assert body["num_layers"] == 5
    assert body["image"] == "https://example.com/a.png"
    assert body["prompt"] == "人物/背景"
    assert "output_format" not in body


def test_split_body_pro_uses_resolution_and_png():
    body = ws.build_split_layers_body(
        config.WAVESPEED_SPLIT_MODEL_PRO, "https://example.com/a.png",
        num_layers=4, prompt="person, background, logo", resolution="2K")
    assert body["resolution"] == "2k"          # 归一小写
    assert body["output_format"] == "png"      # 保透明，绝不能是 jpeg
    assert "num_layers" not in body            # Pro 不吃层数
    assert body["prompt"] == "person, background, logo"


def test_split_body_pro_defaults_resolution_from_config(monkeypatch):
    monkeypatch.setattr(config, "WAVESPEED_SPLIT_PRO_RESOLUTION", "1.5k")
    body = ws.build_split_layers_body(config.WAVESPEED_SPLIT_MODEL_PRO, "https://x/a.png")
    assert body["resolution"] == "1.5k"
    assert "prompt" not in body


# ---------- 3. 管理员闸门（router 403，直调风格同 test_console_requests）----------
def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_split_layers_pro_blocked_for_normal_user():
    from app.routers import tasks as tasks_router

    with pytest.raises(Exception) as ei:
        _run(tasks_router.create_task(
            tasks_router.SubmitBody(
                type="split-layers",
                params={"model": "pro", "source_media_key": "k"}),
            session={"uid": 42, "role": 1}))
    assert getattr(ei.value, "status_code", None) == 403


def test_split_layers_pro_allowed_for_admin(monkeypatch):
    from app.routers import tasks as tasks_router

    captured = {}

    async def fake_submit(uid, kind, params):
        captured.update(uid=uid, kind=kind, params=params)
        return {"id": "req-1", "status": "processing"}

    monkeypatch.setattr(tasks, "submit", fake_submit)
    body = _run(tasks_router.create_task(
        tasks_router.SubmitBody(
            type="split-layers",
            params={"model": "pro", "source_media_key": "k", "resolution": "1k"}),
        session={"uid": 1, "role": 10}))
    assert body["success"] is True
    assert captured["params"]["model"] == "pro"


def test_split_layers_default_model_no_gate(monkeypatch):
    """普通用户走默认 qwen：不得被 Pro 闸门拦（model 缺省 → 默认模型）。"""
    from app.routers import tasks as tasks_router

    captured = {}

    async def fake_submit(uid, kind, params):
        captured.update(uid=uid, kind=kind, params=params)
        return {"id": "req-2", "status": "processing"}

    monkeypatch.setattr(tasks, "submit", fake_submit)
    body = _run(tasks_router.create_task(
        tasks_router.SubmitBody(
            type="split-layers",
            params={"source_media_key": "k", "num_layers": 3}),
        session={"uid": 42, "role": 1}))
    assert body["success"] is True
    assert "model" not in captured["params"]
