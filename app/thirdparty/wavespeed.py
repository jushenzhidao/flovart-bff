"""WaveSpeed AI 直连适配器（多角度 multi-angle / 多图一致性）。

API 形态（wavespeed.ai，官方文档为准）：
- 提交：POST {WAVESPEED_BASE_URL}/v3/{model}
        Header: Authorization: Bearer <WAVESPEED_API_KEY>，Content-Type: application/json
        Body:   {prompt, images:[public_url...], num_images?}
        → 成功返回 prediction id（位于 data.id / id / data.prediction_id / data.task_id 之一）
- 轮询：GET {WAVESPEED_BASE_URL}/v3/predictions/{prediction_id}/result
        → {data:{status, outputs}}  status: pending|processing|completed|failed|...
          outputs: 图片 URL（字符串数组 / {url} / {image_url} 混合，本文件做多形态归一化）
- 取消：DELETE {WAVESPEED_BASE_URL}/v3/predictions/{prediction_id}（尽力，忽略失败）

⚠️ 契约说明：WaveSpeed 不同模型返回字段略有差异，outputs 归一化做了多键容错；
   若实测发现字段名与本文不符，只改本文件的 _extract_outputs，勿动 BFF tasks.py。
"""
import logging
import os
from typing import Any

import httpx

from .. import cloudstore, config

logger = logging.getLogger("bff.thirdparty.wavespeed")

_CLIENT: "httpx.AsyncClient | None" = None


def _client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None:
        if not config.WAVESPEED_ENABLED:
            raise RuntimeError("WAVESPEED_API_KEY 未配置，无法直连 WaveSpeed")
        _CLIENT = httpx.AsyncClient(
            base_url=config.WAVESPEED_BASE_URL,
            timeout=httpx.Timeout(config.WAVESPEED_TIMEOUT, connect=10.0),
            headers={"Authorization": f"Bearer {config.WAVESPEED_API_KEY}"},
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
# 角度参数 → 自然语言机位描述（喂给 Kontext 多图上下文，保持主体一致性）
# ---------------------------------------------------------------------------
_ROTATE = {
    0: "front view",
    45: "three-quarter front view from the right",
    90: "right side view",
    135: "three-quarter rear view from the right",
    180: "back view",
    225: "three-quarter rear view from the left",
    270: "left side view",
    315: "three-quarter front view from the left",
}
_TILT = {
    -30: "shot from a low angle looking up",
    0: "eye-level angle",
    30: "slightly top-down angle",
    60: "top-down bird's-eye angle",
}
_SCALE = {"close-up": "close-up shot", "medium": "medium shot", "wide": "wide shot"}


def build_multiangle_prompt(rotate=0, tilt=0, scale="medium", extra_prompt="") -> str:
    """把 Lovart 风格的角度参数翻译成 WaveSpeed 能理解的英文机位提示词。"""
    parts = [
        _ROTATE.get(int(rotate), f"rotated {rotate} degrees view"),
        _TILT.get(int(tilt), f"tilted {tilt} degrees"),
        _SCALE.get(scale, "medium shot"),
        "of the same subject, keep identity, clothing, lighting and style consistent",
    ]
    if extra_prompt:
        parts.append(extra_prompt)
    return ", ".join(parts) + "."


async def _resolve_source_url(uid: int, source_media_key: str) -> str:
    """把 BFF 盘里的源图解析成 WaveSpeed 可公网拉取的 URL。

    - OSS 后端：用 presigned URL（prod 路径，有效期内可公网访问）。
    - 本地文件后端（dev）：读字节上传到 WaveSpeed 换 URL（需验证上传端点）；
      若上传不可用，抛错提示启用 OSS。
    """
    info = await cloudstore.media_get(uid, source_media_key)
    if not info:
        raise ValueError("源图不存在或无权访问")
    url = info.get("url")
    if url:
        return url
    path = info.get("path")
    if not path:
        raise ValueError("源图无可公网访问的 URL（请启用 OSS 或配置公网 media base）")
    return await _upload_local(path, info.get("mime") or "image/png")


# WaveSpeed 要求上传文件名带扩展名（.png/.jpg...），否则 400：
# "The uploaded file must have a file extension"。本地存储的源图路径通常无扩展名
# （如 data/media/<key>），故按 mime 推导扩展名补上。
_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/tiff": "tif",
    "image/svg+xml": "svg",
    "image/avif": "avif",
}


