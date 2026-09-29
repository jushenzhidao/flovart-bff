"""上游 gemini 带图请求 inline-400 自动重试（飞哥 2026-09-28 拍板）。

## 背景（实锤见 .workbuddy/memory/2026-09-28.md）

上游 rix 的 gemini 图片通道是多节点：坏节点对带图请求（inlineData）返回
400 `contents[0].parts[0].data: required oneof field 'data' must have one
initialized field`，好节点正常 —— 间歇性失败（9/28 15:40 成功→15:54 失败→
16:1x 成功）。重试大概率换到好节点。

守护的行为：
1. 异常形态（_gw_call 抛 NewApiError 400 + 签名）→ 自动重试，成功即回；
2. 错误体形态（HTTP 200 + {error:{message 命中签名}}）→ 同样重试；
3. 最多重试 2 次（共 3 次尝试），耗尽后按原错误落库失败；
4. 文生图（无 inlineData）400 不重试 —— 那是真错误；
5. 带图但 400 消息不匹配签名（如 MIME 拒绝）→ 不重试，防拖慢真实错误。
"""
import asyncio

import pytest

from app import cloudstore, newapi_client as na
from app import tasks
from app.newapi_client import NewApiError

MODEL = "gemini-3.1-flash-image-preview"
MARKER_MSG = ("* ***.contents[0].parts[0].data: required oneof field 'data' "
              "must have one initialized field")
OK_RAW = {"candidates": [{"content": {"parts": [
    {"inlineData": {"mimeType": "image/jpeg", "data": "QQ=="}}]}}]}
BAD_200 = {"error": {"message": MARKER_MSG, "type": "rix_api_error",
                     "code": 400, "param": ""}}
IMG = "data:image/png;base64,AAAA"


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    async def _noop(_s):
        pass
    monkeypatch.setattr(asyncio, "sleep", _noop)


def _install(monkeypatch, behaviors):
    """behaviors: 每次调用依次弹出的行为（('exc', e) / ('raw', d)），耗尽后重复最后一项。"""
    calls = {"n": 0}
    logs: dict[str, dict] = {}

    async def fake_put(uid, request_id, kind, provider, model, params, status="submitted", mode="sync"):
        logs[request_id] = {"status": status, "result": None}
        return logs[request_id]

    async def fake_update(request_id, **fields):
        logs.setdefault(request_id, {}).update(fields)
        return logs[request_id]

    async def fake_gw(method, path, uid, *, json=None, params=None, client=None, pat=None):
        i = min(calls["n"], len(behaviors) - 1)
        calls["n"] += 1
        calls.setdefault("payloads", []).append(json)
        kind_, payload = behaviors[i]
        if kind_ == "exc":
            raise payload
        return payload

    async def fake_persist(task, task_id, uid, kind="image", request_id=None):
        return task

    monkeypatch.setattr(cloudstore, "request_log_put", fake_put)
    monkeypatch.setattr(cloudstore, "request_log_update", fake_update)
    monkeypatch.setattr(tasks, "_gw_call", fake_gw)
    monkeypatch.setattr(tasks, "_persist_outputs", fake_persist)
    monkeypatch.setattr(tasks, "_sync_client", lambda: None)
    return calls, logs


def _run(kind="image-gen"):
    return asyncio.run(tasks._run_sync(
        7, "req-retry", kind, "v1/images/generations",
        {"model": MODEL, "prompt": "p", "image": [IMG]}))


# ---------------------------------------------------------------------------
def test_retry_on_exception_then_success(monkeypatch):
    """形态 a：前两次抛 400 签名异常，第三次成功 → 最终 succeeded，共 3 次调用。"""
    calls, logs = _install(monkeypatch, [
        ("exc", NewApiError(MARKER_MSG, 400)),
        ("exc", NewApiError(MARKER_MSG, 400)),
        ("raw", OK_RAW),
    ])
    view = _run()
    assert calls["n"] == 3
    assert view["status"] == "succeeded"


def test_retry_on_200_error_body_then_success(monkeypatch):
    """形态 b：HTTP 200 + {error} 错误体（15:54 真实失败形态），重试后成功。"""
    calls, _ = _install(monkeypatch, [
        ("raw", BAD_200),
        ("raw", BAD_200),
        ("raw", OK_RAW),
    ])
    view = _run()
    assert calls["n"] == 3
    assert view["status"] == "succeeded"


