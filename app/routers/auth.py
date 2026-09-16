"""认证与用户域：注册（影子建号+赠送+自动登录）/ 登录 / 登出 / self / 站点配置。

流程与 hewapi-bff 一致：BFF 不落用户密码 —— 登录密码只用于当场向 new-api
换 PAT；影子建号密码只发往 new-api。会话 Cookie 里装 PAT（AES-256-GCM 加密）。
"""
import logging
import re

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from .. import config, newapi_client as na, promo
from ..resp import client_ip, fail, ok
from ..security import require_session, set_session, clear_session, is_admin
from ..newapi_client import NewApiError

logger = logging.getLogger("bff.auth")

router = APIRouter()

# new-api 对 User.Password 有 max 校验（hewapi 实测 20 位通过、24 位失败）。
# BFF 层先拦，避免用户拿到英文 validation 报错。
MAX_PASSWORD_LEN = 20
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{2,20}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# quota → 积分换算别名
P = config.quota_to_points


class LoginBody(BaseModel):
    username: str
    password: str


class RegisterBody(BaseModel):
    username: str
    password: str
    display_name: str = ""


def _valid_username(username: str) -> bool:
    return bool(_USERNAME_RE.match(username))


@router.get("/api/config")
async def get_site_config():
    """站点配置：品牌、接入参数、积分口径、功能开关。

    创作站前端启动时拉一次（同时作为「hosted 模式」探测信号），
    换品牌/域名/赠送档位都只改环境变量，不动前端代码。
    """
    return ok({
        "service": config.SERVICE_NAME,
        "brand": {
            "name": config.BRAND_NAME,
            "tagline": config.BRAND_TAGLINE,
            "icp": config.BRAND_ICP,
            "contact": config.BRAND_CONTACT,
        },
        "api": {
            "base_url": config.API_BASE_URL,   # new-api OpenAI 兼容端点（前端 Provider 用）
        },
        "points": {
            "unit": config.POINTS_UNIT_NAME,
            "per_cny": config.POINTS_PER_CNY,
        },
        "features": {
            "hosted": True,
            "signup_bonus_enabled": config.PROMO_SIGNUP_ENABLED,
            "signup_bonus_points": int(config.PROMO_SIGNUP_POINTS or 0),
        },
        "version": config.APP_VERSION,
    })


@router.post("/api/user/login")
async def login(body: LoginBody, request: Request, response: Response):
    request_ip = client_ip(request)
    username = body.username.strip()
    if not username or not body.password:
        return fail("用户名和密码不能为空")
    try:
        info = await na.login(username, body.password, client_ip=request_ip)
    except NewApiError as e:
        if e.status_code == 429:      # 上游限流，原样透传含等待时长的提示
            return fail(e.message, 429)
        msg = e.message
        if "password" in msg.lower() or "用户名或密码" in msg or e.status_code == 400:
            msg = "用户名或密码错误"
        return fail(msg, 401 if e.status_code in (400, 401) else e.status_code)
    role = int((info.get("user") or {}).get("role") or 0)
    set_session(response, {"uid": info["uid"], "username": info["username"],
                           "pat": info["pat"], "role": role})
    return ok({"username": info["username"], "role": role}, "登录成功")


@router.post("/api/user/register")
async def register(body: RegisterBody, request: Request, response: Response):
    """注册 = 管理员影子建号 + 注册赠送 + 自动登录。

    用户名即 new-api 用户名（唯一约束交给上游）。新账号由管理员接口建，
    建出来就是普通用户（role=1），管理权限只来自 admin 账号登录。
    """
    username = body.username.strip()
    if not _valid_username(username):
        return fail("用户名需为 2-20 位字母、数字或下划线")
    if not 8 <= len(body.password) <= MAX_PASSWORD_LEN:
        return fail(f"密码需为 8-{MAX_PASSWORD_LEN} 位")
    display = body.display_name.strip() or username
    try:
        uid = await na.admin_create_user(username, body.password, display)
    except NewApiError as e:
        msg = e.message
        low = msg.lower()
        if "已存在" in msg or "exist" in low or "duplicate" in low:
            return fail("用户名已存在，请直接登录")
        return fail(msg, e.status_code)

    # 赠送失败不阻塞注册（promo 内部已回滚占位并记日志）
    gift = await promo.grant_signup(uid)

    try:
        info = await na.login(username, body.password, client_ip=client_ip(request))
    except NewApiError as e:
        # 建号成功但登录失败（罕见）：账号已存在可直接登录，不删号。
        logger.warning("注册后自动登录失败 uid=%s: %s", uid, e.message)
        return fail(e.message, e.status_code)
    role = int((info.get("user") or {}).get("role") or 0)
    set_session(response, {"uid": info["uid"], "username": info["username"],
                           "pat": info["pat"], "role": role})
    msg = f"注册成功，已赠送 {gift:,} {config.POINTS_UNIT_NAME}" if gift else "注册成功"
    return ok({"username": info["username"], "gift_points": gift, "role": role}, msg)


@router.get("/api/user/logout")
async def logout(response: Response):
    clear_session(response)
    return ok(None, "已退出登录")


def _self_payload(d: dict, session: dict) -> dict:
    """self 载荷：积分口径换算 + 管理入口标记（真正鉴权在 require_admin）。"""
    email = (d.get("email") or "").strip()
    if email.endswith("@example.com") or email.startswith("rc_"):
        email = ""  # 影子建号的上游占位邮箱，不展示
    return {
        "id": d.get("id", session["uid"]),
        "username": d.get("username", session["username"]),
        "display_name": d.get("display_name") or d.get("username", session["username"]),
        "email": email or "-",
        "points": P(d.get("quota", 0)),
        "used_points": P(d.get("used_quota", 0)),
        "request_count": d.get("request_count", 0),
        "group": d.get("group", "default"),
        "unit": config.POINTS_UNIT_NAME,
        "points_per_cny": config.POINTS_PER_CNY,
        "api_base_url": config.API_BASE_URL,
        # 仅用于前端显示管理入口；把该字段改成 true 也调不通 /api/console/*
        "is_admin": is_admin(session),
        "role": session.get("role", 0),
    }


@router.get("/api/user/self")
async def user_self(session: dict = Depends(require_session)):
    uid = session["uid"]
    try:
        d = await na.get_self(session["pat"], uid)
    except na.NewApiError as e:
        if e.status_code != 401:
            raise
        # 用户 PAT 在 new-api 侧失效（官方前端点一次「系统访问令牌」即作废旧值），
        # 降级到管理员代读：uid 来自加密会话 Cookie（可信），不会越权读他人。
        # 这样刷新页面不再因 PAT 失效而 401 跳登录。
        logger.warning("user PAT 失效，降级 admin_get_user uid=%s", uid)
        d = await na.admin_get_user(uid)
    return ok(_self_payload(d, session))
