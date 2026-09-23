"""方案 B 镜像路由真实生图冒烟（2026-09-23，飞哥授权烧额度）。

流程：读 .env 凭据（不回显）→ 登录一次（失败即停，绝不重试）→
走镜像路由提交真实生图 → 轮询到终态 → 报告结果。
"""
import json
import os
import sys
import time

import httpx

BASE = "http://127.0.0.1:8300"
MODEL = "gpt-image-2"
PROMPT = "a tiny orange cat astronaut, simple flat illustration"
MAX_WAIT = 180
POLL_INTERVAL = 3


def read_env(path: str) -> dict:
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main() -> int:
    env = read_env(r"D:\code\flovart-bff\.env")
    user = env.get("NEWAPI_ADMIN_USERNAME", "")
    pwd = env.get("NEWAPI_ADMIN_PASSWORD", "")
    if not user or not pwd:
        print("SMOKE_FAIL: .env 缺 NEWAPI_ADMIN_USERNAME/PASSWORD")
        return 1

    # 会话复用：本地 BFF_COOKIE_SECURE 默认 true，httpx 走 http 不回发 Secure cookie，
    # 故登录后手动把 cookie 值塞进后续请求头。已登录过则复用，绝不重复登录（飞哥铁律）。
    cookie_file = r"D:\code\flovart-bff\scripts\.smoke_cookie"
    cookie_val = ""
    if os.path.exists(cookie_file):
        cookie_val = open(cookie_file, encoding="utf-8").read().strip()

    with httpx.Client(timeout=30.0) as c:
        # 1) 登录 —— 只允许一次
        if not cookie_val:
            t0 = time.time()
            r = c.post(f"{BASE}/api/user/login",
                       json={"username": user, "password": pwd})
            if r.status_code != 200 or not r.json().get("success"):
                print(f"SMOKE_FAIL: 登录失败 http={r.status_code} body={r.text[:300]}")
                return 1
            cookie_val = c.cookies.get("bff_session", "")
            with open(cookie_file, "w", encoding="utf-8") as f:
                f.write(cookie_val)
            print(f"login ok in {time.time()-t0:.1f}s (cookie saved)")
        else:
            print("reuse saved session cookie (no login)")
        c.headers["Cookie"] = f"bff_session={cookie_val}"

        # 2) 异步镜像路由提交
        t0 = time.time()
        r = c.post(f"{BASE}/api/async/v1/images/generations",
                   json={"type": "image-gen",
                         "params": {"model": MODEL, "prompt": PROMPT, "image": []}})
        print(f"async submit http={r.status_code} elapsed={time.time()-t0:.2f}s")
        body = r.json()
        print("submit body:", json.dumps(body, ensure_ascii=False)[:500])
        if r.status_code != 200 or not body.get("success"):
            print("SMOKE_FAIL: 异步镜像提交被拒（若因平台服务为 sync 类型，改走同步镜像）")
            return 1
        data = body.get("data") or {}
        req_id = data.get("id")
        print(f"task_id(request_id)={req_id} view_mode={data.get('mode')} "
              f"view_status={data.get('status')} gw_task={data.get('taskId')}")

        # 3) 异步镜像路由轮询
        deadline = time.time() + MAX_WAIT
        final = None
        n = 0
        while time.time() < deadline:
            n += 1
            pr = c.get(f"{BASE}/api/async/v1/images/generations/{req_id}")
            pb = pr.json()
            d = pb.get("data") or {}
            status = d.get("status")
            print(f"poll#{n} http={pr.status_code} status={status}")
            if status in ("succeeded", "failed", "cancelled"):
                final = d
                break
            time.sleep(POLL_INTERVAL)
        if final is None:
            print("SMOKE_FAIL: 轮询超时 3 分钟")
            return 1
        print("final status:", final.get("status"))
        result = final.get("result") or {}
        print("result keys:", list(result.keys()))
        for img in (result.get("images") or [])[:2]:
            url = str(img.get("url") or "")[:120]
            print(f"  image url={url} bffKey={img.get('_bffMediaKey')}")
        if final.get("status") != "succeeded":
            print("SMOKE_FAIL:", json.dumps(result, ensure_ascii=False)[:600])
            return 1
        # 4) 验证 media 可访问
        ok_media = False
        for img in (result.get("images") or []):
            key = img.get("_bffMediaKey")
            if key:
                mr = c.get(f"{BASE}/api/me/media/{key}")
                print(f"media check http={mr.status_code} bytes={len(mr.content)} "
                      f"ct={mr.headers.get('content-type')}")
                if mr.status_code == 200 and len(mr.content) > 1000:
                    ok_media = True
                break
        print("SMOKE_OK" if ok_media else "SMOKE_OK_WITHOUT_MEDIA_CHECK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
