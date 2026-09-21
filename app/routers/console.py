"""管理台（require_admin）：用户 / 渠道 / 模型 三域 + 总览。

所有上游调用走管理员凭证（见 newapi_client 三通道）。BFF 只做鉴权、换算、
归一化与**敏感字段白名单**（用户 password / 渠道 key 一律不下发前端）。
积分增减在出口换算成 quota 再落上游。上游契约为已实测版本（见
newapi_client.py 头部契约注释 #9-#15）；渠道写操作仍建议 M1 收尾时
人工复核一次（涉及真金白银的改动不做自动化冒烟）。
"""
import re

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, Field

from .. import config, cloudstore, image_model_modes, newapi_client as na, promo
from ..resp import fail, ok
from ..security import require_admin

router = APIRouter()

P = config.quota_to_points
MAX_PASSWORD_LEN = 20
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{2,20}$")


# ---------- 总览 ----------
@router.get("/api/console/overview")
async def console_overview(_s: dict = Depends(require_admin)):
    users = await na.admin_list_users(page=1, page_size=1)
    channels = await na.admin_list_channels(page=1, page_size=1)
    models = await na.admin_enabled_models()
    bonus = promo.signup_summary()
    return ok({
        "users": {"total": users["total"]},
        "channels": {"total": channels["total"]},
        "models": {"total": len(models)},
        "points": {"unit": config.POINTS_UNIT_NAME, "per_cny": config.POINTS_PER_CNY},
        "signup": bonus,
        "newapi": {"base_url": config.NEWAPI_BASE_URL,
                   "base_url_is_default": config.NEWAPI_BASE_URL_IS_DEFAULT},
    })


# ---------- 用户 ----------
class ConsoleUserCreate(BaseModel):
    username: str
    password: str
    display_name: str = ""


class ConsoleUserUpdate(BaseModel):
    username: str = ""
    password: str = ""
    display_name: str = ""


class ConsoleUserQuota(BaseModel):
    points: int = Field(..., gt=0)
    mode: str = "add"  # add | subtract


@router.get("/api/console/users")
async def console_users(keyword: str = "", p: int = 1, page_size: int = 10,
                        _s: dict = Depends(require_admin)):
    d = await na.admin_list_users(keyword=keyword, page=p, page_size=page_size)
    items = []
    for u in d["items"]:
        items.append({
            "id": u.get("id"),
            "username": u.get("username", ""),
            "display_name": u.get("display_name") or u.get("username", ""),
            "email": u.get("email", ""),
            "role": u.get("role", 1),
            "status": u.get("status", 1),
            "group": u.get("group", "default"),
            "points": P(u.get("quota", 0)),
            "used_points": P(u.get("used_quota", 0)),
            "request_count": u.get("request_count", 0),
            "created_time": u.get("created_time", 0),
        })
    return ok({"items": items, "total": d["total"], "page": max(1, p),
               "page_size": min(max(1, page_size), 100)})


@router.post("/api/console/users")
async def console_create_user(body: ConsoleUserCreate,
                              _s: dict = Depends(require_admin)):
    username = body.username.strip()
    if not _USERNAME_RE.match(username):
        return fail("用户名需为 2-20 位字母、数字或下划线")
    if not 8 <= len(body.password) <= MAX_PASSWORD_LEN:
        return fail(f"密码需为 8-{MAX_PASSWORD_LEN} 位")
    uid = await na.admin_create_user(username, body.password, body.display_name.strip())
    return ok({"id": uid, "username": username}, "用户创建成功")


@router.put("/api/console/users/{uid}")
async def console_update_user(uid: int, body: ConsoleUserUpdate,
                              _s: dict = Depends(require_admin)):
    """改用户名/密码/昵称。password 为重置后的新密码（必填）。

    上游 PUT /api/user/ 是整体替换（无 password 会把账号密码清空成空串），
    故用户名与密码都必须显式给出 —— 前端编辑时回填当前值即可。
    uid 不变，余额/Key/日志保留。
    """
    username = body.username.strip()
    if not _USERNAME_RE.match(username):
        return fail("用户名需为 2-20 位字母、数字或下划线")
    if not 8 <= len(body.password) <= MAX_PASSWORD_LEN:
        return fail(f"密码需为 8-{MAX_PASSWORD_LEN} 位")
    display = body.display_name.strip() or username
    await na.admin_update_user(uid, username, body.password, display_name=display)
    return ok({"id": uid, "username": username}, "用户已更新")


