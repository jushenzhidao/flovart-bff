"""冒烟测试：只验证「本地行为」，不触网。

- 探针（healthz/readyz/root）
- 站点配置（免登录）
- 鉴权边界：未登录访问受保护接口 → 401/403
- 注册参数本地校验（不触发真实建号）
"""
from fastapi.testclient import TestClient

from app.main import app


def _client():
    return TestClient(app)


def test_healthz():
    with _client() as c:
        r = c.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["service"] == "flovart-bff"


def test_root():
    with _client() as c:
        r = c.get("/")
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_readyz_passes_with_test_env():
    """夹具已配 64 字符密钥 + 管理员凭证 + 可写临时目录 → readyz 应 200。"""
    with _client() as c:
        r = c.get("/readyz")
    assert r.status_code == 200, r.text
    checks = r.json()["checks"]
    assert checks["secret_key_configured"][0] is True
    assert checks["admin_cred_configured"][0] is True
    assert checks["state_dir_writable"][0] is True


def test_site_config_public():
    with _client() as c:
        r = c.get("/api/config")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["features"]["hosted"] is True
    assert data["service"] == "flovart-bff"
    assert data["points"]["per_cny"] > 0


def test_self_requires_session():
    with _client() as c:
        r = c.get("/api/user/self")
    assert r.status_code == 401


def test_console_requires_session():
    with _client() as c:
        r = c.get("/api/console/overview")
    assert r.status_code == 401


def test_register_validates_locally_before_network():
    """非法用户名在本地拦截（400），不会发起真实建号请求。"""
    with _client() as c:
        r = c.post("/api/user/register",
                   json={"username": "a", "password": "short"})
    assert r.status_code == 400
    assert r.json()["success"] is False


# ---------------------------------------------------------------------------
# 回归：「模型目录」必须按用户分组过滤（否则「列表里有、点下去调不通」）
#
# 事故背景（2026-09-11）：普通用户模型选择器里出现 doubao-seedream-*，但调用
# 报 `No available channel for model ... under group default` —— 因为
# /api/models 用的是 admin_enabled_models()（全站启用模型、不按分组过滤）。
# 实测差异：default 分组 /v1/models 返回 31 个（图片类 0），全站列表 52 个。
#
# 契约：/api/models 必须优先用「用户自己的 sk-」查网关 /v1/models（按分组过滤），
# 失败时才回落全站列表。这里做源码级断言，防止有人改回全站列表。
# ---------------------------------------------------------------------------
def test_model_catalog_uses_user_scoped_query():
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "app" / "routers" / "keys.py"
    text = src.read_text(encoding="utf-8")

    at = text.index('def user_model_catalog')
    body = text[at:at + 2200]

    # 必须调用用户态查询（按分组过滤）
    assert 'user_available_models(' in body, "模型目录必须用用户 sk- 查询（按分组过滤）"
    # 且调用的是「当前用户的平台 Key」，不是管理员凭证
    assert '_ensure_token_plain(' in body, "模型目录应复用当前用户的平台 Key"
    # 全站列表只能作为降级兜底，必须出现在 try/except 之后
    assert 'admin_enabled_models()' in body, "应保留全站列表作为降级兜底"


def test_user_available_models_hits_v1_models():
    """user_available_models 必须打 /v1/models（按分组过滤的端点），且只用 sk- 鉴权。"""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "app" / "newapi_client.py"
    text = src.read_text(encoding="utf-8")

    at = text.index('async def user_available_models')
    body = text[at:at + 1600]
    # 剥掉 docstring（里面会提到 New-Api-User 作为反例说明），只看实现代码
    lines = body.split("\n")
    out, in_doc = [], False
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            in_doc = not in_doc
            if stripped.count('"""') == 2 or stripped.count("'''") == 2:
                in_doc = False
            continue
        if not in_doc:
            out.append(ln)
    code = "\n".join(out)

    assert '"/v1/models"' in code, "必须调用网关 /v1/models"
    assert 'Bearer' in code, "必须以 Bearer sk- 鉴权"
    # /v1 只认 sk-：不得带管理员用户头（key 自归属该用户）
    assert 'New-Api-User' not in code, "/v1/models 不应带管理员用户头"


