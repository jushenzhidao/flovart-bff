"""M1 只读契约实测：对真实 new-api 实例逐条探测并记录。

只做**只读**探测（列表/搜索/自身信息），不做任何会改状态的调用
（建渠道/改用户/加额度/启停/测试全部跳过 —— 那些等契约核对后人工确认再验）。

输出脱敏：打印前抹掉 key/sk- / token / Authorization 相关内容。
用法（需 .env 已配置）：
    .venv/bin/python scripts/probe_console_contracts.py
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, newapi_client as na  # noqa: E402
from app.newapi_client import NewApiError  # noqa: E402

_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9\-_]{4,}|sk-[\w\-]+|Bearer [A-Za-z0-9\-_.]+)", re.I)
_KEY_FIELDS = ("key", "secret", "token", "password", "pat", "access_token", "authorization")


def _redact(obj):
    """递归抹掉可能含密钥的字段值与 sk-/Bearer 串，避免进日志/记录。"""
    if isinstance(obj, dict):
        return {k: ("<redacted>" if k in _KEY_FIELDS else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    if isinstance(obj, str):
        return _SECRET_RE.sub("<redacted>", obj)
    return obj


def _safe(resp_body):
    return _redact(resp_body)


def _keys_of(obj):
    return list(obj.keys()) if isinstance(obj, dict) else None


def _item_preview(items, n=1):
    if not items:
        return {"item_count": 0, "sample": None}
    s = items[0]
    return {"item_count": len(items), "fields": _keys_of(s),
            "sample": _safe(s)}


async def main() -> int:
    cfg = {
        "base_url": config.NEWAPI_BASE_URL,
        "base_url_is_default": config.NEWAPI_BASE_URL_IS_DEFAULT,
        "admin_pat_configured": bool(config.NEWAPI_ADMIN_PAT and config.NEWAPI_ADMIN_UID),
        "admin_pwd_configured": bool(config.NEWAPI_ADMIN_USERNAME and config.NEWAPI_ADMIN_PASSWORD),
        "secret_key_len": len(config.SECRET_KEY),
    }
    print("=== config(脱敏) ===")
    print(json.dumps(cfg, ensure_ascii=False, indent=2))

    async def probe(name: str, fn):
        print(f"\n=== {name} ===")
        try:
            body = await fn()
            inner = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
            print(json.dumps(_safe(inner), ensure_ascii=False, indent=2)[:4000])
            return inner
        except NewApiError as e:
            print(f"NewApiError[{e.status_code}]: {e.message}")
            return None
        except Exception as e:  # noqa: BLE001 —— 探测脚本允许兜底
            print(f"UnexpectedError: {type(e).__name__}: {e}")
            return None

    # 1) 用户全量列表（管理员）：验证 GET /api/user/ 形状
    users = await probe("用户列表 GET /api/user/?p=1&page_size=3", lambda: na.admin_request(
        "GET", "/api/user/", params={"p": 1, "page_size": 3}))
    if users:
        items = users.get("items") or []
        print("total:", users.get("total"), _item_preview(items))

    # 2) 用户搜索（管理员）：verify search 形状（hewapi 已用，但仍核对分页参数）
    await probe("用户搜索 GET /api/user/search?keyword=&p=1&page_size=3", lambda: na.admin_request(
        "GET", "/api/user/search", params={"keyword": "", "p": 1, "page_size": 3}))

    # 3) 渠道列表（管理员）：验证 GET /api/channel/ 形状与字段命名
    ch = await probe("渠道列表 GET /api/channel/?p=1&page_size=3", lambda: na.admin_request(
        "GET", "/api/channel/", params={"p": 1, "page_size": 3}))
    if ch:
        items = ch.get("items") or []
        print("total:", ch.get("total"), _item_preview(items))

    # 4) 模型聚合验证：admin_enabled_models() 走真实数据（仅取前若干渠道，防翻页过重）
    print("\n=== admin_enabled_models() 前 50 个 ===")
    try:
        models = await na.admin_enabled_models()
        print(json.dumps(models[:50], ensure_ascii=False, indent=2))
        print("... total:", len(models))
    except NewApiError as e:
        print(f"NewApiError[{e.status_code}]: {e.message}")

    # 5) 管理端全站日志（只读，验证 /api/log/ 是否可用及形状）
    await probe("全站日志 GET /api/log/?p=1&page_size=1&type=0", lambda: na.admin_request(
        "GET", "/api/log/", params={"p": 1, "page_size": 1, "type": 0}))

    print("\n=== done ===")
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
