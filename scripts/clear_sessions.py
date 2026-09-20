"""清空指定账号的全部登录会话（用管理员 PAT，不需要被清账号的密码）。

适用报错：
    「该账号登录设备数已达上限，请在官方前端退出其他设备后重试」
    「409 AUTH_SESSION_LIMIT」

原理：new-api 的 `UpdateUser` 在检测到 auth_version 变化时会调用
`RevokeAllUserSessions` 吊销该用户全部会话（controller/user.go）。
所以只要用管理员权限 PUT 一个更大的 auth_version，就能远程清空 ——
**不需要知道被清账号的密码，也不需要它能登录**。

这是解开「会话打满 → 登不进去 → 没法清会话」死锁的正解：
会话管理接口（/api/user/sessions）要求真实 dashboard 会话上下文，
PAT 调用返回 403；而 PUT /api/user/ 只要管理员权限，PAT 就够。

注意：抬升 auth_version 会**连同该账号的 PAT 一起作废**（实测确认）。
所以清理自己（管理员本人）时，执行后必须重新 login 换新 PAT 并更新配置，
否则后续所有管理员调用都会 401。清理其他账号不受影响。

用法：
    export NEWAPI_ADMIN_PAT=<管理员 PAT>
    export NEWAPI_ADMIN_UID=1

    python scripts/clear_sessions.py 1              # 按 uid 清
    python scripts/clear_sessions.py chatfire       # 按用户名清
    python scripts/clear_sessions.py --list         # 只列出账号，不做改动
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app import config

BASE = config.NEWAPI_BASE_URL
PAT = config.NEWAPI_ADMIN_PAT
UID = config.NEWAPI_ADMIN_UID


def _headers() -> dict:
    return {"Authorization": f"Bearer {PAT}", "New-Api-User": str(UID)}


async def _find_user(c: httpx.AsyncClient, ident: str) -> dict | None:
    """按 uid 或用户名定位账号。"""
    if ident.isdigit():
        r = await c.get(f"/api/user/{ident}", headers=_headers())
        b = r.json()
        return b.get("data") if b.get("success") else None

    # search 走不通时（keyword 匹配范围有限），退回遍历用户列表精确比对。
    # 与 find_redemption 同一个坑：不能假定 search 能按任意字段命中。
    for page in range(1, 21):
        r = await c.get("/api/user/", headers=_headers(),
                        params={"p": page, "page_size": 100, "order": "-id"})
        b = r.json()
        if not b.get("success"):
            return None
        data = b.get("data") or {}
        items = data.get("items") if isinstance(data, dict) else data
        if not items:
            return None
        for u in items:
            if u.get("username") == ident:
                return u
        if len(items) < 100:
            return None
    return None


async def _list_users(c: httpx.AsyncClient) -> None:
    r = await c.get("/api/user/", headers=_headers(),
                    params={"p": 1, "page_size": 30, "order": "-id"})
    b = r.json()
    data = b.get("data") or {}
    items = data.get("items") if isinstance(data, dict) else data
    print(f"{'uid':<6} {'username':<30} {'role':<6} quota")
    for u in items or []:
        print(f"{u['id']:<6} {(u.get('username') or '')[:30]:<30} "
              f"{u.get('role'):<6} {u.get('quota')}")


async def clear(ident: str) -> int:
    if not PAT:
        print("需要管理员 PAT。请先 export NEWAPI_ADMIN_PAT=<PAT>")
        return 2

    async with httpx.AsyncClient(base_url=BASE, timeout=30.0, trust_env=False) as c:
        u = await _find_user(c, ident)
        if not u:
            print(f"未找到账号：{ident}")
            return 1

        uid, name = u["id"], u.get("username")
        print(f"目标账号：uid={uid} username={name} role={u.get('role')}")

        # auth_version 不在读接口里暴露，直接给一个足够大的值即可 ——
        # 只要与当前值不同就会触发吊销。用时间戳避免反复执行时撞上同值。
        import time
        new_ver = int(time.time())

        body = {
            "id": uid,
            "username": name,
            "display_name": u.get("display_name") or name,
            "auth_version": new_ver,
        }
        r = await c.put("/api/user/", headers=_headers(), json=body)
        b = r.json()
        if not b.get("success"):
            print(f"吊销失败：HTTP {r.status_code} {b.get('message')}")
            return 1

        print(f"已抬升 auth_version 至 {new_ver}，该账号全部会话已吊销。")
        print("现在可以正常登录了。")

        if str(uid) == str(UID):
            print("\n重要：你清理的是管理员本人，当前 PAT 已随之作废。")
            print("     需重新 login 换取新 PAT 并更新 NEWAPI_ADMIN_PAT：")
            print("       1) POST /api/user/login 拿 access_token")
            print("       2) GET  /api/user/token 换 PAT")
            print("       3) DELETE /api/user/sessions/{sid} 归还会话")
            print("     否则后续管理员调用全部 401。")
        else:
            print("\n提示：BFF 侧配置 NEWAPI_ADMIN_PAT + NEWAPI_ADMIN_UID 后不再调 login，"
                  "\n     从根上不会再消耗会话配额。")
        return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--list" in sys.argv:
        async def _run():
            async with httpx.AsyncClient(base_url=BASE, timeout=30.0,
                                         trust_env=False) as c:
                await _list_users(c)
        asyncio.run(_run())
    elif not args:
        print(__doc__)
        sys.exit(2)
    else:
        sys.exit(asyncio.run(clear(args[0])))
