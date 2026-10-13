"""团队（工作室主子账号）端点。多团队分组语义（owner 可建多队，成员一人一队）：

- POST   /api/team                        创建团队（本人为主账号；可多次创建不同分组）
- GET    /api/team                        我的团队列表（owner 带各队成员报表；未加入 teams=[]）
- DELETE /api/team/{team_id}              解散指定团队（仅 owner；成员连带解绑，个人账号/quota 不受影响）
- POST   /api/team/{team_id}/members      {username} 按用户名添加已注册用户（仅 owner）
- DELETE /api/team/{team_id}/members/{uid} 移除成员（仅 owner）
- PATCH  /api/team/{team_id}/members/{uid} 设置成员月度划转限额（仅 owner）

加成员要求对方已注册（按 new-api 用户名精确解析）；子账号消费扣主池的机制
见 app/teams.py 模块头。越权由 _owned_team（team_id 归属校验）兜住，
非本人团队一律 404。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import teams
from ..newapi_client import NewApiError
from ..resp import ok
from ..security import require_session

router = APIRouter()


class CreateTeamBody(BaseModel):
    name: str = Field(default="", max_length=50, description="团队名称（可选）")


class AddMemberBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64, description="已注册用户的 new-api 用户名")


class SetLimitBody(BaseModel):
    monthly_limit_points: int = Field(..., ge=0, description="月度划转限额（积分），0=不限")


def _http(e: NewApiError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=e.message)


@router.post("/api/team")
async def create_team(body: CreateTeamBody, session: dict = Depends(require_session)):
    try:
        team = await teams.create_team(int(session["uid"]), body.name)
    except NewApiError as e:
        raise _http(e) from e
    return ok({"team": {**team, "role": "owner"}, "members": []})


@router.get("/api/team")
async def my_team(session: dict = Depends(require_session)):
    return ok(await teams.team_info(int(session["uid"])))


@router.delete("/api/team/{team_id}")
async def disband_team(team_id: str, session: dict = Depends(require_session)):
    try:
        done = await teams.disband(int(session["uid"]), team_id)
    except NewApiError as e:
        raise _http(e) from e
    return ok({"disbanded": done})


@router.post("/api/team/{team_id}/members")
async def add_member(team_id: str, body: AddMemberBody, session: dict = Depends(require_session)):
    try:
        member = await teams.add_member(int(session["uid"]), team_id, body.username)
    except NewApiError as e:
        raise _http(e) from e
    return ok(member)


@router.delete("/api/team/{team_id}/members/{uid}")
async def remove_member(team_id: str, uid: int, session: dict = Depends(require_session)):
    try:
        done = await teams.remove_member(int(session["uid"]), team_id, uid)
    except NewApiError as e:
        raise _http(e) from e
    if not done:
        raise HTTPException(status_code=404, detail="该用户不在你的团队中")
    return ok({"removed": done})


@router.patch("/api/team/{team_id}/members/{uid}")
async def set_member_limit(team_id: str, uid: int, body: SetLimitBody,
                           session: dict = Depends(require_session)):
    """设置成员月度划转限额（积分，0=不限）。执行点在任务提交前的划转钩子。"""
    try:
        await teams.set_member_limit(int(session["uid"]), team_id, uid, body.monthly_limit_points)
    except NewApiError as e:
        raise _http(e) from e
    return ok({"uid": uid, "monthly_limit_points": body.monthly_limit_points})
