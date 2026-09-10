"""new-api 真实代理客户端（移植自 hewapi-bff，已实测契约部分原样保留）。

## 已实测契约（hewapi-bff 2026-08-22 对真实实例核实，本仓库沿用）
1. 登录:  POST /api/user/login → data.access_token(15min JWT) + data.user.id + data.session.sid
   new-api 会话上限 50 且**硬拒绝、不淘汰最旧会话**，打满 409 AUTH_SESSION_LIMIT。
   → BFF 必须在换完 PAT 后立刻 DELETE /api/user/sessions/{sid} 归还会话。
2. PAT:   GET /api/user/token（登录态调用一次）→ data 为长期 PAT 字符串
   用户态请求头：Authorization: Bearer <PAT> + New-Api-User: <uid>（缺一不可）
   PAT 走 users.access_token 列，**不经过会话系统**（删掉会话后 PAT 仍有效）。
3. Key:   GET /api/token/?p=&size=   POST /api/token/   POST /api/token/:id/key
          DELETE /api/token/:id
4. 日志:  GET /api/log/self?p=&page_size=&type=0；GET /api/log/self/stat?type=0
5. 建号:  POST /api/user/ (管理员) {username,password,display_name} → 仅 success，
          需 GET /api/user/search?keyword= 反查 uid
          密码有 max 长度校验：实测 20 位通过、24 位报 max tag 错误
6. 加额度: POST /api/user/manage (管理员) {id, action:"add_quota", mode, value}
          mode/value 必填；无幂等键，调用方需自行去重（见 promo.py）
7. 删号:  DELETE /api/user/:id (管理员)
8. 改账密: PUT /api/user/ (管理员) {id, username, password, display_name, group}

## 已实测契约（2026-09-03 对本仓库对接的 new-api 实例核实）
9. 用户全量列表（管理员）: GET /api/user/?p=&page_size= → {page,page_size,total,items}
   items 含 password(密文)/setting 等敏感字段 —— **console 输出必须白名单**。
   GET /api/user/search?keyword=&p=&page_size= 同形状（keyword 空 = 全量）。
10. 渠道列表（管理员）: GET /api/channel/?p=&page_size= → {items,total,...}
    - **models 是逗号分隔字符串**（不是数组）；status: 1=启用 2=手动停用；
    - 列表/详情均返回 key 字段 —— **不可下发给前端**，白名单字段输出。
11. 渠道启停: POST /api/channel/{id}/status  body {"status":1|2}
    （ChannelStatusRequest 只允许 1/2，返回 data=changed bool）—— 不是 PUT！
12. 渠道测试: GET /api/channel/test/{id}（触发上游真实请求，慎调）
13. 启用模型目录: GET /api/channel/models_enabled → data 为启用渠道的模型名数组
    （创作站「可用模型目录」的数据源；视频模型 doubao-seedance-* 等一并返回）
14. 全站日志（管理员）: GET /api/log/?p=&page_size=&type= → {page,page_size,total,items}
    字段丰富（user_id/model_name/quota/channel_name/request_id...），可做调用记录页。
15. 渠道 model 型数据（详情用）: GET /api/channel/{id}；GET /api/channel/search?keyword=
"""
import json as _json
import logging
import os
from typing import Any, Optional

import httpx

from . import config, observability

logger = logging.getLogger("bff.newapi")


class NewApiError(Exception):
    """new-api 返回业务失败或网络错误。message 可直接展示给用户。"""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=config.NEWAPI_BASE_URL,
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            trust_env=False,  # 不走本机代理
        )
        observability.instrument_httpx(_client)
    return _client


async def close() -> None:
    global _client, _EXTERNAL_CLIENT
    if _client is not None:
        await _client.aclose()
        _client = None
    if _EXTERNAL_CLIENT is not None:
        await _EXTERNAL_CLIENT.aclose()
        _EXTERNAL_CLIENT = None


def user_headers(pat: str, uid: int) -> dict:
    return {"Authorization": f"Bearer {pat}", "New-Api-User": str(uid)}