def test_retry_exhausted_fails_with_original_error(monkeypatch):
    """三次全坏 → 落库 failed，调用次数 = 3（1 + 2 重试），不再无限打。"""
    calls, logs = _install(monkeypatch, [
        ("exc", NewApiError(MARKER_MSG, 400)),
    ])
    with pytest.raises(NewApiError):
        _run()
    assert calls["n"] == 3
    assert logs["req-retry"]["status"] == "failed"


def test_text_only_400_not_retried(monkeypatch):
    """文生图（无 inlineData）400 是真错误 → 立即抛出，只调 1 次。"""
    calls, _ = _install(monkeypatch, [
        ("exc", NewApiError(MARKER_MSG, 400)),
    ])
    with pytest.raises(NewApiError):
        asyncio.run(tasks._run_sync(
            7, "req-t2i", "image-gen", "v1/images/generations",
            {"model": MODEL, "prompt": "p"}))
    assert calls["n"] == 1


def test_other_400_with_image_not_retried(monkeypatch):
    """带图但消息不匹配签名（如 MIME/参数拒绝）→ 不重试。"""
    calls, _ = _install(monkeypatch, [
        ("exc", NewApiError("image mime not supported", 400)),
    ])
    with pytest.raises(NewApiError):
        _run()
    assert calls["n"] == 1


def test_non_400_status_not_retried(monkeypatch):
    """同签名但非 400（如 500）→ 不重试（避免放大故障流量）。"""
    calls, _ = _install(monkeypatch, [
        ("exc", NewApiError(MARKER_MSG, 500)),
    ])
    with pytest.raises(NewApiError):
        _run()
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# 辅助函数单测
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("msg,expected", [
    (MARKER_MSG, True),
    ("data: required oneof field \"data\" must have one initialized field", True),
    ("image mime not supported", False),
    ("insufficient_user_quota", False),
    ("", False),
    (None, False),
])
def test_marker_matching(msg, expected):
    assert tasks._is_gemini_inline_400_message(msg) is expected


# ---------------------------------------------------------------------------
# 空 prompt → 不发 text part（rix 对 {"text": ""} 必挂，9/28 对照实验实锤）
# ---------------------------------------------------------------------------
def test_empty_prompt_omits_text_part(monkeypatch):
    """参数型工具（移除背景等）prompt 为空 → parts 只有图，不发 {"text": ""}。"""
    calls, _logs = _install(monkeypatch, [("raw", OK_RAW)])
    asyncio.run(tasks._run_sync(
        7, "req-ep", "image-gen", "v1/images/generations",
        {"model": MODEL, "prompt": "", "image": [IMG]}))
    parts = calls["payloads"][0]["contents"][0]["parts"]
    assert parts == [{"inlineData": {"mimeType": "image/png", "data": "AAAA"}}]


def test_blank_prompt_omits_text_part(monkeypatch):
    calls, _logs = _install(monkeypatch, [("raw", OK_RAW)])
    asyncio.run(tasks._run_sync(
        7, "req-bp", "image-gen", "v1/images/generations",
        {"model": MODEL, "prompt": "   \n", "image": [IMG]}))
    parts = calls["payloads"][0]["contents"][0]["parts"]
    assert parts == [{"inlineData": {"mimeType": "image/png", "data": "AAAA"}}]


def test_nonempty_prompt_keeps_text_first(monkeypatch):
    calls, _logs = _install(monkeypatch, [("raw", OK_RAW)])
    asyncio.run(tasks._run_sync(
        7, "req-np", "image-gen", "v1/images/generations",
        {"model": MODEL, "prompt": "  去除背景  ", "image": [IMG]}))
    parts = calls["payloads"][0]["contents"][0]["parts"]
    assert parts[0] == {"text": "  去除背景  "}
    assert parts[1] == {"inlineData": {"mimeType": "image/png", "data": "AAAA"}}


def test_no_prompt_no_image_degenerate_keeps_empty_text(monkeypatch):
    """无图也无 prompt 的退化请求：维持旧形态（空 text），不空 contents。"""
    calls, _logs = _install(monkeypatch, [("raw", OK_RAW)])
    asyncio.run(tasks._run_sync(
        7, "req-dg", "image-gen", "v1/images/generations",
        {"model": MODEL}))
    parts = calls["payloads"][0]["contents"][0]["parts"]
    assert parts == [{"text": ""}]


