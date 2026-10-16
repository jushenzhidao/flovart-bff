#!/usr/bin/env python
"""探测 new-api 网关上真实可用的生图模型。

流程：.env 读取管理员访问令牌 → /api/token/ 找一个启用的 sk- 令牌（key 不回显）
→ /v1/models 拉全量列表按关键词筛生图候选 → 逐个 POST /v1/images/generations 真实生图
→ 三态判定：✅ 真可用（有 data[0].url/b64_json）/ ⚠️ 200但空产出 / ❌ 调用失败。

用法（在 flovart-bff 目录）：
  C:/Users/81068/.workbuddy/binaries/python/envs/flovart-bff/Scripts/python.exe \
      scripts/probe_gateway_image_models.py            # 列候选+逐个实测
  ... --list-only                                      # 只筛候选不烧额度
  ... --probe <模型名>                                  # 只测指定模型
⚠️ 每个候选都会真烧一次额度（一张小图）。--probe-grep 控制子集。
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 生图模型关键词（new-api 站点上 chat/embedding 等会被这些词误伤的概率低）
IMG_HINTS = (
    "image", "dall-e", "dalle", "flux", "seedream", "seededit", "banana",
    "cogview", "kontext", "wanx", "stable", "sd3", "sdxl",
    "hidream", "gpt-image", "qwen-image", "kolors", "photon",
)
# 排除明显不是文生图的
IMG_EXCLUDE = ("embedding", "whisper", "tts", "rerank", "moderation", "video")


def load_env() -> dict:
    env = {}
    path = os.path.join(ROOT, ".env")
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def http(url: str, *, key: str = "", uid: str = "", body: dict | None = None,
         method: str = "GET", timeout: int = 60) -> tuple[int, dict | bytes]:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if uid:
        headers["New-Api-User"] = uid
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        raw = e.read() or b""
        code = e.code
    try:
        return code, json.loads(raw)
    except Exception:
        return code, raw


def pick_token(base: str, pat: str, uid: str) -> tuple[str, str]:
    """从管理员令牌列表挑一个启用的，返回 (sk_key, 名称)。key 不打印。"""
    code, d = http(f"{base}/api/token/?p=1&size=100", key=pat, uid=uid)
    if code != 200:
        print(f"❌ 列令牌失败 HTTP {code}: {str(d)[:200]}")
        sys.exit(1)
    items = (d.get("data") or {}).get("items") if isinstance(d.get("data"), dict) else d.get("data")
    if not items:
        print("❌ 管理员名下没有任何令牌")
        sys.exit(1)
    print(f"[令牌列表] 共 {len(items)} 个：")
    usable = []
    for it in items:
        status = it.get("status")
        name = it.get("name", "?")
        unlimited = it.get("unlimited_quota")
        remain = it.get("remain_quota")
        limits = (it.get("model_limits") or "").strip()
        flag = "启用" if status == 1 else f"停用({status})"
        print(f"  - {name} | {flag} | {'无限额' if unlimited else f'余量 {remain}'}"
              f"{' | 限模型:' + limits[:60] if limits else ''}")
        if status == 1 and not limits:
            usable.append((name, it.get("id")))
    if not usable:
        print("❌ 没有启用的、不限模型清单的令牌")
        sys.exit(1)
    # 优先 BFF 专用 platform 令牌（无限额、与业务同语义），别拿余额小的私人令牌烧
    usable.sort(key=lambda x: 0 if x[0].startswith("flovart-bff") else 1)
    name, tid = usable[0]
    code, d = http(f"{base}/api/token/{tid}/key", key=pat, uid=uid, method="POST")
    if code != 200:
        print(f"❌ 取令牌明文失败 HTTP {code}: {str(d)[:200]}")
        sys.exit(1)
    data = d.get("data")
    if isinstance(data, dict):  # 新版返回 {"data": {"key": "sk-..."}}
        key = data.get("key") or ""
    elif isinstance(data, str):
        key = data
    else:
        key = ""
    if not key:
        print(f"❌ 响应里没有 key 字段（结构: dict keys={list(d.keys()) if isinstance(d, dict) else type(d)}）")
        sys.exit(1)
    if not key.startswith("sk-"):
        key = "sk-" + key
    print(f"[选用令牌] {name}\n")
    return key, name


def list_models(base: str, key: str) -> list[str]:
    code, d = http(f"{base}/v1/models", key=key, timeout=30)
    if code != 200:
        print(f"❌ /v1/models HTTP {code}: {str(d)[:200]}")
        sys.exit(1)
    ids = [m.get("id", "") for m in d.get("data", [])]
    print(f"[模型列表] 网关共 {len(ids)} 个模型 id")
    return ids


def is_image(id_: str) -> bool:
    low = id_.lower()
    return any(h in low for h in IMG_HINTS) and not any(x in low for x in IMG_EXCLUDE)


def probe_image(base: str, key: str, model: str) -> tuple[str, str]:
    """返回 (状态, 详情)。状态：ok / empty200 / fail"""
    code, d = http(
        f"{base}/v1/images/generations", key=key,
        body={"model": model, "prompt": "a cute red apple on a wooden table, photo",
              "n": 1, "size": "1024x1024"},
        method="POST", timeout=300,
    )
    if code != 200:
        detail = json.dumps(d, ensure_ascii=False)[:200] if isinstance(d, (dict, list)) else str(d)[:200]
        return "fail", f"HTTP {code}: {detail}"
    if isinstance(d, dict):
        data = d.get("data") or []
        if data and (data[0].get("url") or data[0].get("b64_json")):
            kind = "url" if data[0].get("url") else "b64"
            return "ok", f"产出 {kind}"
        return "empty200", f"200 但无产出: {json.dumps(d, ensure_ascii=False)[:150]}"
    return "fail", f"非 JSON 响应: {str(d)[:150]}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-only", action="store_true", help="只筛候选，不实测")
    ap.add_argument("--probe", action="append", default=[], help="只测指定模型（可多次）")
    ap.add_argument("--probe-grep", default=None, help="只测名字含此子串的候选")
    a = ap.parse_args()

    env = load_env()
    base = env["NEWAPI_BASE_URL"].rstrip("/")
    pat = env["NEWAPI_ADMIN_PAT"]
    uid = env["NEWAPI_ADMIN_UID"]
    print(f"[网关] {base}\n")

    key, _ = pick_token(base, pat, uid)
    ids = list_models(base, key)

    if a.probe:
        targets = a.probe
    else:
        targets = [i for i in ids if is_image(i)]
        if a.probe_grep:
            targets = [t for t in targets if a.probe_grep.lower() in t.lower()]
    print(f"[生图候选] {len(targets)} 个：")
    for t in targets:
        print(f"  {t}")

    if a.list_only:
        return

    print(f"\n[实测] 逐个真实生图（每个最多等 300s）...")
    results = {"ok": [], "empty200": [], "fail": []}
    for i, m in enumerate(targets, 1):
        t0 = time.time()
        status, detail = probe_image(base, key, m)
        results[status].append((m, detail))
        mark = {"ok": "✅", "empty200": "⚠️", "fail": "❌"}[status]
        print(f"  ({i}/{len(targets)}) {mark} {m} -> {detail}  [{time.time()-t0:.0f}s]")
        sys.stdout.flush()

    print("\n===== 结论 =====")
    print("✅ 真实可用（有产出）:")
    for m, d in results["ok"]:
        print(f"  {m}  ({d})")
    print("⚠️ 能调但 200 空产出（不稳定，慎上）:")
    for m, d in results["empty200"]:
        print(f"  {m}  ({d})")
    print("❌ 调用失败:")
    for m, d in results["fail"]:
        print(f"  {m}  ({d})")


if __name__ == "__main__":
    main()