async def admin_user_headers(uid: int) -> dict:
    """返回以管理员 PAT 代用户 uid 调网关的请求头（自动确保 admin PAT 就绪）。

    用于 BFF 代理用户态请求（聊天补全等）：平台共用一把管理员 key，但 New-Api-User
    让网关把请求归属与计费落到指定 end-user，按 uid 隔离（越权读取由网关 404 兜底）。
    PAT 失效时自动重登一次（与 admin_request / request_as_user 同源）。
    """
    if _admin_cache["pat"] is None:
        _load_admin_cred()
    if _admin_cache["pat"] is None:
        await _admin_login()
    return user_headers(_admin_cache["pat"], int(uid))


# ---------- 管理员凭证（三通道）----------
#   1) NEWAPI_ADMIN_PAT + NEWAPI_ADMIN_UID  —— 不碰会话系统，生产首选
#   2) data/admin_cred.json 落盘缓存           —— 进程重启后复用
#   3) 账密 login 换 PAT                        —— 兜底，用完立刻归还会话
_admin_cache: dict = {"pat": None, "uid": None}


def _load_admin_cred() -> None:
    if config.NEWAPI_ADMIN_PAT and config.NEWAPI_ADMIN_UID:
        _admin_cache["pat"] = config.NEWAPI_ADMIN_PAT
        _admin_cache["uid"] = config.NEWAPI_ADMIN_UID
        logger.info("admin cred loaded from env (no session consumed)")
        return
    try:
        with open(config.ADMIN_CRED_FILE, "r", encoding="utf-8") as f:
            d = _json.load(f)
        if d.get("pat") and d.get("uid"):
            _admin_cache["pat"] = d["pat"]
            _admin_cache["uid"] = int(d["uid"])
            logger.info("admin cred loaded from disk cache")
    except (OSError, ValueError, KeyError):
        pass


def _save_admin_cred() -> None:
    """PAT 落盘。失败不影响主流程（只是下次冷启多消耗一个会话）。"""
    if config.NEWAPI_ADMIN_PAT:
        return
    try:
        os.makedirs(os.path.dirname(config.ADMIN_CRED_FILE), exist_ok=True)
        tmp = config.ADMIN_CRED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump({"pat": _admin_cache["pat"], "uid": _admin_cache["uid"]}, f)
        os.replace(tmp, config.ADMIN_CRED_FILE)
        os.chmod(config.ADMIN_CRED_FILE, 0o600)  # PAT 等同管理员密码
    except OSError as e:
        logger.warning("save admin cred failed: %s", e)


async def _admin_login() -> None:
    """管理员登录换 PAT，换完立刻归还会话（避免占满 50 会话上限）。"""
    if not (config.NEWAPI_ADMIN_USERNAME and config.NEWAPI_ADMIN_PASSWORD):
        logger.error(
            "管理员凭证未配置：建号/加额度/管理台不可用。"
            "请设置 NEWAPI_ADMIN_PAT + NEWAPI_ADMIN_UID（推荐），"
            "或 NEWAPI_ADMIN_USERNAME + NEWAPI_ADMIN_PASSWORD。"
        )
        raise NewApiError("服务暂时不可用，请稍后重试或联系客服", 503)
    try:
        body = await request("POST", "/api/user/login", headers={},
                             json={"username": config.NEWAPI_ADMIN_USERNAME,
                                   "password": config.NEWAPI_ADMIN_PASSWORD})
    except NewApiError as e:
        if e.status_code == 409:
            logger.error(
                "管理员会话数已达 new-api 上限（50 个硬拒绝），login 409。"
                "根治：配置 NEWAPI_ADMIN_PAT + NEWAPI_ADMIN_UID（PAT 不走会话系统）。"
            )
            raise NewApiError("服务暂时不可用，请稍后重试或联系客服", 503) from e
        # 账密错 / 账号被封禁 / 账号不存在：这是 BFF 服务端运维配置问题，
        # 不应把 new-api 内部英文错误透传给终端用户（否则用户会误以为是「自己的密码错了」）。
        # 原始错误记日志供运维排查，对用户统一返回友好提示。
        logger.error(
            "管理员登录 new-api 失败（账密错误或账号被封禁，请核对 NEWAPI_ADMIN_USERNAME/PASSWORD 是否为当前有效管理员账密）：%s",
            e.message,
        )
        raise NewApiError("共享/管理类服务暂时不可用（管理员凭证失效），请联系管理员", 503) from e
    data = body["data"]
    uid = data["user"]["id"]
    access_token = data["access_token"]
    pat_body = await request("GET", "/api/user/token",
                             headers=user_headers(access_token, uid))
    _admin_cache["pat"] = pat_body["data"]
    _admin_cache["uid"] = uid
    _save_admin_cred()
    await _release_session(access_token, uid, data.get("session") or {})


