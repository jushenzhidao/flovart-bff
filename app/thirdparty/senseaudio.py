"""SenseAudio TTS 直连适配器（商汤，MiniMax t2a_v2 协议形态）。

API 形态（docs.senseaudio.cn，官方文档为准）：
- 合成：POST {SENSEAUDIO_BASE_URL}/v1/t2a_v2
        Header: Authorization: Bearer <SENSEAUDIO_API_KEY>，Content-Type: application/json
        Body:   {model, text, stream:false, voice_setting:{voice_id, speed, vol, pitch},
                 audio_setting:{format, sample_rate, bitrate, channel}}
        → 200 {data:{audio:<string>}, extra_info:{audio_length, usage_characters, ...},
               trace_id, base_resp:{status_code, status_msg}}
- 音色：POST /v1/get_voice（路由存在性已 2026-10-20 探测：无凭据 401 JSON，对照路径 404）
        响应结构文档未公开细节，按 MiniMax 同构（system_voice / voice_cloning 数组）
        做多键容错解析，拿不到就抛错（前端回退内置常用音色清单）。

⚠️ 协议说明（本文件是唯一对接点，tasks.py 不感知上游细节）：
- 模型就 2 个：sensenova-tts-2.0（推荐）/ senseaudio-tts-1.5-260319；
- 计费按合成字符数；音色可用范围跟上游账号套餐走 —— **音色清单禁止写死**，运行时拉取；
- data.audio 的编码文档只写 "<string>"：MiniMax 同构是 hex，这里 hex→base64 双探测
  （按魔数校验解码结果，防止假成功）；
- 参数边界（文档错误表）：speed 0.5–2.0 / vol 0–10 / pitch -12–12 /
  sample_rate ∈ {8000,16000,22050,24000,32000,44100} / text ≤10000 字符。
"""
import base64
import binascii
import logging
import time
from typing import Any

import httpx

from .. import config

logger = logging.getLogger("bff.thirdparty.senseaudio")

_CLIENT: "httpx.AsyncClient | None" = None


def _client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None:
        if not config.SENSEAUDIO_ENABLED:
            raise RuntimeError("SENSEAUDIO_API_KEY 未配置，TTS 能力不可用")
        _CLIENT = httpx.AsyncClient(
            base_url=config.SENSEAUDIO_BASE_URL,
            timeout=httpx.Timeout(config.SENSEAUDIO_TIMEOUT, connect=10.0),
            headers={"Authorization": f"Bearer {config.SENSEAUDIO_API_KEY}"},
            trust_env=False,  # 不走本机代理
        )
    return _CLIENT


async def close() -> None:
    """lifespan 关闭时归还连接池。"""
    global _CLIENT
    if _CLIENT is not None:
        await _CLIENT.aclose()
        _CLIENT = None


# ---------------------------------------------------------------------------
# 参数校验/归一化（纯函数，便于单测）
# ---------------------------------------------------------------------------
_ALLOWED_SAMPLE_RATES = {8000, 16000, 22050, 24000, 32000, 44100}
_ALLOWED_MODELS = {"sensenova-tts-2.0", "senseaudio-tts-1.5-260319"}


def build_tts_body(params: dict) -> dict:
    """前端语义参数 → 上游请求体。越界值在这里早失败（省一次上游按字符计费的调用）。"""
    text = str(params.get("text") or "").strip()
    if not text:
        raise ValueError("合成文本不能为空")
    if len(text) > config.SENSEAUDIO_MAX_TEXT_CHARS:
        raise ValueError(f"合成文本超过 {config.SENSEAUDIO_MAX_TEXT_CHARS} 字符上限（当前 {len(text)}）")
    voice_id = str(params.get("voiceId") or params.get("voice_id") or "").strip()
    if not voice_id:
        raise ValueError("必须指定音色 voiceId")

    model = str(params.get("model") or config.SENSEAUDIO_TTS_MODEL).strip()
    if model not in _ALLOWED_MODELS:
        raise ValueError(f"不支持的 TTS 模型: {model}")

    speed = float(params.get("speed") if params.get("speed") is not None else 1)
    if not 0.5 <= speed <= 2.0:
        raise ValueError("语速 speed 必须在 0.5–2.0 之间")
    vol = float(params.get("vol") if params.get("vol") is not None else 1)
    if not 0 <= vol <= 10:
        raise ValueError("音量 vol 必须在 0–10 之间")
    pitch = float(params.get("pitch") if params.get("pitch") is not None else 0)
    if not -12 <= pitch <= 12:
        raise ValueError("音调 pitch 必须在 -12–12 之间")
    # 上游 Go 结构体 voice_setting.pitch 只收 int（传 0.0 会整包反序列化失败，
    # 且 status_code 仍为 0、错误藏在 status_msg —— 2026-10-20 实测血案），必须取整。
    pitch_int = int(round(pitch))

    sample_rate = int(params.get("sampleRate") or 32000)
    if sample_rate not in _ALLOWED_SAMPLE_RATES:
        raise ValueError(f"采样率 {sample_rate} 不支持，可选: {sorted(_ALLOWED_SAMPLE_RATES)}")

    return {
        "model": model,
        "text": text,
        "stream": False,
        "voice_setting": {"voice_id": voice_id, "speed": speed, "vol": vol, "pitch": pitch_int},
        "audio_setting": {
            "format": "mp3",
            "sample_rate": sample_rate,
            "bitrate": 128000,
            "channel": 1,
        },
    }


