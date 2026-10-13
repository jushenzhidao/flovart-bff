# -*- coding: utf-8 -*-
"""网关视频模型可用性实测（用 uid=1 已存 sk-，不碰管理员通道）。

用法:
  只列模型（零成本）:  python scripts/gateway_video_probe.py
  真实发一次最小 t2v:  python scripts/gateway_video_probe.py --fire

- sk- 从 data/user_keys.json 解密获得，绝不打印
- 失败一次立即停手（不重试，遵守限频铁律）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config
from app.security import decrypt_secret
from app import user_keys
import urllib.request


def get_sk() -> str:
    sk = user_keys.get_key(1)
    if not sk:
        print("[STOP] user_keys 里没有 uid=1 的 sk-，无法测试")
        sys.exit(1)
    return sk


def call(base: str, path: str, sk: str, payload=None):
    req = urllib.request.Request(base.rstrip("/") + path)
    req.add_header("Authorization", f"Bearer {sk}")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(payload).encode()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main() -> None:
    fire = "--fire" in sys.argv
    sk = get_sk()
    base = config.NEWAPI_BASE_URL
    print(f"网关: {base}")

    status, body = call(base, "/v1/models", sk)
    if status != 200:
        print(f"[STOP] GET /v1/models -> HTTP {status}: {body[:300]}")
        return
    ids = sorted(m.get("id", "") for m in json.loads(body).get("data", []))
    video_models = [i for i in ids if any(
        h in i.lower() for h in ("seedance", "video", "kling", "vidu", "hailuo", "cogvideo", "sora", "veo", "wan"))]
    print(f"该 key 可见模型 {len(ids)} 个，其中视频类 {len(video_models)} 个:")
    for i in video_models:
        print("  -", i)
    ok = "doubao-seedance-2-0-260128" in ids
    print(">> 目标模型 doubao-seedance-2-0-260128:", "在列表中 ✔" if ok else "不在列表中 ✘（分组未覆盖）")
    if not fire:
        return

    target = "doubao-seedance-2-0-260128" if ok else next(
        (i for i in video_models if "seedance" in i and "t2v" in i), None) or (video_models[0] if video_models else None)
    if not target:
        print("[STOP] 该 key 下没有任何可见视频模型，无法实测")
        return
    print(f">> 实测模型: {target}（480p / 4s 最小参数，会烧真实额度）")
    payload = {
        "model": target,
        "prompt": "一只白色兔子在草地上蹦蹦跳跳",
        "resolution": "480p",
        "duration": 4,
        "aspect_ratio": "16:9",
    }
    status, body = call(base, "/v1/video/generations", sk, payload)
    print(f"POST /v1/video/generations -> HTTP {status}")
    print(body[:800])


if __name__ == "__main__":
    main()