async def admin_request(method: str, path: str, *, json: Any = None,
                        params: dict | None = None,
                        client: "httpx.AsyncClient | None" = None) -> Any:
    """管理员请求：PAT 失效时自动重新登录重试一次。

    client：可传入自定义超时的 httpx.AsyncClient（图片生成需 300s，
    默认 get_client() 仅 15s 会超时）。不清连接池，由调用方管理生命周期。
    """
    if _admin_cache["pat"] is None:
        _load_admin_cred()
    if _admin_cache["pat"] is None:
        await _admin_login()
    try:
        return await request(method, path, json=json, params=params,
                             headers=user_headers(_admin_cache["pat"], _admin_cache["uid"]),
                             client=client)
    except NewApiError as e:
        if e.status_code != 401:
            raise
        # PAT 被外部轮换掉了（官方前端点一次「系统访问令牌」就会作废旧值）
        logger.warning("admin PAT rejected, re-login to rotate")
        await _admin_login()
        return await request(method, path, json=json, params=params,
                             headers=user_headers(_admin_cache["pat"], _admin_cache["uid"]),
                             client=client)


async def request_as_user(method: str, path: str, uid: int, *, json: Any = None,
                          params: dict | None = None,
                          client: "httpx.AsyncClient | None" = None) -> Any:
    """以管理员 PAT 代用户 uid 发起网关请求（Authorization: Bearer <adminPAT> + New-Api-User: <uid>）。

    用于 BFF 代理用户态异步任务（图片 tasks）：平台共用一把管理员 key，
    但 New-Api-User 让网关把请求归属与计费落到指定 end-user，并按 uid 隔离其任务
    （越权读取他人 task 网关返回 404）。PAT 失效时自动重登重试一次（同 admin_request）。

    注意：绝不要用 admin_request 转发用户任务——它把 New-Api-User 写成管理员自身 uid，
    会导致用户任务计费错挂到管理员账上、且无法按用户隔离。
    """
    if _admin_cache["pat"] is None:
        _load_admin_cred()
    if _admin_cache["pat"] is None:
        await _admin_login()
    headers = user_headers(_admin_cache["pat"], int(uid))
    try:
        return await request(method, path, json=json, params=params,
                             headers=headers, client=client)
    except NewApiError as e:
        if e.status_code != 401:
            raise
        await _admin_login()
        return await request(method, path, json=json, params=params,
                             headers=user_headers(_admin_cache["pat"], int(uid)),
                             client=client)


async def request(method: str, path: str, *, headers: dict, json: Any = None,
                  params: dict | None = None, client_ip: str | None = None,
                  client: "httpx.AsyncClient | None" = None) -> Any:
    """统一请求：网络错误与业务失败都抛 NewApiError，data 原样返回。

    client：可传入自定义超时的 httpx.AsyncClient（图片生成需 300s，
    默认 get_client() 仅 15s 会超时）。不负责关闭，由调用方管理。
    client_ip：转发真实客户端 IP 供 new-api 按 IP 限流 —— BFF 出口只有一个 IP，
    不转发的话一个用户狂点登录会把全站锁死。需 new-api 侧配置信任代理生效。
    """
    if client_ip:
        headers = {**headers, "X-Forwarded-For": client_ip, "X-Real-IP": client_ip}
    cli = client or get_client()
    try:
        resp = await cli.request(method, path, headers=headers, json=json, params=params)
    except httpx.HTTPError:
        raise NewApiError("上游服务暂时不可用，请稍后重试", 502)
    return _parse_response(resp, method, path)