async def _upload_local(path: str, mime: str) -> str:
    """本地文件上传到 WaveSpeed 换取公网 URL（dev 仅本地存储时触发）。

    使用 WaveSpeed 官方 legacy 单步上传端点（相对 base_url）：POST /v3/media/upload/binary。
    prod 走 OSS presigned URL 不会触发此分支。
    """
    with open(path, "rb") as f:
        data = f.read()
    # 文件名校验：WaveSpeed 强制要求扩展名，本地源图路径无扩展名时按 mime 补。
    ext = _EXT_BY_MIME.get((mime or "").lower(), "png")
    base = os.path.basename(path) or "source"
    if "." not in base:
        base = f"{base}.{ext}"
    filename = base
    # 官方 legacy single-step endpoint：form-data file 字段。
    # 注意 WAVESPEED_BASE_URL 已是 https://api.wavespeed.ai/api，
    # 这里用相对路径 /v3/...（勿再加 /api，否则会变成 /api/api/... 404）。
    resp = await _client().post(
        "/v3/media/upload/binary",
        files={"file": (filename, data, mime or "image/png")},
        timeout=config.WAVESPEED_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"上传源图到 WaveSpeed 失败 HTTP {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    u = (body.get("data") or {}).get("download_url") or (body.get("data") or {}).get("url") or body.get("url")
    if not u:
        raise RuntimeError(f"WaveSpeed 上传未返回 URL: {resp.text[:200]}")
    return u


async def submit_multi_angle(uid: int, source_media_key: str, *, rotate=0, tilt=0,
                             scale="medium", extra_prompt="", model=None,
                             num_images=1) -> str:
    """提交多角度任务，返回 WaveSpeed prediction id。"""
    src_url = await _resolve_source_url(uid, source_media_key)
    prompt = build_multiangle_prompt(rotate, tilt, scale, extra_prompt)
    model = model or config.WAVESPEED_MULTIANGLE_MODEL
    # Kontext 系列用单图 image 字段；flux-2-turbo/edit 等用 images 数组。
    # 这里按官方文档明确的 Kontext 格式：image + prompt + num_images。
    body = {"prompt": prompt, "image": src_url, "num_images": int(num_images)}
    resp = await _client().post(f"/v3/{model}", json=body)
    if resp.status_code >= 400:
        logger.error("WaveSpeed submit failed %s: %s", resp.status_code, resp.text[:500])
        raise RuntimeError(f"WaveSpeed 提交失败 HTTP {resp.status_code}")
    j = resp.json()
    if isinstance(j, dict) and j.get("success") is False:
        raise RuntimeError(f"WaveSpeed 提交被拒: {j.get('message')}")
    data = (j.get("data") if isinstance(j, dict) else None) or j
    pred_id = None
    if isinstance(data, dict):
        pred_id = data.get("id") or data.get("prediction_id") or data.get("task_id")
    if not pred_id and isinstance(j, dict):
        pred_id = j.get("id") or j.get("prediction_id") or j.get("task_id")
    if not pred_id:
        raise RuntimeError(f"WaveSpeed 未返回 prediction id: {resp.text[:300]}")
    return str(pred_id)


def build_split_layers_body(model: str, src_url: str, *, num_layers=4,
                            prompt="", resolution=None) -> "dict[str, Any]":
    """按模型构造分层提交体（纯函数，便于单测覆盖两种参数形态）。"""
    if "layer-decomposition" in model or "seedream" in model:
        # Seedream V5.0 Pro Layer Decomposition：不吃 num_layers；
        # ⚠️ output_format 必须 png（默认 jpeg 会丢透明通道）。
        body: "dict[str, Any]" = {
            "image": src_url,
            "resolution": (resolution or config.WAVESPEED_SPLIT_PRO_RESOLUTION or "1k").lower(),
            "output_format": "png",
        }
    else:
        # qwen-image/layered：num_layers（2~8）控制层数
        body = {"image": src_url, "num_layers": int(num_layers)}
    if prompt:
        body["prompt"] = prompt
    return body


async def submit_split_layers(uid: int, source_media_key: str, *, num_layers=4,
                              prompt="", model=None, resolution=None) -> str:
    """提交分层（图层分解）任务，返回 WaveSpeed prediction id。

    双模型按 model 分支（2026-09-21 接入 Seedream Pro 分层，仅管理员）：
    - 默认 wavespeed-ai/qwen-image/layered：num_layers（2~8）控制层数，prompt 可选引导语义分组。
    - bytedance/seedream-v5.0-pro/layer-decomposition：prompt 直接描述要拆的层/分组
      （不传则模型自动识别元素）；resolution 1k/1.5k/2k。
    源图复用 _resolve_source_url（本地存储自动上传换公网 URL）。
    """
    src_url = await _resolve_source_url(uid, source_media_key)
    model = model or config.WAVESPEED_SPLIT_MODEL
    body = build_split_layers_body(model, src_url, num_layers=num_layers,
                                   prompt=prompt, resolution=resolution)
    resp = await _client().post(f"/v3/{model}", json=body)
    if resp.status_code >= 400:
        logger.error("WaveSpeed split submit failed %s: %s", resp.status_code, resp.text[:500])
        raise RuntimeError(f"WaveSpeed 分层提交失败 HTTP {resp.status_code}")
    j = resp.json()
    if isinstance(j, dict) and j.get("success") is False:
        raise RuntimeError(f"WaveSpeed 分层提交被拒: {j.get('message')}")
    data = (j.get("data") if isinstance(j, dict) else None) or j
    pred_id = None
    if isinstance(data, dict):
        pred_id = data.get("id") or data.get("prediction_id") or data.get("task_id")
    if not pred_id and isinstance(j, dict):
        pred_id = j.get("id") or j.get("prediction_id") or j.get("task_id")
    if not pred_id:
        raise RuntimeError(f"WaveSpeed 分层未返回 prediction id: {resp.text[:300]}")
    return str(pred_id)


async def get_status(prediction_id: str) -> "tuple[str, list[str]]":
    """轮询结果，返回 (status, image_urls)。"""
    resp = await _client().get(f"/v3/predictions/{prediction_id}/result")
    if resp.status_code >= 400:
        logger.warning("WaveSpeed poll failed %s: %s", resp.status_code, resp.text[:300])
        return "failed", []
    body = resp.json()
    data = (body.get("data") if isinstance(body, dict) else None) or body
    status = str((data.get("status") if isinstance(data, dict) else "") or "").lower()
    outputs = _extract_outputs(data.get("outputs") if isinstance(data, dict) else None)
    return status, outputs


async def cancel(prediction_id: str) -> None:
    """尽力取消（第三方不支持则忽略）。"""
    try:
        await _client().delete(f"/v3/predictions/{prediction_id}")
    except Exception as e:  # noqa: BLE001
        logger.warning("WaveSpeed 取消失败(忽略): %s", e)


def _extract_outputs(raw: Any) -> "list[str]":
    """把 WaveSpeed outputs 多形态归一化为图片 URL 字符串列表。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        urls: "list[str]" = []
        for it in raw:
            if isinstance(it, str):
                urls.append(it)
            elif isinstance(it, dict):
                u = (it.get("url") or it.get("image_url") or it.get("output")
                     or it.get("image") or it.get("imageUrl"))
                if isinstance(u, str):
                    urls.append(u)
        return urls
    if isinstance(raw, dict):
        for k in ("images", "outputs", "urls"):
            v = raw.get(k)
            if isinstance(v, list):
                return _extract_outputs(v)
        u = (raw.get("url") or raw.get("image_url") or raw.get("image"))
        return [u] if isinstance(u, str) else []
    return []
