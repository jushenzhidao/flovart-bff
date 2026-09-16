"""上游网关模型可用性探针（OpenAI 兼容：/v1/models + /v1/images/generations）。

为什么需要它：**`/v1/models` 列出 760 个模型，不等于都能调通。**
模型名在目录里挂着、网关侧却没配可用渠道时，调用返回 503
`No available channel for model <name> under group <g>`。
「列表里有」与「此刻能生成」是两件事，加模型前必须先实测。

用法（key 优先取 --key，其次环境变量 UPSTREAM_KEY，避免写进仓库）：

  # 1. 看有哪些模型（可按关键词过滤）
  python scripts/probe_upstream_models.py list --base https://api.chatfire.cn/v1 --grep gemini

  # 2. 实测单个模型的生图（返回 b64_json 还是 url 也要看清）
  python scripts/probe_upstream_models.py image --base https://api.chatfire.cn/v1 --model gemini-3.1-flash-image-preview

  # 3. 批量实测（每个都真花钱，慎用）
  python scripts/probe_upstream_models.py image --base ... --model a --model b

实测判据：
  - HTTP 200 + data[0].b64_json  → 可用，直接内联返回图片
  - HTTP 200 + data[0].url       → 可用，但返回的是 CDN 链接（`_1k/_2k/_4k` 变体是这种）
  - HTTP 503 model_not_found     → 目录里有、渠道没有（不是 key 的问题）
  - HTTP 401                     → key 无效
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def _key(args) -> str:
    k = args.key or os.getenv("UPSTREAM_KEY", "")
    if not k:
        sys.exit("缺少 key：用 --key 传入，或设置环境变量 UPSTREAM_KEY")
    return k


def _headers(key: str, json_body: bool = False) -> dict:
    h = {"Authorization": f"Bearer {key}"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _get(url: str, key: str, timeout: int = 30):
    req = urllib.request.Request(url, headers=_headers(key))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _post(url: str, key: str, body: dict, timeout: int = 300):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=_headers(key, json_body=True)
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:600], time.time() - t0
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}", time.time() - t0


def cmd_list(args) -> None:
    d = _get(args.base.rstrip("/") + "/models", _key(args))
    rows = d.get("data", d if isinstance(d, list) else [])
    ids = sorted({r.get("id", "") for r in rows if isinstance(r, dict)})
    if args.grep:
        ids = [i for i in ids if args.grep.lower() in i.lower()]
    print(f"模型总数（过滤后）: {len(ids)}")
    for i in ids:
        print("  ", i)


def cmd_image(args) -> None:
    key = _key(args)
    url = args.base.rstrip("/") + "/images/generations"
    for model in args.model:
        body = {"model": model, "prompt": args.prompt, "n": 1, "size": args.size}
        code, resp, dt = _post(url, key, body)
        print(f"\n=== {model} ===")
        print(f"HTTP {code}   {dt:.1f}s")
        if isinstance(resp, dict):
            items = resp.get("data") or []
            if not items:
                print("  无 data 字段，顶层键:", list(resp.keys()))
            for it in items[:1]:
                if "b64_json" in it:
                    print(f"  内联图片 b64_json: {len(it['b64_json'])} 字符")
                if "url" in it:
                    print(f"  CDN 链接: {it['url']}")
            if resp.get("usage"):
                print("  usage:", json.dumps(resp["usage"], ensure_ascii=False))
        else:
            msg = resp.replace("\n", " ")
            print("  ", msg[:400])
            if "No available channel" in msg:
                print("  → 目录里有、网关渠道没有：不是 key 的问题")


def main() -> None:
    # --base/--key 用 parents 下放到每个子命令：否则 argparse 要求它们必须写在
    # 子命令**之前**（`script.py --base X list`），对使用者太反直觉。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base", required=True, help="形如 https://api.example.cn/v1（不带尾斜杠）")
    common.add_argument("--key", help="不传则读环境变量 UPSTREAM_KEY")

    p = argparse.ArgumentParser(description="上游网关模型可用性探针")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", parents=[common], help="列出模型")
    pl.add_argument("--grep", help="按关键词过滤（不区分大小写）")
    pl.set_defaults(func=cmd_list)

    pi = sub.add_parser("image", parents=[common], help="实测生图")
    pi.add_argument("--model", action="append", required=True, help="可重复传入")
    pi.add_argument("--prompt", default="a red apple on a white table, minimal")
    pi.add_argument("--size", default="1024x1024")
    pi.set_defaults(func=cmd_image)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
