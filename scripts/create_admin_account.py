"""一次性运维脚本：为每个业务创建**独立** new-api 管理员账号（根治多应用互踢）。

背景（2026-09-22 三应用互踢血案）：
  flovart / hewapi / 明判 共用 uid=1 的管理员账号 + 同一把 PAT。PAT 是
  users.access_token **单值字段**，任何一方调 GET /api/user/token 重新生成
  都会覆盖旧值 → 其他应用立即 401 → 对方兜底重登再轮换 → 无限乒乓。

根治：每个业务一个独立管理员账号（role=10）。互为独立隔离单元——
某业务轮换**自己**的 PAT 只影响自己，物理上不可能再互踢。

已读 new-api 源码核实（controller/user.go，2026-09-22）：
  · CreateUser（POST /api/user/）：root(100) 建号时可直接带 role=10
    （校验 user.Role >= myRole 才拒绝 → 100 建 10 畅通），**无需再 promote**；
  · ManageUser promote 仅 root 可调，这里用不上（建号已带 role）；
  · UpdateUser 的 role 字段被硬编码还原，别指望编辑接口改 role。
  ⚠️ 若你的上游版本较老、CreateUser 不认 role 字段：脚本建完会自检 role，
     不是 10 就报错退出并提示手动在后台 promote（root 登录后台 → 用户管理
     → 该用户「设为管理员」）。

凭证零污染设计：
  · root 只用账密 login 开会话，**全程不碰 GET /api/user/token**（那是
    唯一会轮换 root access_token 的动作）→ root 的 PAT 不受任何影响；
  · root 会话用完立刻 DELETE /api/user/sessions/{sid} 归还；
  · 新账号 mint 自己的 PAT 时轮换的是**新账号自己**的 access_token，
    天然无互踢问题；同样用完即归还。

用法（root 凭证走参数或环境变量，绝不落盘）：
    python scripts/create_admin_account.py \
        --username flovart_admin --password 'S3cure-Passw0rd' \
        --root-user root --root-password 'root密码' \
        [--base-url https://...] [--display-name xxx] [--role 10]

    环境变量等价：NEWAPI_BASE_URL / NEWAPI_ROOT_USERNAME / NEWAPI_ROOT_PASSWORD

输出：直接可粘贴的 .env 配置模板（NEWAPI_ADMIN_PAT/UID/USERNAME/PASSWORD）。

失败即停：任何一步失败立即退出（登录/建号端点有限频与失败计数，绝不重试）。
"""
import argparse
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

DEFAULT_TIMEOUT = 30.0
ROLE_ADMIN = 10
ROLE_ROOT = 100
PASSWORD_MAX = 20  # 实测契约：20 位通过、24 位报 max tag 错误


def die(msg: str) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def ok(resp: httpx.Response, what: str) -> dict:
    """统一解析 new-api 响应：HTTP 200 + body.success 才算过，失败即停。"""
    if resp.status_code != 200:
        die(f"{what} 失败：HTTP {resp.status_code} — {resp.text[:300]}")
    body = resp.json()
    if not body.get("success"):
        die(f"{what} 失败：{body.get('message') or body}")
    return body


def login_session(cli: httpx.Client, base: str, username: str, password: str,
                  who: str) -> tuple[int, str, dict]:
    """账密登录开会话，返回 (uid, session_access_token, session_info)。不轮换任何 token。"""
    r = cli.post(f"{base}/api/user/login",
                 json={"username": username, "password": password})
    body = ok(r, f"{who} 登录")
    data = body["data"]
    return int(data["user"]["id"]), data["access_token"], data.get("session") or {}


def release_session(cli: httpx.Client, base: str, at: str, uid: int, session: dict,
                    who: str) -> None:
    sid = (session or {}).get("sid")
    if not sid:
        return
    try:
        cli.delete(f"{base}/api/user/sessions/{sid}",
                   headers={"Authorization": f"Bearer {at}", "New-Api-User": str(uid)})
        print(f"  ↳ {who} 会话已归还（sid={sid}）")
    except httpx.HTTPError as e:
        print(f"  ⚠️ {who} 会话归还失败（不阻塞，顶多占一个会话配额）: {e}")


def mint_pat(cli: httpx.Client, base: str, at: str, uid: int, password: str,
             who: str) -> str:
    """给账号换 PAT（GET /api/user/token）。⚠️ 会轮换**该账号自己**的 access_token。

    rc.37+ 需要 X-Security-Proof：POST /api/verify(password scope) 换一次性 proof。
    旧网关无 /api/verify（404/502）则跳过直换。失败一次即停，绝不重试。
    """
    headers = {"Authorization": f"Bearer {at}", "New-Api-User": str(uid)}
    proof = ""
    r = cli.post(f"{base}/api/verify",
                 json={"method": "password", "scope": "access_token.generate",
                       "password": password}, headers=headers)
    if r.status_code in (404, 502):
        print(f"  ↳ 上游无 /api/verify（旧版网关），跳过 proof 直接换")
    else:
        body = ok(r, f"{who} 安全验证")
        proof = (body.get("data") or {}).get("proof_token") or ""
    if proof:
        headers = {**headers, "X-Security-Proof": proof}
    r = cli.get(f"{base}/api/user/token", headers=headers)
    body = ok(r, f"{who} 换取 PAT")
    return body["data"]


