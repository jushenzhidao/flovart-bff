"""团队（工作室主子账号）测试。覆盖三层：

1. LocalMeta 双表 CRUD：建团队 / 加成员 / 成员反查团队 / 移除 / 解散 / 约束
   （一人一队：owner_uid UNIQUE + member uid UNIQUE）。
2. teams 业务层：重复创建 409、添加未注册用户报错、移除非成员 404。
3. 扣费指向：
   - pool_uid_for：成员 → owner uid；非成员 → None
   - ensure_member_quota：低水位 → 从主池划一块；水位充足 → 不动；主池不足 →
     跳过；划转二步失败（加子失败）→ error 且不抛；非成员 → no-op
   - billing.my_points 团队分流：成员读主池（team_pool=True）
"""
import asyncio

import pytest

from app import cloudstore, config, teams
from app.cloudstore import LocalMeta
from app.newapi_client import NewApiError


@pytest.fixture()
def local_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    meta = LocalMeta()
    monkeypatch.setattr(cloudstore, "META", meta)
    return meta


# ---------------- LocalMeta CRUD ----------------

def test_team_create_and_get(local_meta):
    team = asyncio.run(cloudstore.team_create("t1", 100, "设计部"))
    assert team["team_id"] == "t1" and team["owner_uid"] == 100
    assert asyncio.run(cloudstore.team_get("t1"))["name"] == "设计部"
    assert asyncio.run(cloudstore.team_get_by_owner(100))["team_id"] == "t1"
    assert asyncio.run(cloudstore.team_get_by_owner(200)) is None


def test_team_owner_multi_teams(local_meta):
    """多团队分组：同 owner 可建多队，team_list_by_owner 按创建时间升序返回。"""
    asyncio.run(cloudstore.team_create("t1", 100, "设计部"))
    asyncio.run(cloudstore.team_create("t2", 100, "视频组"))
    asyncio.run(cloudstore.team_create("t3", 300, "别人的"))
    rows = asyncio.run(cloudstore.team_list_by_owner(100))
    assert [r["team_id"] for r in rows] == ["t1", "t2"]
    assert rows[0]["name"] == "设计部" and rows[1]["name"] == "视频组"
    # 兼容入口取最早创建的一个
    assert asyncio.run(cloudstore.team_get_by_owner(100))["team_id"] == "t1"
    assert asyncio.run(cloudstore.team_list_by_owner(200)) == []
    assert asyncio.run(cloudstore.team_get("t2"))["owner_uid"] == 100


def test_team_members_flow(local_meta):
    asyncio.run(cloudstore.team_create("t1", 100, ""))
    assert asyncio.run(cloudstore.team_member_add("t1", 201)) is True
    assert asyncio.run(cloudstore.team_member_add("t1", 202)) is True
    assert asyncio.run(cloudstore.team_member_add("t1", 201)) is False  # 重复
    members = asyncio.run(cloudstore.team_members("t1"))
    assert [m["uid"] for m in members] == [201, 202]
    assert asyncio.run(cloudstore.team_get_by_member(202))["owner_uid"] == 100
    assert asyncio.run(cloudstore.team_get_by_member(999)) is None
    assert asyncio.run(cloudstore.team_member_remove("t1", 201)) is True
    assert asyncio.run(cloudstore.team_member_remove("t1", 201)) is False
    assert asyncio.run(cloudstore.team_get_by_member(201)) is None


def test_team_member_uid_unique_across_teams(local_meta):
    """一人只属一队：加进 t2 时 uid 冲突返回 False。"""
    asyncio.run(cloudstore.team_create("t1", 100, ""))
    asyncio.run(cloudstore.team_create("t2", 300, ""))
    assert asyncio.run(cloudstore.team_member_add("t1", 201)) is True
    assert asyncio.run(cloudstore.team_member_add("t2", 201)) is False


def test_team_delete_cascades_members(local_meta):
    asyncio.run(cloudstore.team_create("t1", 100, ""))
    asyncio.run(cloudstore.team_member_add("t1", 201))
    assert asyncio.run(cloudstore.team_delete("t1")) is True
    assert asyncio.run(cloudstore.team_get("t1")) is None
    assert asyncio.run(cloudstore.team_get_by_member(201)) is None


# ---------------- teams 业务层 ----------------