# ---------------------------------------------------------------------------
# 蒙版编辑：mask 作为第二张图 inline 传入（2026-09-28 实锤此前 mask 被静默丢弃）
# ---------------------------------------------------------------------------
def test_mask_appended_as_second_image_part(monkeypatch):
    calls, _logs = _install(monkeypatch, [("raw", OK_RAW)])
    asyncio.run(tasks._run_sync(
        7, "req-mask", "image-gen", "v1/images/generations",
        {"model": MODEL, "prompt": "清除遮罩区域内的所有内容",
         "image": [IMG], "mask": "data:image/png;base64,BBBB"}))
    parts = calls["payloads"][0]["contents"][0]["parts"]
    # 原提示词已含「遮罩」→ 不追加说明；parts = text + 原图 + 蒙版
    assert parts[0] == {"text": "清除遮罩区域内的所有内容"}
    assert parts[1] == {"inlineData": {"mimeType": "image/png", "data": "AAAA"}}
    assert parts[2] == {"inlineData": {"mimeType": "image/png", "data": "BBBB"}}


def test_mask_with_empty_prompt_inserts_hint_text(monkeypatch):
    """prompt 为空 + 有 mask → 补蒙版说明 text part（非空，规避空 text 必挂坑）。"""
    calls, _logs = _install(monkeypatch, [("raw", OK_RAW)])
    asyncio.run(tasks._run_sync(
        7, "req-mask2", "image-gen", "v1/images/generations",
        {"model": MODEL, "prompt": "",
         "image": [IMG], "mask": "data:image/png;base64,BBBB"}))
    parts = calls["payloads"][0]["contents"][0]["parts"]
    assert "蒙版" in parts[0]["text"]
    assert parts[1] == {"inlineData": {"mimeType": "image/png", "data": "AAAA"}}
    assert parts[2] == {"inlineData": {"mimeType": "image/png", "data": "BBBB"}}


def test_no_mask_shape_unchanged(monkeypatch):
    calls, _logs = _install(monkeypatch, [("raw", OK_RAW)])
    asyncio.run(tasks._run_sync(
        7, "req-nm", "image-gen", "v1/images/generations",
        {"model": MODEL, "prompt": "p", "image": [IMG]}))
    parts = calls["payloads"][0]["contents"][0]["parts"]
    assert parts == [{"text": "p"},
                     {"inlineData": {"mimeType": "image/png", "data": "AAAA"}}]


# ---------------------------------------------------------------------------
# 参数型工具 task 语义注入（2026-09-28 实锤：空 prompt + 纯图零指令 → 原样重绘，
# 「移除背景没效果」；chatfire 弱指令「保持原样编辑」同理）
# ---------------------------------------------------------------------------
def test_remove_background_empty_prompt_injects_semantics(monkeypatch):
    calls, _logs = _install(monkeypatch, [("raw", OK_RAW)])
    asyncio.run(tasks._run_sync(
        7, "req-rb", "image-gen", "v1/images/generations",
        {"model": MODEL, "prompt": "", "task": "remove-background", "image": [IMG]}))
    parts = calls["payloads"][0]["contents"][0]["parts"]
    assert "移除" in parts[0]["text"] and "背景" in parts[0]["text"]
    assert parts[1] == {"inlineData": {"mimeType": "image/png", "data": "AAAA"}}


def test_upscale_empty_prompt_injects_semantics(monkeypatch):
    calls, _logs = _install(monkeypatch, [("raw", OK_RAW)])
    asyncio.run(tasks._run_sync(
        7, "req-up", "image-gen", "v1/images/generations",
        {"model": MODEL, "task": "upscale", "image": [IMG]}))
    parts = calls["payloads"][0]["contents"][0]["parts"]
    assert "放大" in parts[0]["text"]


def test_unknown_task_empty_prompt_stays_pure_image(monkeypatch):
    """未知 task / 无 task 的空 prompt 维持纯图零指令（不发明文）。"""
    calls, _logs = _install(monkeypatch, [("raw", OK_RAW)])
    asyncio.run(tasks._run_sync(
        7, "req-ut", "image-gen", "v1/images/generations",
        {"model": MODEL, "task": "mystery-op", "image": [IMG]}))
    parts = calls["payloads"][0]["contents"][0]["parts"]
    assert parts == [{"inlineData": {"mimeType": "image/png", "data": "AAAA"}}]


def test_semantic_prompt_not_overridden_by_user_prompt(monkeypatch):
    """用户自己写了 prompt → 语义注入不生效，原样透传。"""
    calls, _logs = _install(monkeypatch, [("raw", OK_RAW)])
    asyncio.run(tasks._run_sync(
        7, "req-keep", "image-gen", "v1/images/generations",
        {"model": MODEL, "prompt": "画一只猫", "task": "remove-background",
         "image": [IMG]}))
    parts = calls["payloads"][0]["contents"][0]["parts"]
    assert parts[0] == {"text": "画一只猫"}
