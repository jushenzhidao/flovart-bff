"""外部网关 video-gen 分支（2026-10-08）。

🔴 背景（飞哥实测 minimax 视频生成必失败）：
前端 BYOK 视频服务带 `_gateway` → `_run_external` 只有同步图片语义
（`path = spec.get("sync_path") or "v1/images/generations"`）→ 视频任务被拼成
`{base}/v1/images/generations` 畸形 URL → 返回网关前端 HTML → 502。

修复：`_run_external` 对 kind="video-gen" 分流到 `_run_external_video`
（任务端点提交 + 后台轮询 + 视频落盘）。本文件锁定：
1. 提交 body 协议（顶层 model + 火山 content[]，无 {"type","params"} 包装）；
2. 轮询 URL 拼接（v1 前缀去重 + /{task_id}）；
3. 终态判定与留痕（成功落 videos / 失败写原因 / 无任务号即 failed）。
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


def _fake_sleep(monkeypatch):
    """把轮询 sleep 变成立即返回，测试不真等 5-15s。"""
    async def instant(_delay):
        return None

    monkeypatch.setattr(tasks.asyncio, "sleep", instant)


# ---------------------------------------------------------------------------
# 1) 提交 body 协议
# ---------------------------------------------------------------------------
def test_video_body_is_volc_protocol_with_top_level_model():
    """body 必须顶层带 model + 火山 content[]；绝不允许 {"type","params"} 包装
    （网关实测报 Model name not specified）。"""
    params = {
        "model": "minimax",
        "prompt": "一只猫在跑",
        "aspect_ratio": "16:9",
        "duration": 5,
        "resolution": "720p",
        "_gateway": {"base_url": "https://gw.example.com/v1", "api_key": "sk-x"},
    }
    body = tasks._external_video_body(params)
    assert body["model"] == "minimax"
    assert "type" not in body and "params" not in body
    assert body["content"][0] == {"type": "text", "text": "一只猫在跑"}
    # aspect_ratio → 补一份上游字段名 ratio
    assert body["ratio"] == "16:9" and body["aspect_ratio"] == "16:9"
    assert body["duration"] == 5 and body["resolution"] == "720p"
    assert "_gateway" not in body


def test_video_body_first_frame_becomes_image_content():
    """first_frame_url → content 里的 image_url 节点（图生视频）。"""
    body = tasks._external_video_body({
        "model": "minimax", "prompt": "动起来",
        "first_frame_url": "https://oss/a.png",
    })
    img = [c for c in body["content"] if c["type"] == "image_url"]
    assert len(img) == 1
    assert img[0]["image_url"] == {"url": "https://oss/a.png"}
    assert img[0]["role"] == "first_frame"
    assert "first_frame_url" not in body


def test_video_body_last_frame_becomes_image_content():
    """last_frame_url → content 里 role=last_frame 的 image_url 节点（首尾帧模式）。
    2026-10-13 P0：此前第二张图被整体丢弃，首尾帧模型只收到首帧。"""
    body = tasks._external_video_body({
        "model": "seedance", "prompt": "过渡",
        "first_frame_url": "https://oss/a.png",
        "last_frame_url": "https://oss/b.png",
    })
    imgs = [c for c in body["content"] if c["type"] == "image_url"]
    assert [c["role"] for c in imgs] == ["first_frame", "last_frame"]
    assert imgs[1]["image_url"] == {"url": "https://oss/b.png"}
    assert "last_frame_url" not in body
    # 无尾帧时不产生空节点
    solo = tasks._external_video_body({"model": "m", "prompt": "x"})
    assert all(c.get("role") != "last_frame" for c in solo.get("content", []))


def test_video_body_passes_camera_scalars_through():
    """camera_fixed / camera 等新标量原样透传顶层（插件不认识就忽略，不破坏提交）。"""
    body = tasks._external_video_body({
        "model": "seedance", "prompt": "x",
        "camera_fixed": True, "camera": "camera push in",
    })
    assert body["camera_fixed"] is True
    assert body["camera"] == "camera push in"


def test_video_body_empty_prompt_still_submits():
    """纯图生视频（无 prompt）也要有合法 body：content 只有 image 节点也行，
    prompt 为空时可以不带 content。"""
    body = tasks._external_video_body({"model": "m", "prompt": "  "})
    assert body["model"] == "m" and "content" not in body


# ---------------------------------------------------------------------------
# 2) 轮询 URL 拼接（_run_external_video）
# ---------------------------------------------------------------------------
def test_run_external_routes_video_gen_to_video_branch(monkeypatch):
    """kind=video-gen 必须进 _run_external_video，不得再落 sync_path 图片语义。"""
    called = {}

    async def fake_video(uid, request_id, params):
        called.update(uid=uid, request_id=request_id, params=params)
        return {"routed": True}

    monkeypatch.setattr(tasks, "_run_external_video", fake_video)
    params = {"model": "minimax", "_gateway": {"base_url": "https://gw", "api_key": "sk"}}
    out = asyncio.run(tasks._run_external(
        7, "req-v-route", "video-gen", {}, params))
    assert out == {"routed": True}
    assert called["request_id"] == "req-v-route" and called["params"] is params


def test_video_submit_url_dedupes_v1_prefix(monkeypatch):
    """base_url 带 /v1 时不得拼出 /v1/v1/video/generations。"""
    logs = _install_fake_log(monkeypatch)
    _fake_sleep(monkeypatch)
    seen_urls = []

    async def boom(method, url, *, api_key, json=None, client=None):
        seen_urls.append((method, url))
        raise RuntimeError("stop here")

    monkeypatch.setattr(tasks.na, "request_external", boom)
    params = {
        "model": "minimax", "prompt": "x",
        "_gateway": {"base_url": "https://gw.example.com/v1", "api_key": "sk"},
    }
    asyncio.run(tasks._run_external_video(7, "req-v-url", params))
    method, url = seen_urls[0]
    assert method == "POST"
    assert url == "https://gw.example.com/v1/video/generations"
    # 落库不含 _gateway（明文 key 绝不入库）
    assert "_gateway" not in logs["req-v-url"]["payload"]


def test_video_direct_task_endpoint_mode(monkeypatch):
    """baseUrl 已是完整任务端点（飞哥 2026-10-08 实配形态）：
    提交 = POST base 本体，轮询 = GET base/{task_id}，不得追加 /v1/* 后缀。"""
    logs = _install_fake_log(monkeypatch)
    _fake_sleep(monkeypatch)
    seen = []

    async def fake_ext(method, url, *, api_key, json=None, client=None):
        seen.append((method, url))
        if method == "POST":
            return {"id": "tk-1", "status": "queued"}
        return {"id": "tk-1", "status": "succeeded",
                "content": {"video_url": "https://cdn/out.mp4"}}

    async def fake_persist(task, task_id, uid, kind="image", request_id=None):
        return task

    monkeypatch.setattr(tasks.na, "request_external", fake_ext)
    monkeypatch.setattr(tasks, "_persist_outputs", fake_persist)
    base = "https://tasks.oneapis.cn/aivideomaker/api/v3/contents/generations/tasks"
    params = {
        "model": "minimax", "prompt": "一只猫",
        "_gateway": {"base_url": base, "api_key": "sk"},
    }
    asyncio.run(tasks._run_external_video(7, "req-v-direct", params))
    assert seen[0] == ("POST", base)
    assert ("GET", f"{base}/tk-1") in seen
    entry = logs["req-v-direct"]
    assert entry["status"] == "succeeded"
    assert entry["result"]["videos"][0]["url"] == "https://cdn/out.mp4"


# ---------------------------------------------------------------------------
# 3) 提交 / 轮询终态
# ---------------------------------------------------------------------------
def _run_video_flow(monkeypatch, responses, logs):
    """驱动 _external_video_and_record：responses 依序返回（POST 一条 + GET N 条）。"""
    calls = []
    queue = list(responses)

    async def fake_ext(method, url, *, api_key, json=None, client=None):
        calls.append((method, url, json))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def fake_persist(task, task_id, uid, kind="image", request_id=None):
        assert kind == "video-gen"
        return task

    monkeypatch.setattr(tasks.na, "request_external", fake_ext)
    monkeypatch.setattr(tasks, "_persist_outputs", fake_persist)
    asyncio.run(tasks._external_video_and_record(
        7, "req-v-1", "https://gw/v1/video/generations", "https://gw/v1/video/generations",
        "sk-x", {"model": "minimax"}))
    return calls


def test_video_happy_path_polls_until_succeeded(monkeypatch):
    logs = _install_fake_log(monkeypatch)
    _fake_sleep(monkeypatch)
    calls = _run_video_flow(monkeypatch, [
        {"task_id": "t-9"},
        {"task_id": "t-9", "status": "running"},
        {"task_id": "t-9", "status": "succeeded",
         "content": {"video_url": "https://cdn/v.mp4"}},
    ], logs)
    assert calls[0][0] == "POST"
    assert calls[1] == ("GET", "https://gw/v1/video/generations/t-9", None)
    entry = logs["req-v-1"]
    assert entry["status"] == "succeeded"
    assert entry["result"]["videos"][0]["url"] == "https://cdn/v.mp4"
    assert entry["task_id"] == "t-9"


def test_video_failed_status_records_reason(monkeypatch):
    logs = _install_fake_log(monkeypatch)
    _fake_sleep(monkeypatch)
    _run_video_flow(monkeypatch, [
        {"task_id": "t-9"},
        {"task_id": "t-9", "status": "failed", "fail_reason": "内容审核未通过"},
    ], logs)
    entry = logs["req-v-1"]
    assert entry["status"] == "failed"
    err = entry["result"]["error"]
    assert err["type"] == "external_video_failed"
    assert "内容审核未通过" in err["message"]


def test_video_submit_without_task_id_fails_immediately(monkeypatch):
    """202 无任务号无法轮询 → 立即 failed，不许永远 processing。"""
    logs = _install_fake_log(monkeypatch)
    _fake_sleep(monkeypatch)
    _run_video_flow(monkeypatch, [{"ok": True}], logs)
    entry = logs["req-v-1"]
    assert entry["status"] == "failed"
    assert entry["result"]["error"]["type"] == "external_video_no_task_id"


def test_video_submit_sync_rejected_surfaces_upstream_message(monkeypatch):
    """上游同步拒绝（任务插件 payload 校验 FAILED，无 task_id）→
    错误信息直接透出上游原文，而不是笼统的「未返回任务号」。"""
    logs = _install_fake_log(monkeypatch)
    _fake_sleep(monkeypatch)
    _run_video_flow(monkeypatch, [{
        "error": {"code": "invalid_request", "message": json.dumps({
            "status": "FAILED", "errorCode": "INVALID_PAYLOAD",
            "message": "duration: Number must be greater than or equal to 5",
        })},
    }], logs)
    entry = logs["req-v-1"]
    assert entry["status"] == "failed"
    err = entry["result"]["error"]
    assert err["type"] == "external_video_rejected"
    assert "duration" in err["message"]
    assert "INVALID_PAYLOAD" in err["message"]


def test_video_succeeded_without_media_fails(monkeypatch):
    """status=succeeded 但抽不出任何视频 URL → failed 留痕（不许假成功）。"""
    logs = _install_fake_log(monkeypatch)
    _fake_sleep(monkeypatch)
    _run_video_flow(monkeypatch, [
        {"task_id": "t-9"},
        {"task_id": "t-9", "status": "succeeded", "usage": {"total_tokens": 1}},
    ], logs)
    entry = logs["req-v-1"]
    assert entry["status"] == "failed"
    assert entry["result"]["error"]["type"] == "external_video_no_media"


def test_video_poll_transient_errors_tolerated(monkeypatch):
    """轮询偶发网络错误容忍（连续上限内），恢复后照常成功。"""
    logs = _install_fake_log(monkeypatch)
    _fake_sleep(monkeypatch)
    calls = _run_video_flow(monkeypatch, [
        {"task_id": "t-9"},
        RuntimeError("conn reset"),
        RuntimeError("conn reset"),
        {"task_id": "t-9", "status": "succeeded",
         "content": {"video_url": "https://cdn/v.mp4"}},
    ], logs)
    assert len(calls) == 4
    assert logs["req-v-1"]["status"] == "succeeded"


def test_extract_video_urls_variants():
    """URL 抽取：video_url 键 / videos 列表 / .mp4 启发式；去重保序。"""
    volc = {"content": {"video_url": "https://cdn/a.mp4"}}
    assert tasks._extract_video_urls(volc) == ["https://cdn/a.mp4"]
    listed = {"videos": [{"url": "https://cdn/b.mp4"}, {"url": "https://cdn/b.mp4"}]}
    assert tasks._extract_video_urls(listed) == ["https://cdn/b.mp4"]
    hint = {"data": {"url": "https://cdn/c.mp4"}}
    assert tasks._extract_video_urls(hint) == ["https://cdn/c.mp4"]
    # 非 video 语义的 url 不误收（图片封面等）
    noise = {"cover": {"url": "https://cdn/cover.png"}}
    assert tasks._extract_video_urls(noise) == []
