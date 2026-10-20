"""SenseAudio 直连适配器单测（纯函数：请求体构造 / 音频解码 / 音色解析）。"""
import pytest

from app.thirdparty import senseaudio


# ---------------------------------------------------------------------------
# build_tts_body
# ---------------------------------------------------------------------------
def test_build_tts_body_defaults():
    body = senseaudio.build_tts_body({"text": "你好", "voiceId": "female_0033_b"})
    assert body["model"] == "sensenova-tts-2.0"
    assert body["stream"] is False
    assert body["voice_setting"] == {"voice_id": "female_0033_b", "speed": 1, "vol": 1, "pitch": 0}
    assert body["audio_setting"]["format"] == "mp3"
    assert body["audio_setting"]["sample_rate"] == 32000


def test_build_tts_body_rejects_empty_text():
    with pytest.raises(ValueError, match="不能为空"):
        senseaudio.build_tts_body({"text": "  ", "voiceId": "x"})


def test_build_tts_body_rejects_missing_voice():
    with pytest.raises(ValueError, match="voiceId"):
        senseaudio.build_tts_body({"text": "你好"})


def test_build_tts_body_rejects_unknown_model():
    with pytest.raises(ValueError, match="不支持的 TTS 模型"):
        senseaudio.build_tts_body({"text": "hi", "voiceId": "v", "model": "gpt-tts"})


def test_build_tts_body_rejects_out_of_range():
    with pytest.raises(ValueError, match="speed"):
        senseaudio.build_tts_body({"text": "hi", "voiceId": "v", "speed": 3})
    with pytest.raises(ValueError, match="vol"):
        senseaudio.build_tts_body({"text": "hi", "voiceId": "v", "vol": 11})
    with pytest.raises(ValueError, match="pitch"):
        senseaudio.build_tts_body({"text": "hi", "voiceId": "v", "pitch": 20})
    with pytest.raises(ValueError, match="采样率"):
        senseaudio.build_tts_body({"text": "hi", "voiceId": "v", "sampleRate": 48000})


# ---------------------------------------------------------------------------
# decode_audio_payload
# ---------------------------------------------------------------------------
def test_decode_audio_payload_hex_mp3():
    raw = b"ID3\x03\x00hello-audio"
    data = senseaudio.decode_audio_payload(raw.hex())
    assert data == raw


def test_decode_audio_payload_base64_mp3():
    raw = b"\xff\xfb\x90\x00frame-data"
    import base64
    data = senseaudio.decode_audio_payload(base64.b64encode(raw).decode())
    assert data == raw


def test_decode_audio_payload_data_url_prefix():
    raw = b"ID3\x03"
    import base64
    payload = "data:audio/mpeg;base64," + base64.b64encode(raw).decode()
    assert senseaudio.decode_audio_payload(payload) == raw


def test_decode_audio_payload_rejects_empty():
    with pytest.raises(RuntimeError, match="未返回音频"):
        senseaudio.decode_audio_payload("")


def test_decode_audio_payload_rejects_garbage():
    # 既不是合法 hex 也不是合法 base64
    with pytest.raises(RuntimeError, match="无法解码"):
        senseaudio.decode_audio_payload("!!!不是编码!!!")


# ---------------------------------------------------------------------------
# parse_voices_payload
# ---------------------------------------------------------------------------
def test_parse_voices_minimax_shape():
    payload = {
        "system_voice": [
            {"voice_id": "female_0033_b", "description": "嗲嗲台妹"},
            {"voice_id": "male_0004_a", "description": "儒雅道长", "emotion": "平稳"},
        ],
        "voice_cloning": [{"voice_id": "clone_1", "description": "我的克隆"}],
    }
    voices = senseaudio.parse_voices_payload(payload)
    assert [v["voiceId"] for v in voices] == ["female_0033_b", "male_0004_a", "clone_1"]
    assert voices[2]["cloned"] is True and voices[0]["cloned"] is False


def test_parse_voices_dedup_and_skip_invalid():
    payload = {"system_voice": [
        {"voice_id": "a", "name": "A"},
        {"voice_id": "a", "name": "A2"},
        {"name": "无 id"},
    ]}
    assert [v["voiceId"] for v in senseaudio.parse_voices_payload(payload)] == ["a"]


def test_parse_voices_empty():
    assert senseaudio.parse_voices_payload({}) == []
    assert senseaudio.parse_voices_payload(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 任务系统接线（闸门 + 路由挂载）
# ---------------------------------------------------------------------------
def test_tts_submit_gate_disabled(monkeypatch):
    """SENSEAUDIO_ENABLED=false 时提交必须 503 早失败（不落日志、不打上游）。"""
    import asyncio

    from app import config as cfg
    from app import tasks

    monkeypatch.setattr(cfg, "SENSEAUDIO_ENABLED", False)
    spec = {"provider": "thirdparty", "tp": "senseaudio", "mode": "sync"}
    with pytest.raises(Exception) as ei:
        asyncio.run(tasks._submit_thirdparty_sync(
            1, "req_test", "tts", spec, {"text": "hi", "voiceId": "v"}))
    assert "未启用" in str(ei.value)


def test_tts_routes_mounted():
    from app.main import app

    # logfire 会把 include 的子路由包成 _IncludedRouter（.original_router 才是真路由）。
    def collect(router):
        out = set()
        for r in router.routes:
            original = getattr(r, "original_router", None)
            if original is not None:
                out |= collect(original)
                continue
            path = getattr(r, "path", None)
            if path:
                out.add(path)
            sub = getattr(r, "routes", None)
            if sub:
                out |= collect(r)
        return out

    paths = collect(app.router)
    assert "/api/tts/voices" in paths
    assert "/api/tasks" in paths


def test_build_tts_body_pitch_is_int():
    """上游 Go 结构体 pitch 只收 int，浮点必须取整（2026-10-20 实测 0.0 也会炸）。"""
    body = senseaudio.build_tts_body({"text": "hi", "voiceId": "v", "pitch": 1.5})
    assert isinstance(body["voice_setting"]["pitch"], int)
    assert body["voice_setting"]["pitch"] == 2
    body = senseaudio.build_tts_body({"text": "hi", "voiceId": "v"})
    assert isinstance(body["voice_setting"]["pitch"], int)


def test_tts_preview_route_mounted():
    from app.main import app

    def collect(router):
        out = set()
        for r in router.routes:
            original = getattr(r, "original_router", None)
            if original is not None:
                out |= collect(original)
                continue
            path = getattr(r, "path", None)
            if path:
                out.add(path)
            sub = getattr(r, "routes", None)
            if sub:
                out |= collect(r)
        return out

    assert "/api/tts/preview" in collect(app.router)
