"""能力开关接线测试（2026-09-29）。

adapt() 此前写了但从没被 tasks.submit 调用——管理台开关翻了也不生效。
本文件锁两件事：
1. 开关关闭（默认）时 submit 行为逐字节不变（adapt 未命中不改 params）；
2. 开关打开时 model 被换成 wenxin:*、多余参数不外发。
"""

import asyncio

import app.tasks as tasks
from app import upstream_profiles

# 1x1 PNG（合法魔数，过得了 _fix_params_image_mime 的嗅探）
PNG_A = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
PNG_B = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


def _install(monkeypatch, captured: dict, *, overrides: dict | None = None):
    """桩掉 submit → _run_sync 的全部外设，捕获最终外发 params。"""
    import app.cloudstore as cloudstore

    async def fake_log(*a, **k):
        return None

    monkeypatch.setattr(cloudstore, "request_log_put", fake_log)
    monkeypatch.setattr(tasks.cloudstore, "request_log_put", fake_log)
    monkeypatch.setattr(tasks.cloudstore, "request_log_update", fake_log)

    async def fake_run_sync(uid, request_id, kind, path, params, pat=None):
        captured["kind"] = kind
        captured["path"] = path
        captured["params"] = params
        return {"status": "succeeded", "result": {"data": []}}

    monkeypatch.setattr(tasks, "_run_sync", fake_run_sync)

    real_adapt = upstream_profiles.adapt
    calls: list = []

    def spy_adapt(kind, params):
        calls.append(kind)
        return real_adapt(kind, params)

    monkeypatch.setattr(tasks.upstream_profiles, "adapt", spy_adapt)

    if overrides:
        # ⭐ 快照-恢复式清理（2026-10-08）：覆盖层是**真实磁盘文件**且优先级高于
        #    env 默认 —— 旧版 _cleanup_overrides 只把键置 "off"，残留条目会把
        #    后跑的 test_upstream_profiles（monkeypatch env=baidu）压成 off →
        #    全量跑必挂、单独跑侥幸过。这里先快照、测试结束原样恢复。
        from app import store

        _OVERRIDES_SNAPSHOT[0] = store.load_json(upstream_profiles._overrides_path(), {})
        real_set = upstream_profiles.set_override
        for k, v in overrides.items():
            real_set(k, v)

    return calls


_OVERRIDES_SNAPSHOT: list = [None]


def _cleanup_overrides():
    if _OVERRIDES_SNAPSHOT[0] is None:
        return
    from app import store

    try:
        store.save_json_atomic(upstream_profiles._overrides_path(), _OVERRIDES_SNAPSHOT[0])
    except Exception:  # noqa: BLE001 —— 清理失败不掩盖测试本身的断言结果
        pass
    _OVERRIDES_SNAPSHOT[0] = None


# ---------------------------------------------------------------------------
# 开关关闭：默认行为零变化（回归红线）
# ---------------------------------------------------------------------------
def test_switch_off_leaves_params_untouched(monkeypatch):
    captured: dict = {}
    calls = _install(monkeypatch, captured)
    params = {"model": "gpt-image-2", "image": [PNG_A],
              "prompt": "p", "variant": "mask", "mode": "sync"}
    asyncio.run(tasks.submit(7, "image-gen", dict(params)))
    assert calls == ["mask"]            # variant 命中档案名 → adapt 被询问
    assert captured["params"] == params  # 开关 off → 原样透传（逐字段等价）


def test_unknown_variant_skips_adapt(monkeypatch):
    captured: dict = {}
    _install(monkeypatch, captured)
    params = {"model": "m", "prompt": "p", "variant": "relight", "image": [PNG_A]}
    asyncio.run(tasks.submit(7, "image-gen", dict(params)))
    # annotate/relight/edit 不建档案（规格书 §4.1）→ adapt 未命中，params 原样
    assert captured["params"] == params


# ---------------------------------------------------------------------------
# 开关打开：model 换 wenxin:*，白名单字段外发
# ---------------------------------------------------------------------------
def test_switch_on_swaps_model_via_variant(monkeypatch):
    captured: dict = {}
    try:
        _install(monkeypatch, captured, overrides={"mask": "baidu"})
        asyncio.run(tasks.submit(7, "image-gen", {
            "model": "gpt-image-2", "image": [PNG_A],
            "mask": PNG_B, "prompt": "改这里",
            "variant": "mask", "mode": "sync"}))
        params = captured["params"]
        assert params["model"] == "wenxin:replace"
        assert "mode" not in params and "variant" not in params  # 多余参数不外发
    finally:
        _cleanup_overrides()


def test_switch_on_direct_kind(monkeypatch):
    captured: dict = {}
    try:
        _install(monkeypatch, captured, overrides={"dewatermark": "baidu"})
        asyncio.run(tasks.submit(7, "dewatermark", {
            "model": "gemini-3.1-flash-image-preview",
            "image": [PNG_A]}))
        assert captured["params"]["model"] == "wenxin:dewatermark"
        assert captured["kind"] == "dewatermark"
    finally:
        _cleanup_overrides()