def _parse_response(resp: "httpx.Response", method: str, target: str) -> Any:
    """统一解析网关响应：网络/业务失败抛 NewApiError，data 原样返回。target 用于日志（path 或完整 URL）。"""
    if resp.status_code == 401:
        raise NewApiError("凭证已失效，请重新登录", 401)
    if resp.status_code == 409:
        try:
            code = resp.json().get("code", "")
        except ValueError:
            code = ""
        if code == "AUTH_SESSION_LIMIT":
            logger.error("账号会话数已达上限，需在 new-api 前端「登录设备管理」清理会话")
            raise NewApiError("该账号登录设备数已达上限，请在官方前端退出其他设备后重试", 409)
        raise NewApiError("操作冲突，请稍后重试", 409)
    if resp.status_code == 429:
        retry = resp.headers.get("retry-after", "")
        wait = ""
        if retry.isdigit():
            secs = int(retry)
            wait = f"，请 {secs // 60 + 1} 分钟后再试" if secs >= 60 else f"，请 {secs} 秒后再试"
        if not wait:
            wait = "，请稍后再试"
        raise NewApiError("操作过于频繁" + wait, 429)
    try:
        body = resp.json()
    except ValueError:
        content_type = (resp.headers.get("content-type") or "").lower()
        text = ""
        try:
            text = resp.text[:1000]
        except Exception:
            pass
        logger.warning(
            "上游响应无法解析为 JSON: method=%s target=%s status=%s content_type=%s body_preview=%r",
            method, target, resp.status_code, content_type, text,
        )
        # 网关把未知路由兜底成前端 SPA（text/html）——说明该后端端点根本没实现/路径拼错，
        # 不是偶发上游异常。给前端一个能直接看懂的提示，避免被「上游返回异常」误导。
        if "text/html" in content_type:
            raise NewApiError(
                f"网关未实现该图片端点（{target} 返回了前端页面而非 JSON）。"
                f"请确认网关已落地图片接口，或核对同步/异步端点路径配置。",
                502,
            )
        raise NewApiError("上游返回异常（非 JSON）", 502)
    if isinstance(body, dict) and body.get("success") is False:
        raise NewApiError(body.get("message") or "操作失败", 400)
    return body


# ---------------------------------------------------------------------------
# 外部 OpenAI 兼容网关直连（如 chatfire）：用用户自有 sk- key，不走 new-api admin PAT。
# 图片任务「按服务路由」时调用——前端把选中的 AI 服务 baseUrl + api_key 带给 BFF，
# BFF 直连该服务端点，完全按用户配置来。
# ---------------------------------------------------------------------------
_EXTERNAL_CLIENT: "httpx.AsyncClient | None" = None


def _external_client() -> httpx.AsyncClient:
    global _EXTERNAL_CLIENT
    if _EXTERNAL_CLIENT is None:
        _EXTERNAL_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(config.GATEWAY_SYNC_TIMEOUT, connect=10.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
            trust_env=False,  # 不走本机代理
        )
    return _EXTERNAL_CLIENT


async def request_external(method: str, url: str, *, api_key: str,
                            json: Any = None,
                            client: "httpx.AsyncClient | None" = None) -> Any:
    """直连外部 OpenAI 兼容网关（Bearer <api_key>），返回解析后的 JSON 或抛 NewApiError。

    与 request_as_user 不同：不注入 new-api 管理员 PAT / New-Api-User，url 为完整地址。
    """
    cli = client or _external_client()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        resp = await cli.request(method, url, headers=headers, json=json)
    except httpx.HTTPError:
        raise NewApiError("上游服务暂时不可用，请稍后重试", 502)
    return _parse_response(resp, method, url)