def _fake_admin(monkeypatch, users):
    """users: {uid: {"quota": n}}；返回 admin_get_user / admin_add_quota 调用记录。"""
    calls = []

    async def fake_get_user(uid):
        if uid not in users:
            raise NewApiError(f"no user {uid}", 404)
        return dict(users[uid])

    async def fake_add_quota(uid, quota, mode="add"):
        calls.append((uid, quota))
        users[uid]["quota"] = users[uid].get("quota", 0) + quota

    monkeypatch.setattr(teams.na, "admin_get_user", fake_get_user)
    monkeypatch.setattr(teams.na, "admin_add_quota", fake_add_quota)
    return calls


def _fake_resolve(monkeypatch, known):
    async def fake_resolve(username):
        if username in known:
            return known[username]
        raise NewApiError(f"未找到用户 {username}", 404)
    monkeypatch.setattr(teams.na, "admin_resolve_uid_by_username", fake_resolve)


def test_create_team_rules(local_meta):
    """owner 可建多队；已加入其他团队的成员不能另建（先退出）。"""
    asyncio.run(cloudstore.team_create("t1", 100, "a"))
    asyncio.run(teams.create_team(100, "x"))  # owner 第二个团队：OK
    assert len(asyncio.run(cloudstore.team_list_by_owner(100))) == 2
    asyncio.run(cloudstore.team_member_add("t1", 201))
    with pytest.raises(NewApiError):
        asyncio.run(teams.create_team(201, "x"))  # 已是成员


def test_add_member_flow(local_meta, monkeypatch):
    _fake_resolve(monkeypatch, {"zhangsan": 201, "lisi": 202})
    asyncio.run(teams.create_team(100, "t"))          # t 的 team_id 未知 → 先查
    team_id = asyncio.run(cloudstore.team_list_by_owner(100))[0]["team_id"]
    member = asyncio.run(teams.add_member(100, team_id, "zhangsan"))
    assert member == {"uid": 201, "username": "zhangsan"}
    with pytest.raises(NewApiError):  # 再加一次：已在团队
        asyncio.run(teams.add_member(100, team_id, "zhangsan"))
    with pytest.raises(NewApiError):  # 加自己
        _fake_resolve(monkeypatch, {"owner": 100})
        asyncio.run(teams.add_member(100, team_id, "owner"))
    with pytest.raises(NewApiError):  # 目标已自己建队
        asyncio.run(teams.create_team(202, "y"))
        asyncio.run(teams.add_member(100, team_id, "lisi"))
    with pytest.raises(NewApiError):  # 越权操作他人团队 → 404
        asyncio.run(teams.add_member(999, team_id, "zhangsan"))
    assert asyncio.run(teams.remove_member(100, team_id, 201)) is True
    assert asyncio.run(teams.remove_member(100, team_id, 201)) is False  # 404 判定归路由层


def test_pool_uid_for(local_meta):
    asyncio.run(cloudstore.team_create("t1", 100, ""))
    asyncio.run(cloudstore.team_member_add("t1", 201))
    assert asyncio.run(teams.pool_uid_for(201)) == 100
    assert asyncio.run(teams.pool_uid_for(100)) is None  # owner 读自己
    assert asyncio.run(teams.pool_uid_for(999)) is None


# ---------------- 低水位划转 ----------------

def test_ensure_topup_when_low(monkeypatch, local_meta):
    users = {100: {"quota": 1000000}, 201: {"quota": 10}}
    calls = _fake_admin(monkeypatch, users)
    monkeypatch.setattr(config, "TEAM_TOPUP_THRESHOLD_POINTS", 200)
    monkeypatch.setattr(config, "TEAM_TOPUP_CHUNK_POINTS", 1000)
    asyncio.run(cloudstore.team_create("t1", 100, ""))
    asyncio.run(cloudstore.team_member_add("t1", 201))
    asyncio.run(teams.ensure_member_quota(201))
    chunk = config.points_to_quota(1000)
    assert calls == [(100, -chunk), (201, chunk)]
    assert users[201]["quota"] == 10 + chunk