@router.post("/api/console/users/{uid}/quota")
async def console_user_quota(uid: int, body: ConsoleUserQuota,
                             _s: dict = Depends(require_admin)):
    mode = body.mode.strip().lower()
    if mode not in ("add", "subtract"):
        return fail("mode 需为 add 或 subtract")
    quota = config.points_to_quota(body.points)
    await na.admin_add_quota(uid, quota, mode=mode)
    verb = "增加" if mode == "add" else "扣除"
    return ok(None, f"已{verb} {body.points:,} {config.POINTS_UNIT_NAME}")


@router.delete("/api/console/users/{uid}")
async def console_delete_user(uid: int, _s: dict = Depends(require_admin)):
    await na.admin_delete_user(uid)
    return ok(None, "用户已删除")


# ---------- 渠道（契约已实测，见 newapi_client.py 头部 #10-#12）----------
# 渠道对象含 key / header_override / param_override 等敏感字段，**绝不能原样
# 下发给前端** —— 列表统一白名单输出，models 由逗号分隔字符串拆成数组。
_CHANNEL_VIEW_FIELDS = (
    "id", "type", "name", "status", "group", "base_url", "models",
    "weight", "priority", "tag", "auto_ban", "test_time", "response_time",
    "used_quota", "balance", "created_time", "remark", "channel_info",
    "model_mapping", "status_code_mapping",
)


def _channel_view(ch: dict) -> dict:
    view = {k: ch.get(k) for k in _CHANNEL_VIEW_FIELDS if k in ch}
    raw_models = ch.get("models")
    view["models"] = ([m.strip() for m in str(raw_models).split(",") if m.strip()]
                      if isinstance(raw_models, str) else raw_models)
    return view


@router.get("/api/console/channels")
async def console_channels(p: int = 1, page_size: int = 20,
                           _s: dict = Depends(require_admin)):
    d = await na.admin_list_channels(page=p, page_size=page_size)
    return ok({"items": [_channel_view(ch) for ch in d["items"]],
               "total": d["total"], "page": max(1, p),
               "page_size": min(max(1, page_size), 100)})


def _normalize_models(payload: dict) -> dict:
    """上游渠道的 models 是逗号分隔字符串；容忍前端传数组并归一化。"""
    models = payload.get("models")
    if isinstance(models, list):
        payload = {**payload,
                   "models": ",".join(str(m).strip() for m in models if str(m).strip())}
    return payload


@router.post("/api/console/channels")
async def console_create_channel(payload: dict = Body(...),
                                 _s: dict = Depends(require_admin)):
    await na.admin_create_channel(_normalize_models(payload))
    return ok(None, "渠道创建成功")


@router.put("/api/console/channels/{channel_id}")
async def console_update_channel(channel_id: int, payload: dict = Body(...),
                                 _s: dict = Depends(require_admin)):
    await na.admin_update_channel(channel_id, _normalize_models(payload))
    return ok(None, "渠道已更新")


@router.delete("/api/console/channels/{channel_id}")
async def console_delete_channel(channel_id: int,
                                 _s: dict = Depends(require_admin)):
    await na.admin_delete_channel(channel_id)
    return ok(None, "渠道已删除")


class ChannelStatusBody(BaseModel):
    enabled: bool


@router.post("/api/console/channels/{channel_id}/status")
async def console_channel_status(channel_id: int, body: ChannelStatusBody,
                                 _s: dict = Depends(require_admin)):
    await na.admin_set_channel_status(channel_id, body.enabled)
    return ok(None, "渠道已启用" if body.enabled else "渠道已停用")


@router.post("/api/console/channels/{channel_id}/test")
async def console_channel_test(channel_id: int,
                               _s: dict = Depends(require_admin)):
    data = await na.admin_test_channel(channel_id)
    return ok(data, "测试完成")


# ---------- 模型目录 ----------
@router.get("/api/console/models")
async def console_models(p: int = 1, page_size: int = 50,
                         _s: dict = Depends(require_admin)):
    """可用模型目录：启用渠道的模型名数组（契约已实测，含视频模型）。

    上游 /api/channel/models_enabled 一次返回全量数组，BFF 做内存分页后返回，
    保持与用户/渠道列表一致的 {items,total,page,page_size} 视图。
    """
    p = max(1, p)
    page_size = min(max(1, page_size), 200)
    models = await na.admin_enabled_models()
    total = len(models)
    start = (p - 1) * page_size
    end = start + page_size
    items = models[start:end]
    return ok({"items": items, "total": total, "page": p,
               "page_size": page_size, "unit": config.POINTS_UNIT_NAME})


# ---------- 图片模型同步/异步配置 ----------
class ImageModelModeBody(BaseModel):
    model: str = Field(..., min_length=1, max_length=200)
    mode: str = Field(..., pattern="^(sync|async)$")