def _data_of(body: Any) -> dict:
    """解包上游响应：兼容外层带 data 与已是内层两种形状，恒返回 dict。"""
    outer = body if isinstance(body, dict) else {}
    inner = outer.get("data")
    return inner if isinstance(inner, dict) else outer


# ---------- 认证（用户态）----------
async def login(username: str, password: str, client_ip: str | None = None) -> dict:
    """密码登录 → 换 PAT → **立刻归还会话**。返回 {uid, username, pat, user}。

    归还会话是硬性要求（见模块 docstring）：BFF 只需要 PAT、不用会话，
    不归还的话同一账号登满 50 次就永久 409。
    """
    body = await request("POST", "/api/user/login", headers={},
                         json={"username": username, "password": password},
                         client_ip=client_ip)
    data = body["data"]
    access_token = data["access_token"]
    user = data["user"]
    uid = user["id"]
    pat_body = await request("GET", "/api/user/token",
                             headers=user_headers(access_token, uid))
    pat = pat_body["data"]
    await _release_session(access_token, uid, data.get("session") or {})
    return {"uid": uid, "username": user["username"], "pat": pat, "user": user}


async def _release_session(access_token: str, uid: int, session: dict) -> None:
    """归还刚建立的登录会话。必须用 access_token（PAT 调会话接口会被拒）。
    失败只记日志不抛错 —— 顶多浪费一个会话配额，不该让登录因此失败。
    """
    sid = session.get("sid")
    if not sid:
        return
    try:
        await request("DELETE", f"/api/user/sessions/{sid}",
                      headers=user_headers(access_token, uid))
    except Exception:
        logger.warning("释放登录会话失败 uid=%s sid=%s（会话配额将被占用）", uid, sid)


# ---------- 管理员：用户域（hewapi 实测契约）----------
async def admin_create_user(username: str, password: str, display_name: str = "") -> int:
    """管理员建号，返回 uid（建号接口不返回 id，需反查）。"""
    await admin_request("POST", "/api/user/",
                        json={"username": username, "password": password,
                              "display_name": display_name or username})
    found = await admin_request("GET", "/api/user/search",
                                params={"keyword": username, "p": 1, "page_size": 10})
    for item in found["data"]["items"]:
        if item["username"] == username:
            return item["id"]
    raise NewApiError("建号成功但未找到用户", 500)


async def admin_delete_user(uid: int) -> None:
    await admin_request("DELETE", f"/api/user/{int(uid)}")


async def admin_update_user(uid: int, username: str, password: str,
                            display_name: str = "", group: str = "default") -> None:
    """管理员改用户账密。uid 不变，余额/Key/日志全保留。
    注意：group 不传会被清空，默认补 default。
    """
    payload = {"id": int(uid), "username": username,
               "display_name": display_name or username,
               "password": password, "group": group}
    await admin_request("PUT", "/api/user/", json=payload)


async def admin_add_quota(uid: int, quota: int, mode: str = "add") -> None:
    """管理员增减额度。quota 为 new-api 内部单位。无幂等键，调用方需自行去重。"""
    await admin_request("POST", "/api/user/manage",
                        json={"id": int(uid), "action": "add_quota",
                              "mode": mode, "value": int(quota)})


async def admin_list_users(keyword: str = "", page: int = 1, page_size: int = 10) -> dict:
    """管理员用户列表/搜索（契约见头部 #9，均已实测）。

    keyword 非空走 GET /api/user/search，空走 GET /api/user/ 全量；
    两者同形状 {items,total}。**items 含 password 密文，前端必须白名单输出**。
    """
    params = {"p": max(1, page), "page_size": min(max(1, page_size), 100)}
    path = "/api/user/search"
    if keyword.strip():
        params["keyword"] = keyword.strip()
    else:
        path = "/api/user/"
    body = await admin_request("GET", path, params=params)
    data = _data_of(body)
    return {"items": data.get("items") or [], "total": data.get("total", len(data.get("items") or []))}


