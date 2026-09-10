"""统一响应助手：{success, message, data} 响应壳 + 客户端 IP 提取。"""
from fastapi import Request
from fastapi.responses import JSONResponse


def ok(data=None, message: str = ""):
    return {"success": True, "message": message, "data": data}


def fail(message: str, status_code: int = 400):
    return JSONResponse(status_code=status_code, content={"success": False, "message": message})


def client_ip(request: Request) -> str:
    """取真实客户端 IP，转发给 new-api 用于按 IP 限流计数。

    BFF 部署在 Nginx 之后时，X-Forwarded-For 第一段才是真实用户 IP。
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""
