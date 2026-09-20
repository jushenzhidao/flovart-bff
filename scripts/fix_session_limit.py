"""管理员会话打满（409 AUTH_SESSION_LIMIT）诊断与恢复工具。

背景（已读 new-api 源码核实）：
  service/auth_session.go: createLoginSession
      activeCount >= common.UserSessionActiveLimit  →  硬拒绝 ErrUserSessionLimit
  · UserSessionActiveLimit 默认 50，**不淘汰最旧会话**
  · LoginSessionTTL 30 天，会话不会很快自然过期
  · 无管理员豁免：root 用户同样受限
  · 会话管理接口（GET/DELETE /api/user/sessions、revoke-others）要求真实
    dashboard 会话上下文，PAT 调用返回 403 —— 所以「登不进去」就等于
    「没法用 API 清会话」，形成死锁

对 BFF 的影响：管理员账号一旦锁死，建号（注册）、加额度（首充赠送）、
兑换码登录全部瘫痪，是整个系统最高危的单点。

根治方案（已实装在 newapi_client.py）：
  1. 登录换到 PAT 后立刻 DELETE /api/user/sessions/{sid} 归还会话
     → 实测 10 次登录净增 0 个会话
  2. 支持 NEWAPI_ADMIN_PAT 环境变量直供 PAT，BFF 完全不调 login
     → PAT 走 users.access_token 列，不经过会话系统，会话打满也照常可用
  3. PAT 落盘 data/admin_cred.json，进程重启不消耗会话配额

用法：
    .venv/bin/python scripts/fix_session_limit.py            # 诊断
    .venv/bin/python scripts/fix_session_limit.py --sql      # 打印 DB 修复 SQL
"""
import asyncio
import sys
from pathlib import Path

# 按文件位置推导项目根，不写死绝对路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app import config

BASE = config.NEWAPI_BASE_URL
ADMIN_U = config.NEWAPI_ADMIN_USERNAME
ADMIN_P = config.NEWAPI_ADMIN_PASSWORD

# SQL 用子查询定位 uid，不需要人工替换任何占位符 —— 死锁场景下人往往在慌乱中
# 操作，少一步手改就少一次改错行的机会。
# 三种方言分开给：new-api 支持 MySQL / PostgreSQL / SQLite，而
# UNIX_TIMESTAMP() 只有 MySQL 有，在 SQLite 上会直接报语法错。
SQL_HEADER = """\
-- ============ 直接改 new-api 数据库（API 已无解时的唯一出路）============
-- 适用场景：管理员登不进前端（无邮箱找回 / 无 OAuth / SMTP 未配），
--          且 API 全部会话端点都要 access_token —— 形成死锁。
--
-- 依据 model/user_session.go：计入活跃上限的条件是
--   status = 'active' AND expires_at > now    （注意：不检查 revoked_at）
-- 所以把 status 改成 'revoked' 就能释放配额。
--
-- 执行前务必备份：
--   MySQL     mysqldump -u<user> -p <db> user_sessions > sessions.bak.sql
--   PostgreSQL pg_dump -t user_sessions <db> > sessions.bak.sql
--   SQLite    cp one-api.db one-api.db.bak
"""

SQL_MYSQL = """\
-- ---------- MySQL / MariaDB ----------
-- 1) 确认账号与当前活跃会话数
SELECT u.id, u.username, u.role,
       (SELECT COUNT(*) FROM user_sessions s
         WHERE s.user_id = u.id AND s.status = 'active'
           AND s.expires_at > UNIX_TIMESTAMP()) AS active_sessions
  FROM users u WHERE u.username = '{admin}';

-- 2) 撤销该账号全部活跃会话（等价于源码的 RevokeAllUserSessions）
UPDATE user_sessions
   SET status = 'revoked',
       revoked_at = UNIX_TIMESTAMP(),
       revoked_reason = 'manual_cleanup'
 WHERE user_id = (SELECT id FROM users WHERE username = '{admin}')
   AND status = 'active'
   AND expires_at > UNIX_TIMESTAMP();

-- 3) 复核：应返回 0
SELECT COUNT(*) AS still_active FROM user_sessions
 WHERE user_id = (SELECT id FROM users WHERE username = '{admin}')
   AND status = 'active' AND expires_at > UNIX_TIMESTAMP();
"""