async def admin_get_user(uid: int) -> dict:
    """按 uid 取单个用户（管理员 GET /api/user/:id）。返回 new-api 用户对象（含 quota/used_quota）。

    用于「我的积分」等只读展示：直接走 env 管理员服务凭证（与管理后台同源），
    **不依赖用户自身会话里的 PAT**——用户 PAT 可能在 new-api 官方前端被
    「系统访问令牌」作废旧值、或被其他共用账号的业务互踢，导致 401 白屏。
    按 uid 精准读取指定用户余额，且需登录态（攻击者无法读他人 uid）。
    """
    body = await admin_request("GET", f"/api/user/{int(uid)}")
    return _data_of(body)


async def admin_resolve_uid_by_username(username: str) -> int:
    """按 new-api 用户名精确解析成 uid（素材点对点共享指定目标用）。

    走管理员 GET /api/user/search?keyword=username 搜索，精确匹配 username 字段
    （new-api 用户名唯一）。找不到抛 NewApiError(404)。
    """
    username = (username or "").strip()
    if not username:
        raise NewApiError("用户名不能为空", 400)
    body = await admin_request("GET", "/api/user/search",
                               params={"keyword": username, "p": 1, "page_size": 100})
    data = _data_of(body)
    for it in (data.get("items") or []):
        if it.get("username") == username:
            return int(it["id"])
    raise NewApiError(f"未找到用户 {username}", 404)


async def admin_get_user_logs(uid: int, page: int = 1, page_size: int = 10) -> dict:
    """按 uid 拉用户调用日志（管理员 GET /api/log/?user_id=）。

    形状与 /api/log/self 一致（{items,total}），用于用户个人 PAT 失效时
    「我的积分」消费记录降级展示。new-api 全站日志支持 user_id 过滤。
    """
    params = {"p": max(1, page), "page_size": min(max(1, page_size), 100),
              "type": 0, "user_id": int(uid)}
    body = await admin_request("GET", "/api/log/", params=params)
    data = _data_of(body)
    return {"items": data.get("items") or [], "total": data.get("total", len(data.get("items") or []))}


# ---------- 管理员：渠道域（契约见头部注释 #9-#15，均已实测）----------
async def admin_list_channels(page: int = 1, page_size: int = 20) -> dict:
    """渠道列表。返回 {items, total}。**items 含 key 字段，前端必须白名单输出**。"""
    params = {"p": max(1, page), "page_size": min(max(1, page_size), 100)}
    body = await admin_request("GET", "/api/channel/", params=params)
    data = _data_of(body)
    return {"items": data.get("items") or [], "total": data.get("total", len(data.get("items") or []))}


async def admin_create_channel(payload: dict) -> None:
    """新增渠道（透传上游字段，如 type/name/key/base_url/models/groups...）。

    注意：payload 里的 models 需为**逗号分隔字符串**（与上游存储一致）。
    """
    await admin_request("POST", "/api/channel/", json=payload)


async def admin_update_channel(channel_id: int, payload: dict) -> None:
    """更新渠道。上游用 PUT /api/channel/ 且 id 在 body 内。"""
    body = {"id": int(channel_id), **payload}
    await admin_request("PUT", "/api/channel/", json=body)


async def admin_delete_channel(channel_id: int) -> None:
    await admin_request("DELETE", f"/api/channel/{int(channel_id)}")


async def admin_set_channel_status(channel_id: int, enabled: bool) -> None:
    """启用/停用渠道。契约（已实测）：POST /api/channel/{id}/status，
    body {"status": 1|2}，只允许这两个值；返回 data=changed(bool)。
    """
    await admin_request("POST", f"/api/channel/{int(channel_id)}/status",
                        json={"status": 1 if enabled else 2})


