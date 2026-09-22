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
import asyncio
import json as _json
import logging
import os
import re
import time
from typing import Any, Optional

import httpx

from . import config, observability

logger = logging.getLogger("bff.newapi")


class NewApiError(Exception):
    """new-api 返回业务失败或网络错误。message 可直接展示给用户。

    detail：底层真实原因（异常类型+原文 / 上游响应预览），只进请求日志
    （request_log.result.error.detail）与服务器日志，不直接展示给用户——
    此前网络层异常只留通用文案，日志里查不到具体原因（2026-09-18 图生图 502 血案）。
    """

    def __init__(self, message: str, status_code: int = 502, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail


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


def _save_admin_cred(*, force: bool = False) -> None:
    """PAT 落盘。失败不影响主流程（只是下次冷启多消耗一个会话）。

    ⭐ 2026-09-20 多应用共存改造：force=True 时**无视 env 直供也必须落盘** ——
    兜底轮换出的新 PAT 是共享凭据文件（单一事实来源）的最新值，不落盘的话
    另一个共用该账号的 BFF 永远拿不到，会触发「轮换乒乓」（两边互相作废对方 PAT）。
    env 模式的冗余保存仍走默认 force=False（避免无谓写文件）。
    """
    if config.NEWAPI_ADMIN_PAT and not force:
        return  # env 直供且非强制时无需落盘
    try:
        os.makedirs(os.path.dirname(config.ADMIN_CRED_FILE), exist_ok=True)
        tmp = config.ADMIN_CRED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump({"pat": _admin_cache["pat"], "uid": _admin_cache["uid"]}, f)
        os.replace(tmp, config.ADMIN_CRED_FILE)
        os.chmod(config.ADMIN_CRED_FILE, 0o600)  # PAT 等同管理员密码
    except OSError as e:
        logger.warning("save admin cred failed: %s", e)


def _reload_admin_cred() -> bool:
    """从共享凭据文件重读 PAT；发现与内存不同的新值则采纳并返回 True。

    ⭐ 401 自愈第一步（2026-09-20 多应用共存）：共用同一管理员账号的另一个 BFF
    可能刚完成兜底轮换并把新 PAT 落了盘 —— 此时本进程重读文件即可拿到新值，
    **绝不能再走 login 轮换**（否则会把对方刚换的 PAT 又作废，形成乒乓循环，
    每轮白白消耗一个会话 + 签发额度，直到 50 会话/100 签发双限打爆）。
    磁盘值优先于 env/内存旧值：env 只在冷启时作为初始猜测，401 即证明它已失效。
    """
    try:
        with open(config.ADMIN_CRED_FILE, "r", encoding="utf-8") as f:
            d = _json.load(f)
        pat, uid = d.get("pat"), d.get("uid")
        if pat and uid and pat != _admin_cache["pat"]:
            _admin_cache["pat"] = pat
            _admin_cache["uid"] = int(uid)
            logger.warning(
                "admin PAT updated from shared cred file (peer instance rotated) uid=%s",
                _admin_cache["uid"])
            return True
    except (OSError, ValueError, KeyError):
        pass
    return False


# 兜底轮换单飞锁：并发 401 时只放一个协程去 login，其余等锁后先重读文件（双检）。
# asyncio.Lock 自 3.10 起不再绑定事件循环，模块级懒创建安全。
_LOGIN_LOCK: "asyncio.Lock | None" = None
# 最近一次兜底轮换的 monotonic 时间戳（2026-09-22 三应用共用 uid=1 互踢熔断）。
#    跨机 + 读回失效场景下，A/B/C 谁轮换谁踢别人 → 无限乒乓烧穿会话/签发额度。
#    冷静期内拒绝再次轮换（见 _self_heal_admin_cred），把乒乓压成有界抖动。
#    ⚠️ 必须用 None 表示「本进程从未轮换过」：monotonic() 是系统开机以来的秒数，
#    新拉起的容器/全新 CI runner 上该值可能只有几十秒 —— 若初始化成 0.0，
#    `monotonic() - 0.0` 会被误判成「刚轮换过」，进程启动头一个冷静期内
#    的 401 全部被错误熔断（2026-09-22 CI 全红 + 重启后 15 分钟不可自愈的真凶）。
_LAST_ADMIN_ROTATE: "float | None" = None
# 🔴 「401 但不是令牌值错」的上游鉴权码（new-api dashboard 链路，2026-09-22 语义分流）。
#    这些 401 的根因是账号状态（被封禁/用户信息非法/会话被吊销），轮换 PAT 救不了，
#    只会白踢共用账号的其他应用（互踢点火源之一）。命中即拒绝自愈轮换、503 转人工。
#    AUTH_UNAUTHORIZED / AUTH_TOKEN_EXPIRED（令牌值问题）不在内 —— 正常轮换。
#    未知码/旧版上游无 code → 维持旧行为（尝试自愈），由冷静期兜底限频。
NO_ROTATE_AUTH_CODES = frozenset({
    "AUTH_USER_DISABLED",     # 账号被封禁
    "AUTH_USER_INVALID",      # 用户信息非法
    "AUTH_SESSION_REVOKED",   # 登录会话被吊销（非 PAT 值问题）
})


def _auth_code_from_error(e: "NewApiError") -> str:
    """从 401 错误 detail 里提取上游结构化鉴权码（upstream_auth_code=AUTH_*），无则空串。"""
    m = re.search(r"upstream_auth_code=([A-Z_]+)", getattr(e, "detail", "") or "")
    return m.group(1) if m else ""


def _login_lock() -> "asyncio.Lock":
    global _LOGIN_LOCK
    if _LOGIN_LOCK is None:
        _LOGIN_LOCK = asyncio.Lock()
    return _LOGIN_LOCK


async def _self_heal_admin_cred(reject_code: str = "") -> None:
    """PAT 401 后的自愈序列（多应用共存根治，2026-09-20；跨机版见 READBACK）：

    0. 🔴 语义分流（2026-09-22）：上游 401 带结构化 code 且明确指向**账号状态**问题
       （封禁/用户信息非法/会话吊销）时，轮换 PAT 救不了、只会白踢共用账号的其他
       应用 → 直接拒绝自愈、503 转人工（互踢点火源之一，见 NO_ROTATE_AUTH_CODES）。
    1. 重读凭据文件 —— 同机多实例（蓝绿/灰度）场景对端可能已轮换并落盘，直接采纳即可；
    2. 文件无新值 → 锁内再查（等锁期间同进程其他协程可能已治愈）；
    3. ⭐ 读回恢复（NEWAPI_ADMIN_PAT_READBACK=1，默认开，跨服务器部署的关键）：
       login 拿会话后 **GET /api/user/self 把账号当前 access_token 原样读回来**——
       读操作不轮换 token，另一台服务器上共用该账号的 BFF 的 PAT 依然有效，
       从根上消灭「轮换乒乓」（乒乓只在同账号跨机场景，共享文件帮不上忙）；
    4. 读回失败（账号压根没设过 access_token）才最后兜底轮换
      （NEWAPI_ADMIN_LOGIN_FALLBACK=0 可彻底禁用 3/4 步转人工）。
    3.5. 🔴 轮换冷静期（NEWAPI_ADMIN_ROTATE_COOLDOWN，默认 900s，2026-09-22 三应用
      共用 uid=1 血案）：读回失效的上游（/api/user/self 不回 access_token）+ 跨机
      无共享文件时，A/B/C 谁兜底轮换谁踢别人 → 无限乒乓。冷静期内已轮换过仍 401
      → 拒绝再轮换、503 转人工，把乒乓压成「每实例每窗口至多一次」。
    """
    # 第 0 步：账号状态类 401 —— 轮换救不了，绝不轮换（否则白踢共用账号的其他应用）
    if reject_code in NO_ROTATE_AUTH_CODES:
        logger.error(
            "admin 401 code=%s 指向账号状态问题而非 PAT 失效，拒绝自愈轮换"
            "（轮换不会修复且会作废共用该账号的其他应用 PAT）——"
            "请到 new-api 后台核对该管理员账号状态/会话", reject_code)
        raise NewApiError("管理员账号状态异常（非凭证失效），请稍后重试或联系管理员", 503)
    pat_before = _admin_cache["pat"]
    if _reload_admin_cred():
        return
    if not config.NEWAPI_ADMIN_LOGIN_FALLBACK:
        logger.error(
            "admin PAT 401 且凭据文件无新值，兜底登录已禁用"
            "(NEWAPI_ADMIN_LOGIN_FALLBACK=0) —— 需人工更新 PAT 或凭据文件。")
        raise NewApiError("服务暂时不可用，请稍后重试或联系客服", 503)
    async with _login_lock():
        # 双检①：等锁期间同进程其他协程可能已完成治愈（缓存 PAT 已变）
        if _admin_cache["pat"] != pat_before:
            return
        # 双检②：同机另一进程可能刚轮换并落盘
        if _reload_admin_cred():
            return
        # 第 3 步：读回恢复（不轮换，跨机安全）
        if config.NEWAPI_ADMIN_PAT_READBACK:
            try:
                if await _recover_admin_pat_by_readback():
                    return
            except NewApiError as e:
                logger.warning("admin PAT readback failed: %s", e.message)
        # 第 3.5 步：轮换冷静期熔断（2026-09-22 三应用共用 uid=1 互踢血案）。
        #   读回已失败 + 冷静期内本进程轮换过 → 此刻再轮换几乎必然踢掉共用同
        #   一账号的对端，触发乒乓。宁可 503 转人工也不烧互踢循环。
        last_rotate = _LAST_ADMIN_ROTATE
        if config.NEWAPI_ADMIN_ROTATE_COOLDOWN > 0 and last_rotate is not None:
            since = time.monotonic() - last_rotate
            if since < config.NEWAPI_ADMIN_ROTATE_COOLDOWN:
                logger.error(
                    "admin PAT 401 且读回/凭据文件均无法自愈，但 %.0fs 前刚兜底轮换过"
                    "（冷静期 %ds 内拒绝再轮换）。大概率是多应用共用管理员账号互踢："
                    "继续轮换只会作废对端 PAT 形成乒乓。请人工处理——① 给每个业务"
                    "配独立 new-api 管理员账号（根治）；或 ② 在 new-api 后台取当前"
                    "有效 access_token 更新各实例 NEWAPI_ADMIN_PAT 后重启。",
                    since, config.NEWAPI_ADMIN_ROTATE_COOLDOWN)
                raise NewApiError("服务暂时不可用，请稍后重试或联系客服", 503)
        # 第 4 步：最后兜底——真轮换（会作废其他机器/对端的 PAT）
        await _admin_login()


async def _admin_session_login() -> tuple[int, str, dict]:
    """账密登录 new-api，返回 (uid, session_access_token, session_info)。

    只创建会话、**不轮换 access_token**。调用方用完必须 _release_session 归还。
    登录失败一次即抛（不做任何重试——会话签发额度按账号计，绝不自动重试）。
    """
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
    return int(data["user"]["id"]), data["access_token"], data.get("session") or {}


async def _recover_admin_pat_by_readback() -> bool:
    """读回恢复：登录后用 GET /api/user/self **读取**账号当前 access_token。

    ⭐ 与 _admin_login 的本质区别：不调 GET /api/user/token（那个动作会轮换并作废
    旧值）。读回来的 token 是账号现行有效值——其他机器/实例上用同一账号的 BFF
    不受任何影响。这是跨服务器部署（无共享卷可用）下避免互踢的核心手段。
    返回 True 表示已采纳新凭据；False 表示账号未设置过 access_token（需走轮换）。
    """
    uid, access_token, session = await _admin_session_login()
    try:
        body = await request("GET", "/api/user/self",
                             headers=user_headers(access_token, uid))
    finally:
        # 无论读回成败都立刻归还登录会话（会话是稀缺资源）
        try:
            await _release_session(access_token, uid, session)
        except Exception:  # noqa: BLE001 归还失败不影响主流程
            pass
    pat = (body.get("data") or {}).get("access_token") or ""
    if not pat:
        logger.warning("admin account has no access_token set; readback empty")
        return False
    _admin_cache["pat"] = pat
    _admin_cache["uid"] = uid
    _save_admin_cred(force=True)
    logger.warning("admin PAT recovered by readback (no rotation) uid=%s", uid)
    return True


async def _admin_login() -> None:
    """管理员登录轮换 PAT，换完立刻归还会话（避免占满 50 会话上限）。

    ⚠️ 只作最后兜底：GET /api/user/token 会**轮换并作废旧 access_token**，
    会让其他共用该账号的 BFF/实例立刻 401。401 自愈序列见 _self_heal_admin_cred
    （先读回、后轮换）。
    """
    uid, access_token, session = await _admin_session_login()
    try:
        pat = await _mint_access_token(access_token, uid, config.NEWAPI_ADMIN_PASSWORD)
    finally:
        try:
            await _release_session(access_token, uid, session)
        except Exception:  # noqa: BLE001
            pass
    _admin_cache["pat"] = pat
    _admin_cache["uid"] = uid
    # ⭐ 强制落盘：新 PAT 是凭据文件的最新事实，env 直供模式也必须写（同机其他
    #   实例 401 时重读文件即可自愈）。跨机同步靠读回恢复（见 _self_heal_admin_cred）。
    _save_admin_cred(force=True)
    # 盖轮换时间戳（冷静期熔断用）：冷启无凭证的首次轮换同样会踢对端，一并计入。
    _LAST_ADMIN_ROTATE = time.monotonic()


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
        # PAT 被外部轮换掉了（官方前端点一次「系统访问令牌」就会作废旧值）。
        # ⭐ 2026-09-20 根治互踢：先重读共享凭据文件自愈，实在不行才锁内兜底轮换 ——
        #   绝不盲目 login（盲目轮换会作废对端 PAT，两边乒乓直到会话/额度双爆）。
        # ⭐ 2026-09-22 语义分流：上游 401 code 指向账号状态问题（非令牌值错）时
        #   直接拒绝轮换转人工 —— 这是「PAT 明明好好的却被当作旧了」的点火源之一。
        auth_code = _auth_code_from_error(e)
        logger.warning("admin PAT rejected (401), self-healing cred (auth_code=%s)", auth_code)
        await _self_heal_admin_cred(auth_code)
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
        # 同 admin_request：先共享凭据自愈，再锁内兜底轮换（多应用共存根治）；
        # 账号状态类 401（auth_code 命中 NO_ROTATE_AUTH_CODES）拒绝轮换转人工。
        auth_code = _auth_code_from_error(e)
        logger.warning("admin PAT rejected (401) in request_as_user, self-healing cred (auth_code=%s)", auth_code)
        await _self_heal_admin_cred(auth_code)
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
    except httpx.HTTPError as e:
        raise NewApiError("上游服务暂时不可用，请稍后重试", 502,
                          detail=f"{type(e).__name__}: {e}") from e
    return _parse_response(resp, method, path)


def _parse_response(resp: "httpx.Response", method: str, target: str) -> Any:
    """统一解析网关响应：网络/业务失败抛 NewApiError，data 原样返回。target 用于日志（path 或完整 URL）。"""
    if resp.status_code == 401:
        # 401 的语义要分场景，否则会给出**误导性**提示：
        # - 打 new-api（target 是相对 path 或指向 NEWAPI_BASE_URL）→ 会话 PAT 失效，提示重新登录是对的。
        # - 打外部网关（target 是完整 http(s) URL，如 https://api.chatfire.cn/v1/...）→ 是【该服务的
        #   API Key 无效】，跟 BFF 登录态毫无关系。此前一律回「凭证已失效，请重新登录」，
        #   用户被误导去找登录问题（飞哥 2026-09-11 反馈普通用户生图失败即此情形）。
        # 上游 401 带结构化鉴权码（new-api dashboard 链路：AUTH_UNAUTHORIZED/
        # AUTH_TOKEN_EXPIRED/AUTH_USER_DISABLED/AUTH_SESSION_REVOKED/AUTH_USER_INVALID）。
        # 带出去给 admin 401 处理链判断「是不是 PAT 值真的错了」——只有令牌值问题才值得轮换。
        try:
            code = str(resp.json().get("code") or "")
        except ValueError:
            code = ""
        if target.startswith(("http://", "https://")):
            raise NewApiError("AI 服务的 API Key 无效或无权限，请在「AI 服务设置」中检查该服务的 Key", 401,
                              detail=f"upstream_auth_code={code}")
        raise NewApiError("凭证已失效，请重新登录", 401, detail=f"upstream_auth_code={code}")
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
        preview = f"status={resp.status_code} content_type={content_type} body_preview={text[:300]}"
        if "text/html" in content_type:
            raise NewApiError(
                f"网关未实现该图片端点（{target} 返回了前端页面而非 JSON）。"
                f"请确认网关已落地图片接口，或核对同步/异步端点路径配置。",
                502, detail=preview,
            )
        raise NewApiError("上游返回异常（非 JSON）", 502, detail=preview)
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
    except httpx.HTTPError as e:
        raise NewApiError("上游服务暂时不可用，请稍后重试", 502,
                          detail=f"{type(e).__name__}: {e}") from e
    return _parse_response(resp, method, url)


def _data_of(body: Any) -> dict:
    """解包上游响应：兼容外层带 data 与已是内层两种形状，恒返回 dict。"""
    outer = body if isinstance(body, dict) else {}
    inner = outer.get("data")
    return inner if isinstance(inner, dict) else outer


# ---------- 认证（用户态）----------
async def _mint_access_token(session_token: str, uid: int, password: str, *,
                             client_ip: str | None = None) -> str:
    """换取账号的系统访问令牌（PAT），兼容新旧网关。

    ⭐ new-api v1.0.0-rc.37 起引入「安全验证 proof」（middleware/secure_verification.go）：
    GET /api/user/token（scope=access_token.generate）**无条件**要求 X-Security-Proof 头，
    缺失即 403「需要安全验证」。无 2FA/Passkey 的账号唯一可用方式是 **password**：
    先 POST /api/verify（密码换一次性 proof），再带 proof 换 PAT。
    proof 与登录会话绑定（SessionID），必须在同一会话内先消费、后归还会话。

    旧版网关没有 /api/verify（未知路由兜底成 SPA HTML → 502），此时跳过 proof
    直接换——旧网关本来就不拦。除 404/502 外的验证失败（如密码校验不过）绝不重试，
    原样抛给调用方（该端点有失败计数，重试会锁号）。
    """
    headers = user_headers(session_token, uid)
    proof = ""
    try:
        body = await request("POST", "/api/verify", headers=headers,
                             json={"method": "password",
                                   "scope": "access_token.generate",
                                   "password": password},
                             client_ip=client_ip)
        proof = (body.get("data") or {}).get("proof_token") or ""
        if not proof:
            logger.warning("verify returned empty proof_token; minting without proof")
    except NewApiError as e:
        if e.status_code in (404, 502):
            logger.info("gateway has no /api/verify (pre-rc.37), minting without proof")
        else:
            raise
    if proof:
        headers = {**headers, "X-Security-Proof": proof}
    pat_body = await request("GET", "/api/user/token", headers=headers,
                             client_ip=client_ip)
    return pat_body["data"]


async def login(username: str, password: str, client_ip: str | None = None) -> dict:
    """密码登录 → 换 PAT → **立刻归还会话**。返回 {uid, username, pat, user}。

    归还会话是硬性要求（见模块 docstring）：BFF 只需要 PAT、不用会话，
    不归还的话同一账号登满 50 次就永久 409。
    ⚠️ rc.37 起 data.access_token 是 15 分钟短效会话 JWT（非持久 PAT），
    只能用作 proof/换 token 的临时凭证，不可当 PAT 存。
    """
    body = await request("POST", "/api/user/login", headers={},
                         json={"username": username, "password": password},
                         client_ip=client_ip)
    data = body["data"]
    access_token = data["access_token"]
    user = data["user"]
    uid = user["id"]
    pat = await _mint_access_token(access_token, uid, password, client_ip=client_ip)
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

    ⚠️ 这是【全站】启用模型，**不按用户分组过滤**。若某模型只绑在用户所在分组
    之外的渠道上，它会出现在这个列表里但实际调不通（报
    `No available channel for model X under group <用户分组>`）。
    因此**不要直接把本函数的返回值当「用户可用模型」下发** ——
    请用 user_available_models(sk)，它按该 Key 所属分组返回真实可调用的模型。

    保留本函数用于：需要全站清单的场景（如管理端总览）。
    """
    body = await admin_request("GET", "/api/channel/models_enabled")
    outer = body if isinstance(body, dict) else {}
    data = outer.get("data")
    if not isinstance(data, list):
        return []
    return [str(m).strip() for m in data if str(m).strip()]


async def user_available_models(sk: str) -> list:
    """用【用户自己的 sk-】查网关 /v1/models —— 返回该用户【按其分组】真正可调用的模型。

    契约（已实测）：GET {base}/v1/models，Authorization: Bearer <sk->
    → data 为 [{"id": "<model>", ...}]，**已按该 Key 所属分组过滤**。
    例：default 分组返回 31 个（无图片模型）；keypool 分组才有 doubao-seedream-*。

    这是「模型目录要诚实」的关键：BFF 若下发 admin_enabled_models()（全站 52 个），
    用户会看到一堆自己分组调不通的模型（症状：列表里有、点下去 model_not_found）。

    注意：/v1 只认 sk-（不认 PAT、也不需要 New-Api-User —— key 自归属该用户）。
    """
    body = await request("GET", "/v1/models", headers={"Authorization": f"Bearer {sk}"})
    outer = body if isinstance(body, dict) else {}
    data = outer.get("data")
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        if isinstance(item, dict):
            mid = str(item.get("id") or "").strip()
        else:
            mid = str(item).strip()
        if mid:
            out.append(mid)
    return out



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