def main() -> None:
    ap = argparse.ArgumentParser(description="创建独立 new-api 管理员账号（根治多应用共用账号互踢）")
    ap.add_argument("--base-url", default=os.getenv("NEWAPI_BASE_URL", ""),
                    help="new-api 网关地址（默认读 NEWAPI_BASE_URL）")
    ap.add_argument("--username", required=True, help="新账号用户名（如 flovart_admin）")
    ap.add_argument("--password", default="", help="新账号密码（≤20 位；不传则交互输入）")
    ap.add_argument("--display-name", default="")
    ap.add_argument("--role", type=int, default=ROLE_ADMIN,
                    help=f"新账号角色（默认 {ROLE_ADMIN}=管理员）")
    ap.add_argument("--root-user", default=os.getenv("NEWAPI_ROOT_USERNAME", ""))
    ap.add_argument("--root-password", default=os.getenv("NEWAPI_ROOT_PASSWORD", ""))
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    if not base:
        die("缺少 --base-url（或环境变量 NEWAPI_BASE_URL）")
    if not args.root_user or not args.root_password:
        die("缺少 root 凭证：--root-user/--root-password 或 NEWAPI_ROOT_USERNAME/NEWAPI_ROOT_PASSWORD")
    password = args.password or getpass.getpass(f"新账号 {args.username} 的密码（≤{PASSWORD_MAX} 位）: ")
    if not password or len(password) > PASSWORD_MAX:
        die(f"密码必须非空且 ≤{PASSWORD_MAX} 位（new-api 实测契约）")
    if args.role >= ROLE_ROOT:
        die("不能创建 root（接口上界 user.Role >= myRole，root 只能由数据库改）")

    with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as cli:
        # ---- 第 1 步：root 登录开会话（一个会话贯穿建号+反查，最后统一归还）----
        print(f"[1/5] root 登录 {base}")
        root_uid, root_at, root_sess = login_session(cli, base, args.root_user,
                                                     args.root_password, "root")
        root_hdr = {"Authorization": f"Bearer {root_at}", "New-Api-User": str(root_uid)}
        me = ok(cli.get(f"{base}/api/user/self", headers=root_hdr), "root 身份确认")
        root_role = int((me.get("data") or {}).get("role") or 0)
        if root_role != ROLE_ROOT:
            die(f"凭证不是 root（role={root_role}，需要 {ROLE_ROOT}）——建管理员是 root 独占能力")

        # ---- 第 2 步：root 建号，直接带 role（源码实锤可传）----
        print(f"[2/5] 创建账号 {args.username}（role={args.role}）")
        payload = {"username": args.username, "password": password,
                   "display_name": args.display_name or args.username,
                   "role": args.role}
        ok(cli.post(f"{base}/api/user/", json=payload, headers=root_hdr), "建号")

        # ---- 第 3 步：反查 uid + role 自检，随后归还 root 会话 ----
        print("[3/5] 反查 uid 与 role 自检")
        try:
            sr = ok(cli.get(f"{base}/api/user/search",
                            params={"keyword": args.username, "p": 1, "page_size": 10},
                            headers=root_hdr), "反查用户")
        finally:
            release_session(cli, base, root_at, root_uid, root_sess, "root")
        items = (sr.get("data") or {}).get("items") or []
        found = next((u for u in items if u.get("username") == args.username), None)
        if not found:
            die(f"建号后反查不到 {args.username}——请到后台人工核对")
        uid, real_role = int(found["id"]), int(found.get("role") or 0)
        print(f"  ↳ uid={uid}, role={real_role}")
        if real_role < ROLE_ADMIN:
            print(f"  ⚠️ role={real_role} 不是管理员（上游版本建号不带 role）——"
                  f"请 root 登录后台 → 用户管理 → {args.username} → 设为管理员，"
                  f"或调 POST /api/user/manage {{\"id\": {uid}, \"action\": \"promote\"}}，"
                  f"然后直接跑第 4 步：让该业务用新账密登录一次即得专属 PAT")
            sys.exit(2)

        # ---- 第 4 步：新账号登录 + 换自己的 PAT（只轮换新账号自己，无互踢）----
        print("[4/5] 新账号登录并生成专属 PAT")
        new_uid, new_at, new_sess = login_session(cli, base, args.username, password, "新账号")
        if new_uid != uid:
            print(f"  ⚠️ 登录 uid={new_uid} 与反查 uid={uid} 不一致，以登录为准")
            uid = new_uid
        try:
            pat = mint_pat(cli, base, new_at, uid, password, "新账号")
        finally:
            release_session(cli, base, new_at, uid, new_sess, "新账号")

        # ---- 第 5 步：输出配置模板 ----
        print("[5/5] 完成 ✅  把下面配置粘贴到对应业务的 .env（替换共用的 uid=1 配置）：\n")
        env = (f"# {args.username} 专属管理员账号（独立于其他业务，轮换互不影响）\n"
               f"NEWAPI_ADMIN_PAT={pat}\n"
               f"NEWAPI_ADMIN_UID={uid}\n"
               f"NEWAPI_ADMIN_USERNAME={args.username}\n"
               f"NEWAPI_ADMIN_PASSWORD={password}\n")
        print(env)
        print("提醒：\n"
              "  · 该账号的 PAT 只会被本业务自己的兜底逻辑轮换，物理上无法再踢别人；\n"
              "  · PASSWORD 建议同步存进密码管理器——它就是兜底重登的命根子；\n"
              "  · 换完配置重启该业务 BFF，并用 smoke_test.sh L1 层验证。\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("已中断")
    except httpx.HTTPError as e:
        die(f"网络错误：{type(e).__name__}: {e}")
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        die(f"响应解析失败（上游契约可能不符）：{type(e).__name__}: {e}")