async def admin_test_channel(channel_id: int) -> dict:
    """渠道连通测试。契约（已实测路由）：GET /api/channel/test/{id}。

    会触发上游真实请求（可能产生费用/触发上游限流），仅在有明确意图时调用。
    失败时 request() 会把 success:false 转成 NewApiError（message 可直接展示）。
    """
    body = await admin_request("GET", f"/api/channel/test/{int(channel_id)}")
    return _data_of(body)


async def admin_enabled_models() -> list:
    """启用渠道的模型名数组 —— 创作站「可用模型目录」。

    契约（已实测）：GET /api/channel/models_enabled → data 为启用渠道的
    模型名数组（含 doubao-seedance-* 等视频模型）。一次拿全，无需翻页聚合。
    """
    body = await admin_request("GET", "/api/channel/models_enabled")
    outer = body if isinstance(body, dict) else {}
    data = outer.get("data")
    if not isinstance(data, list):
        return []
    return [str(m).strip() for m in data if str(m).strip()]


# ---------- 用户态 ----------
async def get_self(pat: str, uid: int) -> dict:
    body = await request("GET", "/api/user/self", headers=user_headers(pat, uid))
    return body["data"]


async def list_tokens(pat: str, uid: int, page: int = 1, size: int = 100) -> dict:
    body = await request("GET", "/api/token/", headers=user_headers(pat, uid),
                         params={"p": page, "size": size})
    return body["data"]


async def create_token(pat: str, uid: int, name: str) -> None:
    await request("POST", "/api/token/", headers=user_headers(pat, uid),
                  json={"name": name, "remain_quota": 0, "expired_time": -1,
                        "unlimited_quota": True, "model_limits_enabled": False,
                        "model_limits": "", "allow_ips": "", "group": ""})


async def get_token_key(pat: str, uid: int, token_id: int) -> str:
    body = await request("POST", f"/api/token/{token_id}/key", headers=user_headers(pat, uid))
    return body["data"]["key"]


async def delete_token(pat: str, uid: int, token_id: int) -> None:
    await request("DELETE", f"/api/token/{token_id}", headers=user_headers(pat, uid))


# ---------- 用户态 / 管理员：聊天用 API Key（sk-）发放 ----------
# 关键事实：new-api 的 OpenAI 兼容端点 /v1/chat/completions 只认 API Key(sk-)，
# 不认 PAT（PAT 仅用于管理类 /api/* 接口）。因此「用户免 Key 聊天」必须由 BFF
# 代用户发放一个归属该用户的 sk-，聊天计费落到该用户配额，前端永不可见 Key。
# 明文 sk- 仅创建后一次返回，之后网关不再回显，故 BFF 必须自己持久化。

async def _find_user_token_id(pat: str, uid: int, name: str) -> "int | None":
    """new-api 当前版本创建 Key 后仅返回 {"success":true}（无 data），
    明文 sk- 与 id 都不在创建响应里，只能从列表按唯一 name 反查 id。"""
    try:
        lst = await request("GET", "/api/token/", headers=user_headers(pat, uid),
                            params={"p": 1, "size": 100})
    except NewApiError:
        return None
    items = (lst.get("data") or {}).get("items") or []
    for it in items:
        if it.get("name") == name:
            return it.get("id")
    return None


async def mint_user_api_key(pat: str, uid: int) -> str:
    """用户态：给自己建一个 API Key(sk-)，返回明文 key。

    new-api 当前版本 POST /api/token/ 仅返回 {"success":true}（无 data），
    明文 sk- 需再 POST /api/token/{id}/key 获取。故流程改为：
    创建（按唯一 name）→ 列表反查 id → POST /{id}/key 取明文。
    key 归属该用户，聊天走其配额。
    """
    name = f"flovart-bff-chat-{uid}"
    tid = await _find_user_token_id(pat, uid, name)
    if tid is None:
        await request("POST", "/api/token/", headers=user_headers(pat, uid),
                      json={"name": name, "remain_quota": 0, "expired_time": -1,
                            "unlimited_quota": True, "model_limits_enabled": False,
                            "model_limits": "", "allow_ips": "", "group": ""})
        tid = await _find_user_token_id(pat, uid, name)
    if tid is None:
        raise NewApiError("网关创建 API Key 未返回 id", 502)
    kb = await request("POST", f"/api/token/{tid}/key", headers=user_headers(pat, uid))
    key = (kb.get("data") or {}).get("key")
    if not key:
        raise NewApiError("网关未返回 API Key 明文", 502)
    return key