def test_ensure_noop_when_above_threshold(monkeypatch, local_meta):
    users = {100: {"quota": 1000000}, 201: {"quota": 9999999}}
    calls = _fake_admin(monkeypatch, users)
    monkeypatch.setattr(config, "TEAM_TOPUP_THRESHOLD_POINTS", 200)
    monkeypatch.setattr(config, "TEAM_TOPUP_CHUNK_POINTS", 1000)
    asyncio.run(cloudstore.team_create("t1", 100, ""))
    asyncio.run(cloudstore.team_member_add("t1", 201))
    asyncio.run(teams.ensure_member_quota(201))
    assert calls == []


def test_ensure_noop_when_pool_insufficient(monkeypatch, local_meta):
    users = {100: {"quota": 10}, 201: {"quota": 10}}
    calls = _fake_admin(monkeypatch, users)
    monkeypatch.setattr(config, "TEAM_TOPUP_THRESHOLD_POINTS", 200)
    monkeypatch.setattr(config, "TEAM_TOPUP_CHUNK_POINTS", 1000)
    asyncio.run(cloudstore.team_create("t1", 100, ""))
    asyncio.run(cloudstore.team_member_add("t1", 201))
    asyncio.run(teams.ensure_member_quota(201))
    assert calls == []  # 主池不够：不动账


def test_ensure_noop_for_non_member_and_owner(monkeypatch, local_meta):
    users = {100: {"quota": 10}}
    calls = _fake_admin(monkeypatch, users)
    monkeypatch.setattr(config, "TEAM_TOPUP_CHUNK_POINTS", 1000)
    asyncio.run(teams.ensure_member_quota(100))  # owner
    asyncio.run(teams.ensure_member_quota(999))  # 非成员
    assert calls == []


def test_ensure_survives_second_step_failure(monkeypatch, local_meta):
    """扣主成功、加子失败 → 记漂账 error 但不抛（提交链路不被阻断）。"""
    users = {100: {"quota": 1000000}, 201: {"quota": 10}}
    _fake_admin(monkeypatch, users)
    monkeypatch.setattr(config, "TEAM_TOPUP_THRESHOLD_POINTS", 200)
    monkeypatch.setattr(config, "TEAM_TOPUP_CHUNK_POINTS", 1000)
    asyncio.run(cloudstore.team_create("t1", 100, ""))
    asyncio.run(cloudstore.team_member_add("t1", 201))

    async def broken_add(uid, quota, mode="add"):
        if uid == 201:
            raise RuntimeError("network down")
        users[uid]["quota"] += quota

    monkeypatch.setattr(teams.na, "admin_add_quota", broken_add)
    asyncio.run(teams.ensure_member_quota(201))  # 不抛
    assert users[100]["quota"] == 1000000 - config.points_to_quota(1000)


def test_ensure_disabled_when_chunk_zero(monkeypatch, local_meta):
    users = {100: {"quota": 1000000}, 201: {"quota": 10}}
    calls = _fake_admin(monkeypatch, users)
    monkeypatch.setattr(config, "TEAM_TOPUP_CHUNK_POINTS", 0)
    asyncio.run(cloudstore.team_create("t1", 100, ""))
    asyncio.run(cloudstore.team_member_add("t1", 201))
    asyncio.run(teams.ensure_member_quota(201))
    assert calls == []


# ---------------- 月度限额（待办 3） ----------------

def test_member_limit_and_topup_counter(local_meta):
    asyncio.run(cloudstore.team_create("t1", 100, ""))
    asyncio.run(cloudstore.team_member_add("t1", 201))
    assert asyncio.run(cloudstore.team_member_get(201))["monthly_limit_points"] == 0
    assert asyncio.run(cloudstore.team_member_set_limit("t1", 201, 1500)) is True
    assert asyncio.run(cloudstore.team_member_get(201))["monthly_limit_points"] == 1500
    assert asyncio.run(cloudstore.team_member_set_limit("t1", 999, 100)) is False
    # 同月累加
    r1 = asyncio.run(cloudstore.team_member_add_topup("t1", 201, 1000, "2026-10"))
    assert r1["monthly_topup_points"] == 1000
    r2 = asyncio.run(cloudstore.team_member_add_topup("t1", 201, 500, "2026-10"))
    assert r2["monthly_topup_points"] == 1500
    # 跨月重置
    r3 = asyncio.run(cloudstore.team_member_add_topup("t1", 201, 700, "2026-11"))
    assert r3 == {"period_month": "2026-11", "monthly_topup_points": 700}