def _looks_like_mp3(data: bytes) -> bool:
    return data[:3] == b"ID3" or (len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)


def _looks_like_ogg(data: bytes) -> bool:
    return data[:4] == b"OggS"


def decode_audio_payload(raw: str) -> bytes:
    """data.audio 字符串 → 音频字节。hex（MiniMax 同构形态）与 base64 双探测。

    判据：解码结果必须带已知音频魔数，否则视为「解码形态猜错」，换下一种；
    两种都失败才报错 —— 防止把密文垃圾当成功（静默坏产物是最坏的失败形态）。
    """
    s = (raw or "").strip()
    if not s:
        raise RuntimeError("上游未返回音频内容")
    # 偶发带 data: 前缀的 base64 形态
    if s.startswith("data:"):
        _, _, s = s.partition(",")
    candidates = []
    try:
        candidates.append(bytes.fromhex(s))
    except ValueError:
        pass
    try:
        candidates.append(base64.b64decode(s, validate=True))
    except (binascii.Error, ValueError):
        pass
    for data in candidates:
        if _looks_like_mp3(data) or _looks_like_ogg(data):
            return data
    if candidates:
        # 魔数没认出来但确实解出了非空字节 —— 上游可能换了封装，取 hex 优先结果并告警。
        logger.warning("TTS 音频魔数未识别（首字节 %s），按 hex 解码结果放行", candidates[0][:4].hex())
        return candidates[0]
    raise RuntimeError("上游音频编码无法解码（hex/base64 均失败）")


def _raise_upstream_error(payload: dict, http_status: int) -> None:
    base_resp = payload.get("base_resp") or {}
    code = base_resp.get("status_code")
    msg = base_resp.get("status_msg") or f"HTTP {http_status}"
    raise RuntimeError(f"TTS 合成失败（{code or http_status}）: {msg}")


async def synthesize(params: dict) -> dict:
    """同步合成。返回 {audio: bytes, mime, model, voiceId, durationMs, usageChars}。"""
    body = build_tts_body(params)
    voice_id = body["voice_setting"]["voice_id"]
    model = body["model"]
    resp = await _client().post("/v1/t2a_v2", json=body)
    try:
        payload = resp.json()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"TTS 上游返回非 JSON（HTTP {resp.status_code}）: {resp.text[:200]}") from e
    if resp.status_code >= 400:
        _raise_upstream_error(payload if isinstance(payload, dict) else {}, resp.status_code)
    if not isinstance(payload, dict):
        raise RuntimeError("TTS 上游响应结构异常")
    # 业务错误可能包 200 信封里（base_resp.status_code != 0）
    base_resp = payload.get("base_resp") or {}
    if base_resp.get("status_code") not in (None, 0):
        _raise_upstream_error(payload, resp.status_code)
    data = payload.get("data") or {}
    raw_audio = data.get("audio") or data.get("data") or ""
    if not str(raw_audio).strip():
        # 该上游失败时 status_code 也可能仍是 0、错误只写在 status_msg（实测）。
        msg = str(base_resp.get("status_msg") or "").strip()
        if msg and msg.lower() != "success":
            raise RuntimeError(f"TTS 合成失败: {msg}")
        raise RuntimeError("上游未返回音频内容")
    audio = decode_audio_payload(str(raw_audio))
    extra = payload.get("extra_info") or {}
    return {
        "audio": audio,
        "mime": "audio/mpeg",
        "model": model,
        "voiceId": voice_id,
        "durationMs": int(extra.get("audio_length") or 0),
        "usageChars": int(extra.get("usage_characters") or len(body["text"])),
    }


