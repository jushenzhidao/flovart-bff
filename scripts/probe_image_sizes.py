#!/usr/bin/env python
"""探测网关生图渠道实际接受的 size 档位。

背景（2026-10-15 生产报错）：前端把 4K/4:3 映射成 3312x2480 传给 gpt-image-2.5-flare，
上游 400 "The image request is invalid"。本脚本对指定模型逐个 size 真实生一张小图，
输出「接受 / 拒绝」表，供档位收敛（BFF clamp / 前端 capability）用。

用法（在 flovart-bff 目录）：
  python scripts/probe_image_sizes.py --model gpt-image-2.5-flare \
      --sizes auto,1024x1024,1536x1024,1024x1536,1344x1008,1792x1024,2048x1536,3312x2480
⚠️ 每个 size 真烧一次额度（一张小图）。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_gateway_image_models import load_env, http, pick_token  # noqa: E402


def probe_size(base: str, key: str, model: str, size: str, quality: str | None = None) -> tuple[str, str]:
    """返回 (状态, 详情)。状态：ok / reject / error"""
    body: dict = {"model": model, "prompt": "a single red apple on white background, photo",
                  "n": 1, "size": size}
    if quality:
        body["quality"] = quality
    code, d = http(
        f"{base}/v1/images/generations", key=key, body=body,
        method="POST", timeout=300,
    )
    detail = json.dumps(d, ensure_ascii=False)[:160] if isinstance(d, (dict, list)) else str(d)[:160]
    if code != 200:
        return "reject", f"HTTP {code}: {detail}"
    if isinstance(d, dict):
        data = d.get("data") or []
        if data and (data[0].get("url") or data[0].get("b64_json")):
            return "ok", f"产出 {'url' if data[0].get('url') else 'b64'}"
        return "error", f"200 但无产出: {detail}"
    return "error", f"非 JSON: {detail}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--sizes", required=True, help="逗号分隔的 size 列表")
    ap.add_argument("--quality", default=None, help="可选：随每个 size 请求附带 quality 参数（如 high/medium/low）")
    a = ap.parse_args()

    env = load_env()
    base = env["NEWAPI_BASE_URL"].rstrip("/")
    key, tok_name = pick_token(base, env["NEWAPI_ADMIN_PAT"], env["NEWAPI_ADMIN_UID"])
    print(f"[探测] model={a.model} sizes={a.sizes}\n")

    accepted, rejected, errored = [], [], []
    for i, size in enumerate([s.strip() for s in a.sizes.split(",") if s.strip()], 1):
        t0 = time.time()
        status, detail = probe_size(base, key, a.model, size, quality=a.quality)
        mark = {"ok": "✅", "reject": "❌", "error": "⚠️"}[status]
        (accepted if status == "ok" else rejected if status == "reject" else errored).append(size)
        print(f"  ({i}) {mark} size={size} -> {detail}  [{time.time()-t0:.0f}s]")
        sys.stdout.flush()

    print("\n===== 结论 =====")
    print("✅ 接受:", ", ".join(accepted) if accepted else "(无)")
    print("❌ 拒绝:", ", ".join(rejected) if rejected else "(无)")
    if errored:
        print("⚠️ 异常:", ", ".join(errored))


if __name__ == "__main__":
    main()
