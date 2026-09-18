# -*- coding: utf-8 -*-
"""网关 gemini 生图冒烟验证（用完即删临时令牌）。

用途：验证 new-api 渠道新增模型后 /v1/images/generations 是否可路由。
用法：python scripts/smoke_gemini_gateway.py [model ...]
  模型列表缺省为 3 个 gemini 生图模型。凭据读 .env 的 NEWAPI_ADMIN_USERNAME/PASSWORD。
注意：/api/user/login 有按 IP 的会话签发限频（AUTH_SESSION_ISSUANCE_LIMIT，429），
     被 429 时本脚本会间隔 10 分钟重试一次，最多重试 2 次。
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://newapi-bff.oneapis.cn"
REPO = Path(__file__).resolve().parent.parent
DEFAULT_MODELS = [
    "gemini-3.1-flash-image-preview",
    "gemini-3.1-flash-lite-image",
    "gemini-3-pro-image-preview",
]


def load_env():
    env = {}
    for line in (REPO / ".env").open(encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def login(env):
    body = json.dumps({"username": env["NEWAPI_ADMIN_USERNAME"],
                       "password": env["NEWAPI_ADMIN_PASSWORD"]}).encode()
    req = urllib.request.Request(BASE + "/api/user/login", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        d = json.loads(r.read())
    data = d.get("data") or {}
    return data.get("access_token"), (data.get("user") or {}).get("id")


def main():
    models = sys.argv[1:] or DEFAULT_MODELS
    env = load_env()
    for attempt in range(3):
        try:
            tok, uid = login(env)
            break
        except urllib.error.HTTPError as e:
            print(f"login HTTP {e.code}（第 {attempt + 1} 次），等待 10 分钟后重试...")
            if attempt == 2:
                raise SystemExit("登录持续 429，请稍后手动重跑本脚本")
            time.sleep(600)
    H = {"Content-Type": "application/json", "New-Api-User": str(uid),
         "Authorization": "Bearer " + tok}

    def api(method, path, obj=None):
        data = json.dumps(obj).encode() if obj is not None else None
        req = urllib.request.Request(BASE + path, data=data, headers=H, method=method)
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    name = "smoke-gemini-临时验证"
    api("POST", "/api/token/", {"name": name, "remain_quota": 1000000, "expired_time": -1,
                                "unlimited_quota": True, "model_limits_enabled": False,
                                "model_limits": "", "group": ""})
    items = ((api("GET", "/api/token/?p=1&size=20").get("data") or {}).get("items")
             or (api("GET", "/api/token/?p=1&size=20").get("data") or []))
    t = next((x for x in items if x.get("name") == name), None)
    tid = t.get("id")
    raw = t.get("key") or ""
    sk = raw if raw.startswith("sk-") else "sk-" + raw
    print("临时令牌:", tid, "| key 长度:", len(raw))
    try:
        for m in models:
            body = json.dumps({"model": m, "prompt": "a red apple on a white table"}).encode()
            req = urllib.request.Request(BASE + "/v1/images/generations", data=body,
                                         headers={"Authorization": "Bearer " + sk,
                                                  "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    raw_resp = r.read()
                    j = json.loads(raw_resp)
                    imgs = j.get("data") or []
                    has_img = bool(imgs and (imgs[0].get("url") or imgs[0].get("b64_json")))
                    print(f"{m}: 200 len={len(raw_resp)} has_image={has_img}")
            except urllib.error.HTTPError as e:
                print(f"{m}: HTTP {e.code} {e.read()[:200]}")
    finally:
        try:
            req = urllib.request.Request(BASE + f"/api/token/{tid}", headers=H, method="DELETE")
            with urllib.request.urlopen(req, timeout=60) as r:
                print("清理临时令牌:", json.loads(r.read()).get("success"))
        except Exception as ex:
            print("清理失败（请手动删令牌）:", ex)


if __name__ == "__main__":
    main()