# ---------------------------------------------------------------------------
# 音色清单（运行时拉取，禁止写死 —— 可用范围跟上游账号套餐走）
# ---------------------------------------------------------------------------
_VOICE_CACHE: "dict[str, tuple[float, list[dict]]]" = {}
_VOICE_CACHE_TTL = 600  # 秒


def _normalize_voice(entry: dict, *, cloned: bool = False) -> "dict | None":
    vid = str(entry.get("voice_id") or entry.get("voiceId") or entry.get("id") or "").strip()
    if not vid:
        return None
    # 实测（2026-10-20）description 可能是数组（如 ["成熟女声，……"]），直接 str() 会渲染成 "['…']"。
    raw_desc = entry.get("description") or entry.get("desc") or ""
    if isinstance(raw_desc, (list, tuple)):
        raw_desc = "，".join(str(part).strip() for part in raw_desc if str(part).strip())
    raw_desc = str(raw_desc).strip()
    # name 优先 voice_name（如「温柔御姐」），长描述降级为 emotion 备用信息。
    name = str(entry.get("voice_name") or entry.get("name") or "").strip() or raw_desc or vid
    emotion = str(entry.get("emotion") or "").strip() or (raw_desc if raw_desc and raw_desc != name else "") or None
    return {
        "voiceId": vid,
        "name": name,
        "emotion": emotion,
        "cloned": cloned,
    }


def parse_voices_payload(payload: dict) -> list[dict]:
    """get_voice 响应 → [{voiceId, name, emotion, cloned}]，多键容错。

    MiniMax 同构形态：{system_voice:[...], voice_cloning:[...], ...}；
    若上游改了字段名，退而求其次扫全部数组字段。
    """
    if not isinstance(payload, dict):
        return []
    entries: list[dict] = []
    for key in ("system_voice", "systemVoice", "voices"):
        value = payload.get(key)
        if isinstance(value, list):
            entries.extend((v, False) for v in value if isinstance(v, dict))
    for key in ("voice_cloning", "voiceCloning"):  # 克隆/自定义音色单独标 cloned
        value = payload.get(key)
        if isinstance(value, list):
            entries.extend((v, True) for v in value if isinstance(v, dict))
    if not entries:  # 兜底：扫第一层所有数组字段（无法区分克隆，默认系统音色）
        for value in payload.values():
            if isinstance(value, list):
                entries.extend((v, False) for v in value if isinstance(v, dict) and (v.get("voice_id") or v.get("voiceId")))
    out, seen = [], set()
    for entry, cloned in entries:
        item = _normalize_voice(entry, cloned=cloned)
        if item and item["voiceId"] not in seen:
            seen.add(item["voiceId"])
            out.append(item)
    return out


async def list_voices(force: bool = False) -> list[dict]:
    """拉当前账号可用音色（进程内缓存 TTL 10 分钟，避免每次开弹窗都打上游）。"""
    cache_key = config.SENSEAUDIO_TTS_MODEL
    now = time.monotonic()
    if not force:
        cached = _VOICE_CACHE.get(cache_key)
        if cached and now - cached[0] < _VOICE_CACHE_TTL:
            return cached[1]
    # voice_type 必填（缺失报 field "voice_type" is not set）；all/system 合法，
    # voice_cloning 该上游报 invalid voice type。账号无可用音色时 data 为空数组。
    resp = await _client().post("/v1/get_voice", json={"model": config.SENSEAUDIO_TTS_MODEL, "voice_type": "all"})
    try:
        payload = resp.json()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"TTS 音色接口返回非 JSON（HTTP {resp.status_code}）") from e
    if resp.status_code >= 400:
        _raise_upstream_error(payload if isinstance(payload, dict) else {}, resp.status_code)
    base_resp = (payload or {}).get("base_resp") or {}
    if isinstance(payload, dict) and base_resp.get("status_code") not in (None, 0):
        _raise_upstream_error(payload, resp.status_code)
    data = payload.get("data") if isinstance(payload, dict) else None
    voices = parse_voices_payload(data if isinstance(data, dict) else (payload or {}))
    if voices:
        _VOICE_CACHE[cache_key] = (now, voices)
    else:
        # 实测 get_voice 偶发 200+空清单（status_code 仍是 0/success，几分钟后又正常）——
        # 空结果不是有效数据：不覆盖缓存，回退上次拿到的清单（哪怕已过 TTL）。
        logger.warning("SenseAudio get_voice 返回空清单（model=%s），回退上次缓存", cache_key)
        stale = _VOICE_CACHE.get(cache_key)
        if stale:
            return stale[1]
    return voices
