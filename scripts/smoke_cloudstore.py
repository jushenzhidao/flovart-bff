"""本地兜底冒烟：未配云（OSS_ENABLED=false / 无 PG）时，cloudstore 与 tasks 纯函数的闭环。

覆盖：
  1) KV 文档读写 / 乐观锁 revision 自增 / 列表
  2) 媒体写入-读取-删除
  3) 请求日志（image task 双模式）：put(async/sync) → get → list → update(status) → get
  4) tasks 纯函数：_normalize_sync_result 多形态、_iter_outputs 收集产物
用法：BFF_SKIP_DOTENV=1 由脚本内强制设置；临时 DATA_DIR 落本地 SQLite + 文件。
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["BFF_SKIP_DOTENV"] = "1"
os.environ.pop("POSTGRES_DSN", None)
os.environ["POSTGRES_HOST"] = ""
os.environ["OSS_ENABLED"] = "false"
_tmp = tempfile.mkdtemp(prefix="flovart_smoke_")
os.environ["BFF_DATA_DIR"] = _tmp

from app import cloudstore, tasks  # noqa: E402


async def main():
    uid = 1001
    # 1) 文档
    p = await cloudstore.doc_put(uid, "projects", "p1", {"name": "x"})
    assert p["revision"] == 1, p
    p2 = await cloudstore.doc_put(uid, "projects", "p1", {"name": "y"})
    assert p2["revision"] == 2, p2
    got = await cloudstore.doc_get(uid, "projects", "p1")
    assert got["payload"]["name"] == "y", got
    lst = await cloudstore.doc_list(uid, "projects")
    assert any(d["doc_key"] == "p1" for d in lst), lst
    print("[OK] doc put/get/list + revision")

    # 2) 媒体
    blob = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    info = await cloudstore.media_put(uid, "image", "image/png", blob)
    assert info["url"].startswith("/api/me/media/"), info
    media = await cloudstore.media_get(uid, info["media_key"])
    assert media and media["path"], media
    deleted = await cloudstore.media_delete(uid, info["media_key"])
    assert deleted, "media delete"
    print("[OK] media put/get/delete")

    # 3) 请求日志（双模式）
    rid_a = "req_async_001"
    await cloudstore.request_log_put(uid, rid_a, "image-gen", "openai", "gpt-image",
                                     {"model": "gpt-image", "prompt": "cat"}, status="submitted", mode="async")
    await cloudstore.request_log_update(rid_a, status="processing", task_id="gw_task_9")
    row_a = await cloudstore.request_log_get(rid_a)
    assert row_a["uid"] == uid and row_a["status"] == "processing" and row_a["task_id"] == "gw_task_9", row_a
    assert row_a["payload"]["prompt"] == "cat", row_a

    rid_s = "req_sync_001"
    await cloudstore.request_log_put(uid, rid_s, "upscale", None, "upscale-v1",
                                     {"image": "x", "scale": 2}, status="submitted", mode="sync")
    await cloudstore.request_log_update(rid_s, status="succeeded", task_id=rid_s,
                                        gateway_request_id="gw-req-abc",
                                        result={"url": "/api/me/media/zzz"})
    row_s = await cloudstore.request_log_get(rid_s)
    assert row_s["status"] == "succeeded" and row_s["gateway_request_id"] == "gw-req-abc", row_s
    assert row_s["result"]["url"] == "/api/me/media/zzz", row_s

    listing = await cloudstore.request_log_list(uid, limit=10, offset=0)
    assert len(listing) == 2, listing
    assert {r["request_id"] for r in listing} == {rid_a, rid_s}, listing
    print("[OK] request_log put/update/get/list (async+sync)")

    # 4) tasks 纯函数：同步结果归一化 + 产物收集
    r1 = tasks._normalize_sync_result({"data": [{"url": "http://g/x.png"}]})
    assert r1["data"][0]["url"] == "http://g/x.png", r1
    r2 = tasks._normalize_sync_result({"layers": [{"name": "a", "url": "u"}], "image": {"url": "i"}})
    assert "layers" in r2 and "image" in r2, r2
    r3 = tasks._normalize_sync_result({"url": "http://g/single.png"})
    assert r3["url"] == "http://g/single.png", r3

    outs = tasks._iter_outputs({"data": [{"url": "a"}, {"b64_json": "YQ=="}],
                                "image": {"url": "b"}})
    assert len(outs) == 3, outs  # data 两个 + image 一个
    gid = tasks._extract_gw_request_id({"request_id": "gw-123"})
    assert gid == "gw-123", gid
    print("[OK] tasks._normalize_sync_result / _iter_outputs / _extract_gw_request_id")

    print("\nALL SMOKE PASSED  (DATA_DIR=%s)" % _tmp)


if __name__ == "__main__":
    asyncio.run(main())
