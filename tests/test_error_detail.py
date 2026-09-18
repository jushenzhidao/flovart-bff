"""网络层/非 JSON 上游错误的 detail 透传（2026-09-18 图生图 502 血案）。

此前 httpx.HTTPError 被吞成通用文案「上游服务暂时不可用」，请求日志里
result.error 只有 message，查不到真实原因（连接重置/超时/413 等）。
现在 NewApiError.detail 携带底层原因，_error_record 落进日志。
"""
import asyncio

import httpx

from app import newapi_client as na
from app.newapi_client import NewApiError
from app.tasks import _error_record


class _BrokenClient:
    """模拟网关连接被掐（nginx 413/重置等场景）。"""

    def request(self, *args, **kwargs):
        raise httpx.ConnectError("connection reset by peer",
                                 request=httpx.Request("POST", "http://gw/v1/x"))


def test_request_network_error_carries_detail():
    with __import__("pytest").raises(NewApiError) as ei:
        asyncio.run(na.request("POST", "/v1/images/generations",
                               headers={}, json={}, client=_BrokenClient()))
    assert ei.value.status_code == 502
    assert ei.value.message == "上游服务暂时不可用，请稍后重试"
    assert "ConnectError" in ei.value.detail
    assert "connection reset by peer" in ei.value.detail


def test_error_record_includes_detail():
    exc = NewApiError("上游服务暂时不可用，请稍后重试", 502,
                      detail="ReadError: peer closed connection")
    err = _error_record(exc, "gateway_sync_error", "request", path="v1/images/generations")
    assert err["error"]["detail"] == "ReadError: peer closed connection"
    assert err["error"]["message"] == "上游服务暂时不可用，请稍后重试"


def test_error_record_omits_detail_when_absent():
    err = _error_record(NewApiError("操作失败", 400), "gateway_sync_error", "request")
    assert "detail" not in err["error"]
