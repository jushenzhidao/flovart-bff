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