# ---------------------------------------------------------------------------
# 平台 AI 服务（2026-09-14 语义重塑：模型清单，不含密钥）
#
# 诉求：「管理员在设置页配好模型 → 用户能选到 → 用【自己的默认 Key】调用」。
# 契约：GET 所有登录用户可读（返回模型清单 + 网关基址）；PUT 仅管理员可写；
#       两者都走同一份 cloud_docs(uid=0, scope='platform') 全局文档。
#       **不再下发 key/baseUrl** —— 用户 Key 与网关基址由 BFF 侧统一。
# ---------------------------------------------------------------------------
def test_platform_services_endpoints_registered():
    from app.main import app

    # 用 OpenAPI 视图而非 app.routes —— 本 FastAPI 版本把 include_router 的
    # 子路由包成 _IncludedRouter（无 path 属性），直接遍历 app.routes 看不到。
    spec = app.openapi()
    path = spec["paths"].get("/api/platform/services")
    assert path is not None, "平台服务端点必须注册"
    assert "get" in path, "需提供 GET（所有登录用户可读）"
    assert "put" in path, "需提供 PUT（仅管理员可写）"


def test_platform_services_read_vs_write_permission():
    """读=require_session（全员），写=require_admin（仅管理员）—— 不能写反。"""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "app" / "routers" / "platform_services.py"
    text = src.read_text(encoding="utf-8")

    read_at = text.index("async def list_platform_services")
    read_body = text[read_at:read_at + 900]
    assert "require_session" in read_body, "读取必须全员可用（require_session）"

    write_at = text.index("async def put_platform_services")
    write_body = text[write_at:write_at + 700]
    assert "require_admin" in write_body, "写入必须限管理员（require_admin）"


def test_platform_services_global_scope_is_uid_zero():
    """全局配置必须落在 uid=0（保留维度），而非任何真实用户 uid。"""
    from app.routers import platform_services as ps

    assert ps.PLATFORM_UID == 0, "全局配置必须用保留 uid=0"
    assert ps.PLATFORM_SCOPE == "platform"
    assert ps.PLATFORM_DOC_KEY == "services"


def test_platform_services_list_returns_gateway_base_url():
    """GET 必须下发网关基址 —— 前端靠它把「用户自己的 sk-」打到正确网关。

    若不发，前端只能猜 baseUrl，一旦与用户 sk- 不同源就必然 401。
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "app" / "routers" / "platform_services.py"
    text = src.read_text(encoding="utf-8")
    read_at = text.index("async def list_platform_services")
    read_body = text[read_at:read_at + 900]
    assert "gatewayBaseUrl" in read_body, "必须下发 gatewayBaseUrl（与用户 sk- 同源）"
    assert "config.API_BASE_URL" in read_body


def test_platform_services_sanitize_strips_key_and_base_url():
    """⭐ 关键回归（2026-09-14）：平台服务**不得**落库/下发密钥与 Base URL。

    旧语义曾把管理员 key 明文下发给所有账号（泄漏面 = 任何登录用户），
    且直连外部网关**绕开 new-api 计费**（平台白付成本）。
    新语义下调用一律用用户自己的 Key + BFF 网关基址。
    """
    from app.routers.platform_services import _sanitize

    out = _sanitize({
        "id": "svc-1",
        "provider": "openai_compatible",
        "name": "平台模型",
        "baseUrl": "https://api.chatfire.cn/v1",
        "key": "sk-test",
        "capabilities": ["image"],
        "models": ["gpt-image-2"],
        "extraConfig": {"flovart_platform": "1", "requestFormat": "openai"},
        "someLocalField": 123,
    }, "admin")

    assert "key" not in out, "密钥绝不允许落库/下发"
    assert "baseUrl" not in out, "Base URL 由 BFF 统一（用户 Key 必须与网关同源）"
    assert out["extraConfig"].get("flovart_platform") is None, "必须剔除平台 Key 池标记"
    assert out["extraConfig"].get("_gateway") is None, "不得带 _gateway（会绕开计费）"
    assert out["extraConfig"].get("requestFormat") == "openai", "保留合法 extraConfig"
    assert out["models"] == ["gpt-image-2"], "模型清单必须保留（这是平台服务的核心）"
    assert out["customModels"] == ["gpt-image-2"], "customModels 与 models 双向同步"
    assert out["updatedBy"] == "admin"
    assert "someLocalField" not in out, "白名单外字段不得下发"


def test_platform_services_redact_never_leaks_key():
    """下发（_redact）同样必须剔除 key/baseUrl —— 兼容库里残留的旧数据。"""
    from app.routers.platform_services import _redact

    out = _redact({
        "id": "svc-1",
        "name": "平台模型",
        "key": "sk-legacy-leftover",
        "baseUrl": "https://api.chatfire.cn/v1",
        "customModels": ["gpt-image-2"],
    })
    assert "key" not in out, "旧数据里的 key 也不能下发"
    assert "baseUrl" not in out
    assert out["models"] == ["gpt-image-2"], "从 customModels 回填 models（兼容旧数据）"
