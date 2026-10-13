"""团队（工作室主子账号）业务层。飞哥 2026-10-09 拍板做 1、2：

1. 团队模型：一个主账号（owner）建一个团队（cloud_teams.owner_uid UNIQUE），
   按用户名拉已注册用户进团队（cloud_team_members.uid UNIQUE —— 一人只属一队）。
2. 扣费指向：**额度账本仍在 new-api（quota），BFF 不自建余额表**。子账号消费
   照旧被 new-api 扣自己 quota（网关语义零改动、计费落点正确），「共享池」由
   两件事组成：
   a) 展示层 —— 子账号「我的积分」读主账号池余额（billing.py 分流，见其注释）；
   b) 补给层 —— 低水位自动划转 ensure_member_quota：任务提交前若子账号 quota
      低于阈值（TEAM_TOPUP_THRESHOLD_POINTS），从主账号池划一块
      （TEAM_TOPUP_CHUNK_POINTS）过去（admin_add_quota 两次调用）。

⚠️ 划转是两步非原子（先扣主再加子）：中间失败会漂账，记 error 日志人工对账；
   并发场景用进程内 in-flight 集合去重，最坏多划一块（宁可多给不可双扣）。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from . import cloudstore, config, newapi_client as na
from .newapi_client import NewApiError

logger = logging.getLogger("bff.teams")

_inflight: set[int] = set()


def _current_month() -> str:
    """计费月标签（UTC，如 '2026-10'）。与 new-api 侧无耦合，仅 BFF 划转计数用。"""
    return datetime.now(timezone.utc).strftime("%Y-%m")


async def create_team(owner_uid: int, name: str) -> dict:
    """创建团队。owner 可创建多个团队（分组）；已加入**其他**团队的成员身份 → 409
    （一人只属一队，成员不能另起炉灶建队，先退出原团队）。"""
    member_of = await cloudstore.team_get_by_member(owner_uid)
    if member_of:
        raise NewApiError("你已加入一个团队，不能重复创建", 409)
    team = await cloudstore.team_create(
        uuid.uuid4().hex, owner_uid, (name or "").strip()[:50])
    return team


async def _owned_team(owner_uid: int, team_id: str) -> dict:
    """校验 team_id 属于该 owner，返回团队行；否则 404（不泄露他人团队存在性）。"""
    team = await cloudstore.team_get(team_id)
    if not team or team["owner_uid"] != owner_uid:
        raise NewApiError("团队不存在", 404)
    return team


async def team_info(uid: int) -> dict:
    """我的团队视角（兼用量报表）。返回 {teams: [...]}，每项 {team, members, role}：
    owner → 名下全部团队（各带成员报表）；member → 所在的单个团队；无 → 空数组。
    """
    async def _enrich(m: dict) -> dict:
        """补网关侧用户资料与水位（单成员失败只置空，不影响整体报表）。"""
        out = {**m, "username": "", "display_name": "", "quota": None, "used_points": None}
        try:
            u = await na.admin_get_user(m["uid"])
            out["username"] = u.get("username", "")
            out["display_name"] = u.get("display_name", "")
            quota = u.get("quota", u.get("remain_quota"))
            out["quota"] = config.quota_to_points(quota) if quota is not None else None
            used = u.get("used_quota")
            out["used_points"] = config.quota_to_points(used) if used is not None else None
        except Exception as e:  # noqa: BLE001
            logger.warning("团队成员资料拉取失败 uid=%s: %s", m["uid"], e)
        return out

    async def _entry(team: dict, role: str) -> dict:
        members = await cloudstore.team_members(team["team_id"])
        items = await asyncio.gather(*[_enrich(m) for m in members])
        return {"team": {**team, "role": role}, "members": items}

    entries: list[dict] = []
    owned = await cloudstore.team_list_by_owner(uid)
    entries.extend(await asyncio.gather(*[_entry(t, "owner") for t in owned]))
    membership = await cloudstore.team_get_by_member(uid)
    if membership:
        entries.append(await _entry(membership, "member"))
    return {"teams": entries}


async def set_member_limit(owner_uid: int, team_id: str, uid: int, limit_points: int) -> bool:
    """owner 设置成员月度划转限额（积分，0=不限）。限额执行点在 ensure_member_quota。"""
    team = await _owned_team(owner_uid, team_id)
    if limit_points < 0:
        raise NewApiError("限额不能为负数", 400)
    done = await cloudstore.team_member_set_limit(team["team_id"], uid, limit_points)
    if not done:
        raise NewApiError("该用户不在你的团队中", 404)
    return done


async def add_member(owner_uid: int, team_id: str, username: str) -> dict:
    """主账号按用户名拉已注册用户进指定团队。目标已属任何团队（或已自己建队）→ 409。"""
    team = await _owned_team(owner_uid, team_id)
    uid = await na.admin_resolve_uid_by_username(username)
    if uid == owner_uid:
        raise NewApiError("不能添加自己（你是团队所有者）", 400)
    if await cloudstore.team_list_by_owner(uid):
        raise NewApiError("该用户已创建了自己的团队", 409)
    if not await cloudstore.team_member_add(team["team_id"], uid):
        existing = await cloudstore.team_get_by_member(uid)
        raise NewApiError(
            "该用户已在其他团队" if existing and existing["team_id"] != team["team_id"]
            else "该用户已在你的团队中", 409)
    return {"uid": uid, "username": username}


async def remove_member(owner_uid: int, team_id: str, uid: int) -> bool:
    await _owned_team(owner_uid, team_id)
    return await cloudstore.team_member_remove(team_id, uid)


async def disband(owner_uid: int, team_id: str) -> bool:
    await _owned_team(owner_uid, team_id)
    return await cloudstore.team_delete(team_id)


# ---------------------------------------------------------------------------
# 低水位自动划转（共享池的「补给层」）
# ---------------------------------------------------------------------------

async def pool_uid_for(uid: int) -> "int | None":
    """余额展示的「扣费指向」：团队成员 → 主账号 uid（读主池）；其他人 → None（读自己）。"""
    team = await cloudstore.team_get_by_member(uid)
    return team["owner_uid"] if team else None


async def ensure_member_quota(uid: int) -> None:
    """任务提交前调用：团队成员低于水位线则从主池划一块，**受月度限额约束**（待办 3）。

    限额口径 = 成员**当月从池划走的积分**（monthly_topup_points，自然月滚动）：
    划转是 BFF 唯一的补池闸门，成员当月消耗 ≈ 当月划走，口径自洽。限额 0 = 不限。
    限额内只划差额；任何失败只 warning/error，**绝不阻断任务提交**（子账号自身
    quota 还剩多少是 new-api 的事；划转是体验优化，不是准入闸门）。owner / 非成员直接返回。
    """
    if config.TEAM_TOPUP_CHUNK_POINTS <= 0:
        return
    if uid in _inflight:
        return
    team = await cloudstore.team_get_by_member(uid)
    if not team:
        return
    _inflight.add(uid)
    try:
        member_row = await cloudstore.team_member_get(uid)
        limit = int((member_row or {}).get("monthly_limit_points") or 0)
        month = _current_month()
        used = 0
        if member_row and member_row.get("period_month") == month:
            used = int(member_row.get("monthly_topup_points") or 0)
        chunk_quota = config.points_to_quota(config.TEAM_TOPUP_CHUNK_POINTS)
        if chunk_quota <= 0:
            return
        member = await na.admin_get_user(uid)
        member_quota = member.get("quota", member.get("remain_quota", 0)) or 0
        if member_quota >= config.points_to_quota(config.TEAM_TOPUP_THRESHOLD_POINTS):
            return
        # 月度限额（待办 3）：limit>0 时当月划转不得超额，最多划到差额。
        grant_points = config.TEAM_TOPUP_CHUNK_POINTS
        if limit > 0 and used + grant_points > limit:
            grant_points = limit - used
            if grant_points <= 0:
                logger.info("成员本月划转已达限额，跳过 member=%s used=%s limit=%s",
                            uid, used, limit)
                return
        owner = await na.admin_get_user(team["owner_uid"])
        owner_quota = owner.get("quota", owner.get("remain_quota", 0)) or 0
        owner_need = config.points_to_quota(grant_points)
        if owner_quota < owner_need:
            logger.warning("团队主池余额不足，跳过划转 member=%s owner=%s pool_quota=%s need=%s",
                           uid, team["owner_uid"], owner_quota, owner_need)
            return
        await na.admin_add_quota(team["owner_uid"], -owner_need)
        try:
            await na.admin_add_quota(uid, owner_need)
        except Exception:  # noqa: BLE001 —— 扣了主没加子：漂账，人工对账
            logger.error("团队划转漂账：主池已扣未加子 member=%s owner=%s chunk=%s"
                         "（请人工给 member=%s 补 %s quota）",
                         uid, team["owner_uid"], owner_need, uid, owner_need)
            return
        await cloudstore.team_member_add_topup(team["team_id"], uid, grant_points, month)
        logger.info("团队划转完成 owner=%s member=%s chunk_points=%s month_used=%s/%s",
                    team["owner_uid"], uid, grant_points, used + grant_points,
                    limit if limit > 0 else "∞")
    except Exception as e:  # noqa: BLE001 —— 划转失败绝不阻断提交
        logger.warning("团队划转检查失败 uid=%s: %s", uid, e)
    finally:
        _inflight.discard(uid)
