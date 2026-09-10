"""计费展示（只读）：剩余积分。

扣费由 new-api 网关负责（BFF 仅做网关异步接口的透传代理，不扣费；见 IMAGE-ASYNC-TASKS-CONTRACT.md）。
BFF 只做展示，分两个端点：

- 剩余积分：GET /api/me/points        —— 走 env 管理员服务凭证按 uid 读 new-api 用户 quota，
                                           经 quota_to_points 换算（**不依赖用户个人 PAT**，
                                           避免 PAT 被作废导致 401 白屏）
- 消费记录：GET /api/log/self          —— usage.py 已挂载，stat.points 为累计消费积分，
                                           items 为每条调用明细（含 points）；用户 PAT 失效时
                                           自动降级为管理员按 uid 拉取，统计降级为空值

积分余额走管理员凭证，消费记录优先用户 PAT、失效降级管理员；前端均不碰 key。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from .. import config, newapi_client as na
from ..resp import ok
from ..security import require_session

router = APIRouter()
logger = logging.getLogger("bff.billing")


@router.get("/api/me/points")
async def my_points(session: dict = Depends(require_session)):
    """当前登录用户的剩余积分（new-api 内部 quota 经 quota_to_points 换算）。

    **直接走 env 管理员服务凭证（与管理后台同源）按 uid 读取，不依赖用户自身
    会话里的 PAT**——用户 PAT 可能在 new-api 官方前端被「系统访问令牌」作废旧值、
    或被其他共用账号的业务互踢，导致 401 白屏。管理员凭证稳定（env 直供 +
    401 自动重登），余额展示不再受个人 PAT 影响。仅要求登录态，按 session uid
    精准读取本人余额（攻击者无法读他人 uid）。

    响应：
      points       对外剩余积分（int，向下取整，绝不虚报余额）
      used_points  累计已用积分（int）
      unit         积分单位名（默认「积分」）
      raw_quota / raw_used_quota  裸 quota，仅后台对账用，前端请展示 points
    """
    uid = session["uid"]
    data = await na.admin_get_user(uid)
    # new-api 用户对象：quota=剩余，used_quota=已用；个别版本叫 remain_quota。
    quota = data.get("quota")
    if quota is None:
        quota = data.get("remain_quota", 0)
    used = data.get("used_quota", 0)
    return ok({
        "points": config.quota_to_points(quota),
        "used_points": config.quota_to_points(used),
        "unit": config.POINTS_UNIT_NAME,
        "raw_quota": quota,
        "raw_used_quota": used,
    })
