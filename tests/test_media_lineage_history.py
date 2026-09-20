"""媒体血缘列 + 历史记录接口（本地行为测试：不触网）。

覆盖：
- LocalMeta 存量库迁移：旧 cloud_media 自动补血缘列（source_request_id 等）
- media_put / media_index_put 记录血缘（source_request_id / source_kind），media_index_get 可读回
- reqlog_history：分页 / total / kind 过滤 / uid 隔离 / b64 剥离
- GET /api/me/history/records 端点行为（直调协程，绕过鉴权依赖）
"""
import asyncio
import os
import sqlite3
import tempfile

import pytest

from app import cloudstore, config
from app.routers import cloud as cloud_router


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def local_env(monkeypatch):
    """全新临时 DATA_DIR + 全新 LocalMeta 单例，避免与其他测试共享线程连接。"""
    d = tempfile.mkdtemp(prefix="flovart-lineage-")
    monkeypatch.setattr(config, "DATA_DIR", d)
    monkeypatch.setattr(cloudstore, "META", cloudstore.LocalMeta())
    return d


# ---------- ① 存量库迁移 ----------

def test_local_media_migration_adds_lineage_columns(local_env):
    db_path = os.path.join(local_env, "flovart_cloud.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE cloud_media(
            uid INTEGER NOT NULL, media_key TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'media',
            mime TEXT NOT NULL DEFAULT 'application/octet-stream', size INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX idx_cloud_media_uid ON cloud_media(uid);
    """)
    conn.commit()
    conn.close()

    meta = cloudstore.LocalMeta()  # 首次 _connect 触发迁移
    _run(meta.media_index_put(7, "k0", "image", "image/png", 10))

    cols = {r[1] for r in sqlite3.connect(db_path).execute("PRAGMA table_info(cloud_media)")}
    assert {"source_request_id", "source_kind", "width", "height",
            "thumb_key", "deleted_at"} <= cols
    assert any(r[1] == "idx_cloud_media_src" for r in
               sqlite3.connect(db_path).execute("PRAGMA index_list(cloud_media)"))


# ---------- ② 血缘写入 + 读回 ----------

def test_media_put_records_lineage(local_env):
    info = _run(cloudstore.media_put(
        7, "image", "image/png", b"\x89PNG-fake",
        source_request_id="req-abc", source_kind="generated"))
    got = _run(cloudstore.META.media_index_get(7, info["media_key"]))
    assert got["source_request_id"] == "req-abc"
    assert got["source_kind"] == "generated"

    # 无血缘调用（如旧路径）不炸、字段为 None
    info2 = _run(cloudstore.media_put(7, "image", "image/png", b"\x89PNG-2"))
    got2 = _run(cloudstore.META.media_index_get(7, info2["media_key"]))
    assert got2["source_request_id"] is None
    assert got2["source_kind"] is None


# ---------- ③ 历史记录 ----------

def _seed(uid, request_id, kind="image-gen", with_result=False):
    payload = {"prompt": "cat", "model": "gpt-image-2"}
    if request_id == "r-b64":
        payload = dict(payload, b64_json="A" * 1000)
    _run(cloudstore.request_log_put(
        uid, request_id, kind, "gateway", "gpt-image-2", payload,
        status="submitted", mode="async"))
    if with_result:
        _run(cloudstore.request_log_update(
            request_id, status="succeeded",
            result={"data": [{"b64_json": "B" * 1000}]}))


def test_request_log_history_pagination_and_isolation(local_env):
    _seed(7, "r1")
    _seed(7, "r2")
    _seed(8, "r3")  # 别的 uid，不得出现
    _seed(7, "v1", kind="video-gen")

    data = _run(cloudstore.request_log_history(7, limit=2, offset=0))
    assert data["total"] == 3
    ids = [it["request_id"] for it in data["items"]]
    assert len(ids) == 2 and "r3" not in ids

    page2 = _run(cloudstore.request_log_history(7, limit=2, offset=2))
    assert page2["total"] == 3 and len(page2["items"]) == 1

    only_img = _run(cloudstore.request_log_history(7, limit=10, offset=0, kind="image-gen"))
    assert only_img["total"] == 2
    only_vid = _run(cloudstore.request_log_history(7, limit=10, offset=0, kind="video-gen"))
    assert only_vid["total"] == 1 and only_vid["items"][0]["request_id"] == "v1"


def test_request_log_history_strips_b64(local_env):
    _seed(7, "r-b64", with_result=True)
    data = _run(cloudstore.request_log_history(7, limit=10, offset=0))
    item = data["items"][0]
    assert item["payload"]["b64_json"] == "<base64:1000 chars>"
    assert item["result"]["data"][0]["b64_json"] == "<base64:1000 chars>"
    assert item["payload"]["prompt"] == "cat"  # 正常字段不受影响


# ---------- ④ 端点 ----------

def test_history_records_endpoint(local_env):
    _seed(7, "r1")
    resp = _run(cloud_router.history_records(
        limit=10, offset=0, kind="", session={"uid": 7}))
    assert resp["success"] is True
    assert resp["data"]["total"] == 1
    assert resp["data"]["items"][0]["request_id"] == "r1"
    assert resp["data"]["items"][0]["payload"]["prompt"] == "cat"
