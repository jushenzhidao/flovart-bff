"""Nano Banana 模式处理：撤销强制同步补丁后的回归守护（2026-11-25）。

事故复盘（本地实测，飞哥的 jieapi.com 异步渠道）：
- 曾误判为「前端把模型配成 async → 打了 video 端点嵌套体 → 上游读不到 model」，
  据此给 nano-banana-* 加了强制 sync 补丁。
- 实测定论：网关 /v1/models 里该模型 owned_by="advanced custom async"，
  async/v1/images/generations 提交 202 后查询即 503「分组 default 下无可用渠道」，
  同步端点亦 503；同类型 nano-banana-pro 同症状，普通模型 gpt-image-1 正常。
  → 真因是网关侧该渠道 distributor 不可达，非 BFF 端点/载荷问题。
- 故撤销强制同步：BFF 不再对 nano-banana 特判，完全尊重 params.mode（与所有模型一致）。

本文件守护：
1) nano-banana 传 async → 走网关异步任务端点，不被偷偷改成 sync；
2) nano-banana 传 sync → 走同步端点，同样不特判；
3) 模式解析对任意模型一致（无隐藏特例）。
"""
import asyncio

from app import cloudstore, config, tasks


def _fake_logs(monkeypatch, captured):
    logs = {}

    async def fake_put(uid, request_id, kind, provider, model, params,
                       status="submitted", mode="sync", source="web"):
        logs[request_id] = {"status": status, "mode": mode, "model": model, "params": params}
        return logs[request_id]

    async def fake_update(request_id, **fields):
        logs.setdefault(request_id, {}).update(fields)
        return logs[request_id]

    def fake_spawn(uid, request_id, kind, path, params, pat=None):
        captured["spawn"] = {"path": path, "params": dict(params), "kind": kind}

    async def fake_gw(method, path, uid, *, json=None, params=None, client=None, pat=None):
        captured["gw"] = {"method": method, "path": path, "json": json}
        return {"task_id": "t-nano-probe", "status": "QUEUED"}

    def fake_normalize(raw):
        return {"task_id": raw.get("task_id"), "status": "QUEUED"}

    monkeypatch.setattr(cloudstore, "request_log_put", fake_put)
    monkeypatch.setattr(cloudstore, "request_log_update", fake_update)
    monkeypatch.setattr(tasks, "_spawn_sync_call", fake_spawn)
    monkeypatch.setattr(tasks, "_gw_call", fake_gw)
    monkeypatch.setattr(tasks, "_normalize_task", fake_normalize)
    monkeypatch.setattr(tasks, "_proxy_client", lambda: None)
    return logs


def test_nano_banana_async_respected_not_forced_sync(monkeypatch):
    """撤销补丁后：async 就是 async，不再被偷偷拽到同步端点。"""
    captured = {}
    logs = _fake_logs(monkeypatch, captured)

    view = asyncio.run(tasks.submit(64, "image-gen", {
        "model": "nano-banana-2",
        "prompt": "生成一只老鹰",
        "size": "1024x1024",
        "mode": "async",
    }))

    assert view["mode"] == "async"
    assert captured["gw"]["path"] == config.GATEWAY_IMAGE_TASKS_PATH
    assert logs[view["id"]]["mode"] == "async"
    assert "spawn" not in captured  # 绝没被偷偷改成同步


def test_nano_banana_sync_respected_too(monkeypatch):
    """显式 sync 也照办：对 nano-banana 没有任何隐藏特判。"""
    captured = {}
    logs = _fake_logs(monkeypatch, captured)

    view = asyncio.run(tasks.submit(64, "image-gen", {
        "model": "nano-banana-2", "prompt": "a cat", "mode": "sync",
    }))

    assert view["mode"] == "sync"
    assert captured["spawn"]["path"] == config.GATEWAY_SYNC_IMAGE_PATH
    assert logs[view["id"]]["mode"] == "sync"
    assert "gw" not in captured


def test_gpt_image_async_untouched(monkeypatch):
    """普通异步模型不受影响。"""
    captured = {}
    logs = _fake_logs(monkeypatch, captured)

    view = asyncio.run(tasks.submit(64, "image-gen", {
        "model": "gpt-image-1", "prompt": "a cat", "mode": "async",
    }))

    assert view["mode"] == "async"
    assert captured["gw"]["path"] == config.GATEWAY_IMAGE_TASKS_PATH
    assert logs[view["id"]]["mode"] == "async"
    assert "spawn" not in captured
