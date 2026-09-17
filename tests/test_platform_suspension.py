"""平台服务「下架 / 删除」闸门（飞哥 2026-09-16）。

## 需求原文

> 「我发布过的 AI 服务，我管理员删除掉了，用户使用的时候还是可以看到；
>   我希望加一个下架按钮、且直接删除后用户这边也不应该看到，
>   就算用户没有刷新拉取，直接使用也是提示用户模型下架了」

## 为什么光靠前端拉取不够

平台服务在用户侧是**本地持久化影子条目**（`extraConfig.platformSource='1'`），
只在登录/冷启动时与服务端对齐一次 → 页面开着就一直在。
更关键的是：只要该模型在上游网关渠道里还存在，用户拿着自己的平台 Key
**照样调得通** —— 从列表里删掉拦不住使用。

⇒ 准入判定必须落在服务端。本文件守护三件事：

1. **撤回集**（`revoked`）由 PUT 的 diff 自动维护：
   `(历史撤回 ∪ 上次已发布) − 本次已发布`。删除自动记录，重新发布自动解除。
2. **闸门**：`/api/tasks`（非 `_gateway`）与 `/api/chat/completions` 拒绝已下架模型，
   且错误文案说明原因（前端只读 `message`，不能只给 HTTP 状态码）。
3. **不误伤**：网关渠道目录（平台 Key 池）与用户 BYOK（`_gateway`）的模型
   从来没进过撤回集，必须照常放行。
"""
import asyncio

import pytest

from app import cloudstore, platform_catalog as catalog
from app.platform_catalog import ModelSuspendedError
from app.routers import platform_services, tasks as tasks_router

# ---------------------------------------------------------------------------
# 夹具：内存版 cloud_docs（不触网、不落盘）
# ---------------------------------------------------------------------------


class _DocStore:
    def __init__(self):
        self.doc = None

    async def doc_get(self, uid, scope, doc_key):
        return self.doc

    async def doc_put(self, uid, scope, doc_key, payload):
        rev = int((self.doc or {}).get("revision", 0)) + 1
        self.doc = {"payload": payload, "revision": rev, "updated_at": "2026-09-16T00:00:00Z"}
        return {"revision": rev, "updated_at": "2026-09-16T00:00:00Z"}


@pytest.fixture()
def store(monkeypatch):
    s = _DocStore()
    monkeypatch.setattr(cloudstore, "doc_get", s.doc_get)
    monkeypatch.setattr(cloudstore, "doc_put", s.doc_put)
    # TTL 缓存会跨用例串状态，每个用例前必须清掉。
    catalog.invalidate()
    yield s
    catalog.invalidate()


def _svc(sid, name, models, **extra):
    return {"id": sid, "provider": "openai_compatible", "name": name,
            "capabilities": ["image"], "models": list(models), **extra}


async def _put(services, base_revision=None):
    body = {"services": services}
    if base_revision is not None:
        body["base_revision"] = base_revision
    return await platform_services.put_platform_services(body, {"username": "admin"})


# ---------------------------------------------------------------------------
# 1) 撤回集公式（纯函数）
# ---------------------------------------------------------------------------
def test_merge_revoked_records_deleted_models():
    """删除条目 → 其模型进撤回集。"""
    prev = {"a": "S1", "b": "S1"}
    next_ = {"b": "S1"}
    out = catalog.merge_revoked(prev_published=prev, next_published=next_, revoked_old={}, now=100.0)
    assert set(out) == {"a"}
    assert out["a"] == 100.0


def test_merge_revoked_republish_clears_revocation():
    """重新发布同一模型 → 自动从撤回集移出（自愈，不需要管理员手动恢复）。"""
    out = catalog.merge_revoked(
        prev_published={"a": "S1"}, next_published={"a": "S1"},
        revoked_old={"a": 50.0}, now=100.0)
    assert "a" not in out


def test_merge_revoked_keeps_history_and_normalizes_case():
    """历史撤回项不能因为一次无关的 PUT 就丢掉；模型名大小写不敏感。"""
    out = catalog.merge_revoked(
        prev_published={"B": "S1"}, next_published={"c": "S2"},
        revoked_old={"old-one": 10.0}, now=100.0)
    assert "old-one" in out and "b" in out and "c" not in out


def test_merge_revoked_prunes_expired_entries():
    """超期（180 天）的撤回项应被裁掉，撤回集不能无限膨胀。"""
    ancient = 100.0 - 200 * 86400
    out = catalog.merge_revoked(
        prev_published={}, next_published={}, revoked_old={"fossil": ancient}, now=100.0)
    assert out == {}


