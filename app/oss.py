"""外部对象存储适配层（OSS / COS / S3 兼容，boto3 S3 协议）。

BFF 联网读写远端存储，不落本地磁盘：
- 写：服务端 put_object（前端经 BFF 上传，BFF 再写 OSS；无 CORS 需求）
- 读：presigned URL（前端带 Cookie 请求 /api/me/media/{key}，BFF 307 重定向到 OSS；
      因此需为 bucket 配置 CORS，允许前端域名的 GET）

三大云差异只靠 env 消除（endpoint / region / addressing_style / provider）。
"""
import logging

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from . import config

logger = logging.getLogger("bff.oss")

_CLIENT = None


def get_client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = boto3.client(
            "s3",
            endpoint_url=config.OSS_ENDPOINT or None,
            aws_access_key_id=config.OSS_ACCESS_KEY,
            aws_secret_access_key=config.OSS_SECRET_KEY,
            region_name=config.OSS_REGION or None,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                s3={"addressing_style": config.OSS_ADDRESSING_STYLE or "auto"},
            ),
        )
    return _CLIENT


def is_configured() -> bool:
    return bool(config.OSS_BUCKET and config.OSS_ACCESS_KEY and config.OSS_SECRET_KEY)


def ping() -> "tuple[bool, str]":
    if not is_configured():
        return False, "OSS 未配置（缺 bucket / AK / SK）"
    try:
        get_client().head_bucket(Bucket=config.OSS_BUCKET)
        return True, "ok"
    except ClientError:
        # ⚠️ 部分网关/反代拦截 HEAD 方法（实测 cn.s3ai.cn：GET/PUT 放行、HEAD 一律 403）。
        # 降级用 list_objects_v2（GET 语义）探测桶可达性，不因 HEAD 被拦而误判不可用。
        try:
            get_client().list_objects_v2(Bucket=config.OSS_BUCKET, MaxKeys=1)
            return True, "ok(head 被网关拦截，list 降级探测通过)"
        except ClientError as e2:
            code = e2.response.get("Error", {}).get("Code")
            return False, f"bucket 不可访问({code})"
        except Exception as e2:  # noqa: BLE001
            return False, f"OSS 连接失败: {e2}"
    except Exception as e:  # noqa: BLE001
        return False, f"OSS 连接失败: {e}"


# --------------------------------------------------------------------------
# 键构造（统一前缀隔离多环境 / 多租户）
# --------------------------------------------------------------------------
def user_prefix(uid: int) -> str:
    p = config.OSS_PREFIX.strip().strip("/")
    return f"{p}/users/{uid}/" if p else f"users/{uid}/"


def doc_key(uid: int, scope: str, doc_key: str) -> str:
    return f"{user_prefix(uid)}docs/{scope}/{doc_key}.json"


def media_key(uid: int, media_key: str) -> str:
    return f"{user_prefix(uid)}media/{media_key}"


# --------------------------------------------------------------------------
# 基础操作
# --------------------------------------------------------------------------
def put_object(key: str, data: bytes, content_type: str = "application/octet-stream",
               metadata: "dict | None" = None) -> None:
    extra = {"Metadata": {k: str(v) for k, v in (metadata or {}).items()}}
    get_client().put_object(Bucket=config.OSS_BUCKET, Key=key, Body=data,
                            ContentType=content_type, **extra)


def get_object_bytes(key: str) -> "tuple[bytes, dict]":
    """返回 (body, 响应元数据)。404 → 抛 ClientError（调用方判空）。"""
    obj = get_client().get_object(Bucket=config.OSS_BUCKET, Key=key)
    return obj["Body"].read(), obj


def head_object_meta(key: str) -> "dict | None":
    try:
        return get_client().head_object(Bucket=config.OSS_BUCKET, Key=key)
    except ClientError:
        # 部分网关/CDN（nginx 反代等）会拦截 HEAD 方法（403），GET 却放行。
        # 降级：list_objects_v2 精确判存在（S3 对精确前缀是索引查询，零下载）。
        # 307 架构下 mime/size 由 OSS 对 presigned URL 的最终 GET 响应提供，此处只需存在性。
        try:
            r = get_client().list_objects_v2(Bucket=config.OSS_BUCKET, Prefix=key, MaxKeys=1)
            for o in r.get("Contents", []):
                if o["Key"] == key:
                    return {"ContentLength": int(o.get("Size", 0)),
                            "ContentType": "application/octet-stream"}
        except ClientError:
            pass
        return None


def delete_object(key: str) -> bool:
    try:
        get_client().delete_object(Bucket=config.OSS_BUCKET, Key=key)
        return True
    except ClientError:
        return False


def presign_get(key: str, ttl: int) -> str:
    return get_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": config.OSS_BUCKET, "Key": key},
        ExpiresIn=ttl,
    )


def list_prefix(prefix: str) -> "list[tuple[str, int]]":
    """返回 [(key, size), ...]（自动翻页）。"""
    c = get_client()
    out: "list[tuple[str, int]]" = []
    try:
        for page in c.get_paginator("list_objects_v2").paginate(
            Bucket=config.OSS_BUCKET, Prefix=prefix
        ):
            for o in page.get("Contents", []):
                out.append((o["Key"], int(o.get("Size", 0))))
    except ClientError as e:
        logger.warning("list_prefix 失败 prefix=%s: %s", prefix, e)
    return out