def test_ensure_respects_monthly_limit(monkeypatch, local_meta):
    """limit=1500、chunk=1000：第一次划满 1000，第二次只补差额 500，第三次到顶跳过。"""
    users = {100: {"quota": 1000000}, 201: {"quota": 10}}
    calls = _fake_admin(monkeypatch, users)
    monkeypatch.setattr(config, "TEAM_TOPUP_THRESHOLD_POINTS", 200)
    monkeypatch.setattr(config, "TEAM_TOPUP_CHUNK_POINTS", 1000)
    asyncio.run(cloudstore.team_create("t1", 100, ""))
    asyncio.run(cloudstore.team_member_add("t1", 201))
    asyncio.run(cloudstore.team_member_set_limit("t1", 201, 1500))
    from app.teams import _current_month
    month = _current_month()

    asyncio.run(teams.ensure_member_quota(201))  # 划 1000（ensure 内部记账 used=1000）
    users[201]["quota"] = 10  # 模拟烧完，低于水位才会触发第二次划转
    asyncio.run(teams.ensure_member_quota(201))  # 差额 500
    users[201]["quota"] = 10
    asyncio.run(teams.ensure_member_quota(201))  # 到顶：跳过
    chunk = config.points_to_quota(1000)
    half = config.points_to_quota(500)
    # 扣主三次调用：-1000、-500、第三次无；加子对应
    assert calls.count((100, -chunk)) == 1
    assert calls.count((100, -half)) == 1
    assert calls.count((201, chunk)) == 1
    assert calls.count((201, half)) == 1


def test_ensure_resets_counter_next_month(monkeypatch, local_meta):
    """上月划满到顶，本月计数归零后可继续划。"""
    users = {100: {"quota": 1000000}, 201: {"quota": 10}}
    calls = _fake_admin(monkeypatch, users)
    monkeypatch.setattr(config, "TEAM_TOPUP_THRESHOLD_POINTS", 200)
    monkeypatch.setattr(config, "TEAM_TOPUP_CHUNK_POINTS", 1000)
    asyncio.run(cloudstore.team_create("t1", 100, ""))
    asyncio.run(cloudstore.team_member_add("t1", 201))
    asyncio.run(cloudstore.team_member_set_limit("t1", 201, 1000))
    from app.teams import _current_month
    month = _current_month()
    asyncio.run(cloudstore.team_member_add_topup("t1", 201, 1000, "2000-01"))  # 上月到顶
    asyncio.run(teams.ensure_member_quota(201))  # 本月：重置后正常划 1000
    chunk = config.points_to_quota(1000)
    assert calls.count((100, -chunk)) == 1


def test_team_info_enriches_members(monkeypatch, local_meta):
    """owner 视角报表：成员带 username/display_name/水位/累计消费（待办 4）。"""
    users = {201: {"username": "zhangsan", "display_name": "小张",
                   "quota": 500000, "used_quota": 100000}}
    _fake_admin(monkeypatch, users)
    asyncio.run(cloudstore.team_create("t1", 100, "设计部"))
    asyncio.run(cloudstore.team_create("t2", 100, "视频组"))
    asyncio.run(cloudstore.team_member_add("t1", 201))
    info = asyncio.run(teams.team_info(100))
    assert len(info["teams"]) == 2
    first = info["teams"][0]
    assert first["team"]["role"] == "owner" and first["team"]["name"] == "设计部"
    m = first["members"][0]
    assert m["username"] == "zhangsan" and m["display_name"] == "小张"
    assert m["quota"] == 10000 and m["used_points"] == 2000  # quota_to_points 换算
    assert info["teams"][1]["members"] == []  # 视频组还没人


def test_team_info_member_view(monkeypatch, local_meta):
    _fake_admin(monkeypatch, {201: {"username": "z", "display_name": "", "quota": 0, "used_quota": 0}})
    asyncio.run(cloudstore.team_create("t1", 100, ""))
    asyncio.run(cloudstore.team_member_add("t1", 201))
    info = asyncio.run(teams.team_info(201))
    assert info["teams"][0]["team"]["role"] == "member"
    assert info["teams"][0]["team"]["owner_uid"] == 100


def test_team_info_empty(local_meta):
    assert asyncio.run(teams.team_info(999)) == {"teams": []}