def test_merge_revoked_keeps_full_gateway_catalog_scale():
    """回归（2026-09-16 实测踩坑）：撤回集上限绝不能裁掉正常记录。

    一条平台服务携带的是**整个网关目录**（本地实测 741 / 743 个模型）。
    把服务全部下架 → 743 条撤回记录。原实现 REVOKED_MAX=500 会裁掉 243 条，
    那 243 个模型静默漏过闸门、照旧可调用 —— 直接违背
    「下架了就直接使用也要提示下架」这条需求。
    """
    catalog_size = 743
    prev = {f"model-{i:04d}": "OpenAI" for i in range(catalog_size)}
    out = catalog.merge_revoked(
        prev_published=prev, next_published={}, revoked_old={}, now=100.0)
    assert len(out) == catalog_size, (
        f"下架整条服务后应撤回全部 {catalog_size} 个模型，实际只保留 {len(out)} 个 "
        f"—— 被裁掉的那些模型会漏过闸门")
    assert set(out) == set(prev)


def test_revoked_max_is_sized_for_real_catalog():
    """上限必须远大于真实网关目录规模，否则它就从「保险丝」变成了「漏洞源」。"""
    assert catalog.REVOKED_MAX >= 10_000


def test_published_models_excludes_suspended():
    """下架条目的模型不计入「已发布」—— 这正是它会被 diff 撤回的原因。"""
    services = [_svc("s1", "A", ["m1"]), _svc("s2", "B", ["m2"], suspended=True)]
    assert set(catalog.published_models(services)) == {"m1"}
    assert set(catalog.suspended_models(services)) == {"m2"}


# ---------------------------------------------------------------------------
# 2) 端到端：PUT 之后闸门必须立刻拦（不依赖任何重启/缓存过期）
# ---------------------------------------------------------------------------
def test_delete_service_blocks_model_immediately(store):
    """发布 → 删除 → 立刻调用被拒（并说明原因）。"""
    asyncio.run(_put([_svc("s1", "Gemini 图像", ["gemini-3.1-flash-image-preview"])]))
    asyncio.run(catalog.assert_model_available("gemini-3.1-flash-image-preview"))  # 发布时可调

    asyncio.run(_put([]))  # 管理员删除

    with pytest.raises(ModelSuspendedError) as ei:
        asyncio.run(catalog.assert_model_available("gemini-3.1-flash-image-preview"))
    assert ei.value.code == "MODEL_SUSPENDED"
    assert ei.value.reason == "removed"
    assert "gemini-3.1-flash-image-preview" in ei.value.message
    assert "删除" in ei.value.message


def test_delete_full_catalog_service_blocks_every_model(store):
    """回归（真实规模）：一条服务携带整个网关目录，删除后**每个**模型都必须被拦。

    这里刻意用 743 个模型走完整的 PUT → 删除 → 闸门链路，
    确保没有任何一个模型因为撤回集被裁剪而漏出去。
    """
    models = [f"cat-model-{i:04d}" for i in range(743)]
    asyncio.run(_put([_svc("s1", "OpenAI", models)]))
    asyncio.run(_put([]))  # 管理员删除整条服务

    state = asyncio.run(catalog.read_state(force=True))
    assert len(state["revoked"]) == 743, f"撤回集只剩 {len(state['revoked'])} 条，有模型会漏拦"

    # 抽查首/中/尾（旧实现的 500 上限会先丢最旧的，尾部最容易漏）
    for m in (models[0], models[371], models[-1]):
        with pytest.raises(ModelSuspendedError):
            asyncio.run(catalog.assert_model_available(m))


def test_suspend_then_restore_roundtrip(store):
    """下架 → 拦截；上架 → 恢复放行。"""

    asyncio.run(_put([_svc("s1", "Gemini 图像", ["gemini-3.1-flash-image"])]))
    asyncio.run(_put([_svc("s1", "Gemini 图像", ["gemini-3.1-flash-image"], suspended=True)]))

    with pytest.raises(ModelSuspendedError) as ei:
        asyncio.run(catalog.assert_model_available("gemini-3.1-flash-image"))
    assert "Gemini 图像" in ei.value.message, "错误里要带上服务名，用户才知道找谁"
    assert ei.value.reason == "suspended"

    asyncio.run(_put([_svc("s1", "Gemini 图像", ["gemini-3.1-flash-image"], suspended=False)]))
    asyncio.run(catalog.assert_model_available("gemini-3.1-flash-image"))


def test_gate_is_case_insensitive(store):
    """模型名大小写不同不能绕过（网关侧也不区分）。"""
    asyncio.run(_put([_svc("s1", "A", ["Gemini-3.1-Flash"])]))
    asyncio.run(_put([]))
    with pytest.raises(ModelSuspendedError):
        asyncio.run(catalog.assert_model_available("gemini-3.1-flash"))