async def admin_mint_user_api_key(uid: int) -> str:
    """管理员代建：以管理员凭证为该 uid 生成 sk-（New-Api-User 隔离）。

    兜底用（用户 PAT 已失效、无法自建房时）：无需用户 PAT，直接以管理员
    凭证为该 uid 生成 sk-。同样适配 new-api「创建不返回明文」的新契约：
    创建（按唯一 name）→ 列表反查 id → POST /{id}/key 取明文。
    """
    name = f"flovart-bff-chat-{uid}"
    async def _find() -> "int | None":
        try:
            lst = await admin_request("GET", "/api/token/", params={"p": 1, "size": 100})
        except NewApiError:
            return None
        items = (lst.get("data") or {}).get("items") or []
        for it in items:
            if it.get("name") == name:
                return it.get("id")
        return None
    tid = await _find()
    if tid is None:
        await admin_request("POST", "/api/token/",
                            json={"user_id": int(uid), "name": name, "remain_quota": 0,
                                  "expired_time": -1, "unlimited_quota": True,
                                  "model_limits_enabled": False, "model_limits": "",
                                  "allow_ips": "", "group": ""})
        tid = await _find()
    if tid is None:
        raise NewApiError("网关创建 API Key 未返回 id", 502)
    kb = await admin_request("POST", f"/api/token/{tid}/key")
    key = (kb.get("data") or {}).get("key")
    if not key:
        raise NewApiError("网关未返回 API Key 明文", 502)
    return key


async def admin_ensure_user_api_key(uid: int, name: str | None = None) -> dict:
    """管理员代用户确保有一把默认平台 Key，返回 {id, name, key, status}。

    用于用户 PAT 失效时的降级：不依赖用户自身 PAT，直接以管理员凭证为该
    uid 创建/复用 token 并取明文 key（admin_request 自带 401 重登自愈，
    故管理员通道本身可靠）。name 含 uid（flovart-bff-platform-{uid}），
    以便在管理员视角的全站 token 列表中唯一匹配到该用户（普通 GET /api/token/
    返回全站 token，同名会误匹配他人）。
    """
    if name is None:
        name = f"flovart-bff-platform-{uid}"
    async def _find() -> "dict | None":
        try:
            lst = await admin_request("GET", "/api/token/", params={"p": 1, "size": 100})
        except NewApiError:
            return None
        items = (lst.get("data") or {}).get("items") or []
        for it in items:
            if it.get("name") == name:
                return it
        return None
    tok = await _find()
    if tok is None:
        await admin_request("POST", "/api/token/",
                            json={"user_id": int(uid), "name": name, "remain_quota": 0,
                                  "expired_time": -1, "unlimited_quota": True,
                                  "model_limits_enabled": False, "model_limits": "",
                                  "allow_ips": "", "group": ""})
        tok = await _find()
    if tok is None:
        raise NewApiError("网关创建 API Key 未返回 id", 502)
    kb = await admin_request("POST", f"/api/token/{tok['id']}/key")
    key = (kb.get("data") or {}).get("key")
    if not key:
        raise NewApiError("网关未返回 API Key 明文", 502)
    return {"id": tok["id"], "name": tok.get("name") or name,
            "key": key, "status": tok.get("status", 1)}


async def get_logs(pat: str, uid: int, page: int, page_size: int) -> dict:
    body = await request("GET", "/api/log/self", headers=user_headers(pat, uid),
                         params={"p": page, "page_size": page_size, "type": 0})
    return body["data"]


async def get_log_stat(pat: str, uid: int) -> dict:
    body = await request("GET", "/api/log/self/stat", headers=user_headers(pat, uid),
                         params={"type": 0})
    return body["data"]