SQL_POSTGRES = """\
-- ---------- PostgreSQL ----------
SELECT u.id, u.username, u.role,
       (SELECT COUNT(*) FROM user_sessions s
         WHERE s.user_id = u.id AND s.status = 'active'
           AND s.expires_at > EXTRACT(EPOCH FROM NOW())::bigint) AS active_sessions
  FROM users u WHERE u.username = '{admin}';

UPDATE user_sessions
   SET status = 'revoked',
       revoked_at = EXTRACT(EPOCH FROM NOW())::bigint,
       revoked_reason = 'manual_cleanup'
 WHERE user_id = (SELECT id FROM users WHERE username = '{admin}')
   AND status = 'active'
   AND expires_at > EXTRACT(EPOCH FROM NOW())::bigint;
"""

SQL_SQLITE = """\
-- ---------- SQLite（默认部署常用）----------
SELECT u.id, u.username, u.role,
       (SELECT COUNT(*) FROM user_sessions s
         WHERE s.user_id = u.id AND s.status = 'active'
           AND s.expires_at > strftime('%s','now')) AS active_sessions
  FROM users u WHERE u.username = '{admin}';

UPDATE user_sessions
   SET status = 'revoked',
       revoked_at = strftime('%s','now'),
       revoked_reason = 'manual_cleanup'
 WHERE user_id = (SELECT id FROM users WHERE username = '{admin}')
   AND status = 'active'
   AND expires_at > strftime('%s','now');
"""

SQL_FOOTER = """\
-- ---------- 改完之后 ----------
-- a) 若启用了 Redis：必须重启 new-api 或清 Redis，否则会话快照仍在缓存里，
--    改库不会立刻生效（这一步最容易被漏掉，表现为「SQL 跑了但还是 409」）。
--
-- b) 若改完变成 429 AUTH_SESSION_ISSUANCE_LIMIT，那是另一套「签发窗口限流」，
--    它按 created_at 计数且不看 status，撤销无效。等窗口过去，或删行：
--    DELETE FROM user_sessions
--     WHERE user_id = (SELECT id FROM users WHERE username = '{admin}')
--       AND status = 'revoked';
--
-- c) 恢复后立刻做这件事，否则还会复发：
--    前端「个人设置 → 系统访问令牌」生成 PAT，配到 BFF 的
--    NEWAPI_ADMIN_PAT + NEWAPI_ADMIN_UID。PAT 走 users.access_token 列，
--    不经过会话系统，从根上不再消耗会话配额。
"""