def test_gate_never_touches_unknown_models(store):
    """⚠️ 不误伤：网关渠道目录 / 用户 BYOK 的模型从没进过撤回集，必须放行。"""
    asyncio.run(_put([_svc("s1", "A", ["m-published"])]))
    asyncio.run(_put([]))
    asyncio.run(catalog.assert_model_available("gpt-4.1-mini"))       # 网关渠道目录里的
    asyncio.run(catalog.assert_model_available("my-own-model"))       # 用户自配的
    asyncio.run(catalog.assert_model_available(None))                 # 无模型的 task type


def test_empty_catalog_blocks_nothing(store):
    """从未发布过任何服务 → 撤回集为空 → 全部放行（升级上线的安全默认）。"""
    asyncio.run(catalog.assert_model_available("whatever-model"))


# ---------------------------------------------------------------------------
# 3) 模型目录出口过滤（治「显示」）
# ---------------------------------------------------------------------------
def test_filter_available_drops_revoked_keeps_order(store):
    asyncio.run(_put([_svc("s1", "A", ["m-old", "m-keep"])]))
    asyncio.run(_put([_svc("s1", "A", ["m-keep"])]))

    out = asyncio.run(catalog.filter_available(["m-keep", "m-old", "other", "m-keep"]))
    assert out == ["m-keep", "other"], "已下架的剔掉、保留原序、顺带去重"


def test_filter_available_drops_suspended(store):
    asyncio.run(_put([_svc("s1", "A", ["m1", "m2"], suspended=True)]))
    assert asyncio.run(catalog.filter_available(["m1", "m2", "m3"])) == ["m3"]


# ---------------------------------------------------------------------------
# 4) 下发与持久化细节
# ---------------------------------------------------------------------------
def test_suspended_entry_redacts_credentials(store):
    """下架条目不得再下发 key/baseUrl —— 否则旧前端仍可绕过下架直连。"""
    svc = _svc("s1", "A", ["m1"], suspended=True, key="sk-secret", baseUrl="https://x/v1")
    asyncio.run(_put([svc]))
    out = asyncio.run(platform_services.list_platform_services({}))["data"]
    entry = out["items"][0]
    assert entry["suspended"] is True
    assert "key" not in entry and "baseUrl" not in entry
    assert entry["keyPresent"] is False


def test_put_response_reports_newly_revoked(store):
    """回执里要能看到「这次下架了哪些模型」，管理员才知道按钮起了作用。"""
    asyncio.run(_put([_svc("s1", "A", ["m1", "m2"])]))
    res = asyncio.run(_put([_svc("s1", "A", ["m1"])]))
    data = res["data"]
    assert data["newly_revoked"] == ["m2"]
    assert data["published_models"] == ["m1"]
    assert data["revoked_models"] == ["m2"]


def test_sanitize_syncs_models_and_custommodels(store):
    """models / customModels 双向同步 —— 只存一边会让另一条前端读取路径拿到空清单。"""
    asyncio.run(_put([{"id": "s1", "name": "A", "customModels": ["m1", "m2"]}]))
    doc = store.doc["payload"]
    assert doc["services"][0]["models"] == ["m1", "m2"]
    assert doc["services"][0]["customModels"] == ["m1", "m2"]

    asyncio.run(_put([{"id": "s1", "name": "A", "models": ["m3"]}]))
    doc = store.doc["payload"]
    assert doc["services"][0]["customModels"] == ["m3"]


def test_legacy_entry_without_suspended_is_published(store):
    """存量数据没有 suspended 字段 → 视为上架（不能把老条目全判成下架）。"""
    asyncio.run(_put([{"id": "s1", "name": "A", "models": ["m1"]}]))
    assert store.doc["payload"]["services"][0]["suspended"] is False
    asyncio.run(catalog.assert_model_available("m1"))


# ---------------------------------------------------------------------------
# 5) 路由闸门：/api/tasks 拦截，但 BYOK(_gateway) 放行
# ---------------------------------------------------------------------------
def _submit(body, monkeypatch):
    calls = []

    async def fake_submit(uid, kind, params):
        calls.append((uid, kind, params))
        return {"id": "req-1", "status": "processing"}

    monkeypatch.setattr(tasks_router.tasks, "submit", fake_submit)
    res = asyncio.run(tasks_router.create_task(tasks_router.SubmitBody(**body), {"uid": 7}))
    return res, calls


def test_create_task_blocks_suspended_model(store, monkeypatch):
    asyncio.run(_put([_svc("s1", "A", ["m1"])]))
    asyncio.run(_put([]))
    with pytest.raises(ModelSuspendedError):
        _submit({"type": "image-gen", "params": {"model": "m1", "prompt": "x"}}, monkeypatch)