@router.get("/api/console/image-models")
async def console_image_models(_s: dict = Depends(require_admin)):
    """图片模型同步/异步配置：列出网关启用模型中的图片类，附当前配置模式。

    未显式配置的模型回退全局 GATEWAY_IMAGE_GEN_MODE；前端据此给管理员改。
    """
    models = await na.admin_enabled_models()
    modes = image_model_modes.get_all()
    default_mode = config.GATEWAY_IMAGE_GEN_MODE
    items = [
        {"model": m, "mode": modes.get(m, default_mode)}
        for m in models if image_model_modes.is_image_model(m)
    ]
    items.sort(key=lambda x: x["model"])
    return ok({"items": items, "default_mode": default_mode, "total": len(items)})


@router.post("/api/console/image-models")
async def console_set_image_model_mode(body: ImageModelModeBody,
                                       _s: dict = Depends(require_admin)):
    """设置某图片模型的同步/异步模式。生图时 BFF 按 params.model 查此项覆盖全局默认。"""
    image_model_modes.set_mode(body.model, body.mode)
    return ok({"model": body.model, "mode": body.mode})


# ---------- 用户请求日志（管理员视角）----------
def _strip_b64(obj, _max=256):
    """兼容别名：实现已上移 cloudstore.strip_b64（chat 落库也要用）。"""
    return cloudstore.strip_b64(obj, _max)


async def _uid_username_map(uids: set) -> dict:
    """批量解析 uid→username（admin_list_users 翻页兜底，最多 5 页×100）。

    失败不阻断主流程 —— 列表缺 username 只是展示降级，不是查询失败。
    """
    names: dict = {}
    if not uids:
        return names
    try:
        for page in range(1, 6):
            d = await na.admin_list_users(page=page, page_size=100)
            for u in d.get("items") or []:
                if u.get("id") in uids:
                    names[u["id"]] = u.get("username", "")
            if len(names) >= len(uids) or len(d.get("items") or []) < 100:
                break
    except Exception:  # noqa: BLE001 —— 上游限流/不可达时降级为空映射
        pass
    return names


@router.get("/api/console/requests")
async def console_requests(username: str = "", uid: int = 0, kind: str = "",
                           status: str = "", model: str = "",
                           limit: int = 50, offset: int = 0,
                           _s: dict = Depends(require_admin)):
    """全站用户请求日志（cloud_request_log）。

    筛选：username（精确，经 new-api 解析成 uid）/ uid 二选一；kind=image|video|chat…
    status=submitted|processing|succeeded|failed|cancelled；model 子串匹配。
    列表返回摘要行（含 username，不含 payload/result）；详情走 /{request_id}。
    """
    limit = min(max(1, limit), 200)
    offset = max(0, offset)
    filter_uid = int(uid) if uid else None
    if username.strip():
        filter_uid = await na.admin_resolve_uid_by_username(username.strip())
    items, total = await cloudstore.request_log_admin_list(
        filter_uid, kind.strip(), status.strip(), model.strip(), limit, offset)
    names = await _uid_username_map({it["uid"] for it in items})
    for it in items:
        it["username"] = names.get(it["uid"], "")
    return ok({"items": items, "total": total, "limit": limit, "offset": offset})


@router.get("/api/console/requests/{request_id}")
async def console_request_detail(request_id: str,
                                 _s: dict = Depends(require_admin)):
    """单条请求日志详情（完整 payload + result）。result 内超长 base64 替换为占位符。"""
    row = await cloudstore.request_log_get(request_id)
    if not row:
        return fail("请求记录不存在", 404)
    row["result"] = _strip_b64(row.get("result"))
    row["payload"] = _strip_b64(row.get("payload"))
    return ok(row)


# ---------- 媒体清理 ----------
@router.get("/api/console/oss/cleanup")
async def console_cleanup_preview(days: int = 0,
                                  _s: dict = Depends(require_admin)):
    """dry-run：统计将有多少媒体被清理（按 OSS_RETENTION_DAYS，可用 days 覆盖预览）。"""
    result = await cloudstore.cleanup_expired_media(
        days or config.OSS_RETENTION_DAYS, config.OSS_CLEANUP_BATCH, dry_run=True)
    return ok(result)


@router.post("/api/console/oss/cleanup")
async def console_cleanup_run(days: int = 0,
                              _s: dict = Depends(require_admin)):
    """立即执行一轮清理（默认按 OSS_RETENTION_DAYS；days 可临时覆盖本次阈值）。"""
    result = await cloudstore.cleanup_expired_media(
        days or config.OSS_RETENTION_DAYS, config.OSS_CLEANUP_BATCH, dry_run=False)
    return ok(result)