async def main():
    print("=" * 60)
    print(f"new-api: {BASE}")
    print(f"管理员:  {ADMIN_U}")
    print("=" * 60)

    async with httpx.AsyncClient(base_url=BASE, timeout=20, trust_env=False) as c:
        r = await c.post("/api/user/login", json={"username": ADMIN_U, "password": ADMIN_P})
        try:
            code = r.json().get("code", "")
        except ValueError:
            code = ""

        if r.status_code == 200 and r.json().get("success"):
            d = r.json()["data"]
            uid, sid = d["user"]["id"], d["session"]["sid"]
            H = {"Authorization": f"Bearer {d['access_token']}", "New-Api-User": str(uid)}
            n = len((await c.get("/api/user/sessions", headers=H)).json().get("data") or [])
            print(f"\n[OK] 管理员可正常登录，uid={uid}，当前活跃会话 {n}/50")

            if n > 1:
                print(f"     清理其他 {n - 1} 个历史会话…")
                rr = await c.post("/api/user/sessions/revoke-others", headers=H)
                print("     revoke-others:", rr.status_code, rr.json().get("message", ""))

            # 顺手把 PAT 打出来，方便配置到环境变量彻底摆脱 login 依赖
            pat = (await c.get("/api/user/token", headers=H)).json().get("data")
            await c.delete(f"/api/user/sessions/{sid}", headers=H)  # 归还本次会话
            left = "?"
            print(f"     本次会话已归还，剩余活跃会话应为 {left if left != '?' else n - (n - 1) - 1}")
            print("\n" + "-" * 60)
            print("建议：把下面两行写进环境变量/.env，BFF 将不再调用 login，")
            print("     从根本上不再消耗会话配额（PAT 不经过会话系统）：")
            print(f"\n  export NEWAPI_ADMIN_PAT='{pat}'")
            print(f"  export NEWAPI_ADMIN_UID={uid}")
            print("-" * 60)
            print("\n注意：注意：在 new-api 前端点「系统访问令牌」会重新生成 PAT 并作废此值，")
            print("   届时需要重新执行本脚本获取。")
            return

        print(f"\n[FAIL] 管理员登录失败: HTTP {r.status_code} code={code}")
        print(f"       {r.text[:200]}")

        if code != "AUTH_SESSION_LIMIT":
            print("\n非会话上限问题，请检查账密是否正确。")
            return

        print("\n诊断：账号活跃会话已达 50 上限。new-api 的判定是硬拒绝、")
        print("      不淘汰最旧会话，且会话 TTL 30 天，不会很快自然恢复。")
        print("      会话管理接口需要 dashboard 会话上下文，PAT 调不通，")
        print("      所以「登不进去」= 「没法用 API 自救」。")

        st = (await c.get("/api/status")).json().get("data", {})
        ways = {
            "邮箱找回密码": st.get("email_verification"),
            "GitHub 登录": st.get("github_oauth"),
            "LinuxDo 登录": st.get("linuxdo_oauth"),
            "Telegram 登录": st.get("telegram_oauth"),
            "微信登录": st.get("wechat_login"),
            "Passkey": st.get("passkey_login"),
        }
        avail = [k for k, v in ways.items() if v]

        print("\n" + "=" * 60)
        print("恢复方案（按成本从低到高）")
        print("=" * 60)

        if avail:
            print(f"\n【方案 A】用其他登录方式进入前端（本实例已开启：{', '.join(avail)}）")
            print("  进入后：个人设置 → 登录设备管理 → 退出其他设备")
        else:
            print("\n【方案 A】不可用：本实例只开启了密码登录，")
            print("  未开启邮箱找回 / OAuth / Passkey，无法从前端绕过。")

        print("\n【方案 B】用另一个 root/管理员账号救援")
        print("  在用户管理里编辑被锁账号并提交更大的 auth_version，")
        print("  UpdateUser 会触发 RevokeAllUserSessions 吊销其全部会话。")
        print("  （源码 controller/user.go：吊销条件是 auth_version 抬升）")

        print("\n【方案 C】直接改数据库 —— 无其他登录方式时的唯一出路")
        print("  执行 `--sql` 查看完整语句。")

        print("\n【善后】恢复后立刻配置 NEWAPI_ADMIN_PAT 环境变量，")
        print("       让 BFF 不再依赖 login，避免同样的事故复发。")


def _print_sql(admin: str) -> None:
    """打印三种方言的修复语句。

    为什么全都打出来而不是让用户选：死锁发生时最缺的就是「还要去查我用的是哪种
    数据库」这种额外步骤。三段都给，用户挑对应的那段粘贴即可。
    """
    if not admin:
        print("需要指定账号名。用法：")
        print("  python scripts/fix_session_limit.py --sql <管理员用户名>")
        print("  或先 export NEWAPI_ADMIN_USERNAME=<用户名>")
        raise SystemExit(2)

    print(SQL_HEADER)
    for block in (SQL_MYSQL, SQL_POSTGRES, SQL_SQLITE):
        print(block.format(admin=admin))
    print(SQL_FOOTER.format(admin=admin))


if __name__ == "__main__":
    if "--sql" in sys.argv:
        # 账号优先取命令行位置参数，其次环境变量 —— 死锁时环境变量往往还没配
        rest = [a for a in sys.argv[1:] if not a.startswith("-")]
        _print_sql(rest[0] if rest else ADMIN_U)
    else:
        asyncio.run(main())