def test_create_task_allows_byok_external_gateway(store, monkeypatch):
    """带 `_gateway` = 用户 BYOK 直连自己的端点，与平台供给无关 → 绝不能拦。"""
    asyncio.run(_put([_svc("s1", "A", ["m1"])]))
    asyncio.run(_put([]))
    res, calls = _submit({
        "type": "image-gen",
        "params": {"model": "m1", "prompt": "x",
                   "_gateway": {"base_url": "https://my.own/v1", "api_key": "sk-mine"}},
    }, monkeypatch)
    assert res["success"] is True
    assert len(calls) == 1


def test_create_task_allows_unrelated_and_modelless_types(store, monkeypatch):
    asyncio.run(_put([_svc("s1", "A", ["m1"])]))
    asyncio.run(_put([]))
    _submit({"type": "image-gen", "params": {"model": "gpt-4.1-mini", "prompt": "x"}}, monkeypatch)
    _submit({"type": "upscale", "params": {"image": "data:image/png;base64,AA"}}, monkeypatch)


# ---------------------------------------------------------------------------
# 6) HTTP 层契约：409 必须是**统一响应壳**，否则前端看不到原因
# ---------------------------------------------------------------------------
def test_suspended_model_returns_unified_shell_over_http(store):
    """前端 `hostedClient.api()` **只读 `body.message`**。

    若这里走 FastAPI 默认的 `{"detail": ...}`，用户只会看到
    「请求失败(409)」—— 「模型已下架」这个关键信息又被吞掉了，
    等于没做。故本用例锁死响应形状。
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.security import require_session

    asyncio.run(_put([_svc("s1", "OpenAI", ["gemini-3.1-flash-image"])]))
    asyncio.run(_put([]))  # 管理员删除整条服务

    app.dependency_overrides[require_session] = lambda: {"uid": 7, "username": "tester"}
    try:
        with TestClient(app) as c:
            r = c.post("/api/tasks", json={
                "type": "image-gen",
                "params": {"model": "gemini-3.1-flash-image", "prompt": "x"},
            })
    finally:
        app.dependency_overrides.pop(require_session, None)

    assert r.status_code == 409
    body = r.json()
    assert body["success"] is False
    assert body["code"] == "MODEL_SUSPENDED"
    assert "gemini-3.1-flash-image" in body["message"]
    assert "detail" not in body, "必须返回统一响应壳，否则前端只能显示『请求失败(409)』"
    assert body["data"]["reason"] == "removed"


# ---------------------------------------------------------------------------
# 7) 模型级下架（suspendedModels，2026-09-17 飞哥：下架/上架解耦到具体模型）
# ---------------------------------------------------------------------------
def test_service_level_publish_ignores_suspended_models(store):
    """上架条目里被模型级下架的模型：不计入已发布、闸门拦截（reason=suspended）。"""
    asyncio.run(_put([_svc("s1", "A", ["m1", "m2", "m3"], suspendedModels=["m2"])]))
    state = asyncio.run(catalog.read_state(force=True))
    assert set(state["published"]) == {"m1", "m3"}
    with pytest.raises(ModelSuspendedError) as ei:
        asyncio.run(catalog.assert_model_available("m2"))
    assert ei.value.reason == "suspended"
    asyncio.run(catalog.assert_model_available("m1"))
    asyncio.run(catalog.assert_model_available("m3"))


def test_model_level_resume_restores_gate(store):
    """模型级下架 → 恢复：重新进入已发布集，撤回集自愈放行。"""
    asyncio.run(_put([_svc("s1", "A", ["m1", "m2"], suspendedModels=["m1"])]))
    with pytest.raises(ModelSuspendedError):
        asyncio.run(catalog.assert_model_available("m1"))
    asyncio.run(_put([_svc("s1", "A", ["m1", "m2"])]))
    asyncio.run(catalog.assert_model_available("m1"))


def test_suspended_whole_service_ignores_suspended_models(store):
    """整体下架 + 模型级清单并存：全部模型都拦，互不冲突。"""
    asyncio.run(_put([_svc("s1", "A", ["m1", "m2"], suspended=True, suspendedModels=["m2"])]))
    for m in ("m1", "m2"):
        with pytest.raises(ModelSuspendedError):
            asyncio.run(catalog.assert_model_available(m))


def test_sanitize_keeps_suspended_models_clean(store):
    """白名单放行 + 类型兜底：非列表丢弃、空串剔除、去重保序。"""
    asyncio.run(_put([{"id": "s1", "name": "A", "models": ["m1", "m2"],
                       "suspendedModels": ["m2", "m2", " ", "m1"]}]))
    svc = store.doc["payload"]["services"][0]
    assert svc["suspendedModels"] == ["m2", "m1"]

    asyncio.run(_put([{"id": "s1", "name": "A", "models": ["m1"], "suspendedModels": "m2"}]))
    assert "suspendedModels" not in store.doc["payload"]["services"][0]
