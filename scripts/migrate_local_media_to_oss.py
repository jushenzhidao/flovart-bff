#!/usr/bin/env python3
"""一次性搬迁：容器卷里的本地媒体字节 → S3 兼容对象存储（MinIO / OSS / COS）。

背景：BFF 的字节后端是硬切换（OssBlob / LocalBlob，cloudstore.py:900），
打开 OSS_ENABLED 后本地卷 /data/media 里的存量字节不会被读到，必须先搬。

在 BFF 容器内执行（同 compose 网络可达 MinIO，且已挂载 flovart-bff-data:/data）：

    # 先试跑（只统计，不上传）：
    docker compose run --rm bff python scripts/migrate_local_media_to_oss.py --dry-run

    # 正式搬迁（可安全重复执行，已上传的对象自动跳过）：
    docker compose run --rm bff python scripts/migrate_local_media_to_oss.py

原则：
- 以 PG cloud_media 索引为准（uid, media_key）：索引里有的才搬，孤儿文件忽略。
- 本地路径：{BFF_DATA_DIR}/media/{uid}/{key[:2]}/{key}（LocalBlob._path 布局）。
- 目标键：{OSS_PREFIX}/users/{uid}/media/{key}（与 oss.media_key 完全一致）。
- 幂等：目标对象已存在（HEAD 200）则跳过，不重复传。
- 迁移前 .env 必须已配好 OSS_*（OSS_ENABLED=1 等），脚本用同一套配置。
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402
import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402
from botocore.exceptions import ClientError, EndpointConnectionError  # noqa: E402

from app import config  # noqa: E402


async def main(dry_run: bool) -> int:
    if not config.USE_PG:
        print("❌ POSTGRES 未配置（USE_PG=False），请在配置了 PG 的容器内运行。")
        return 2
    if not config.OSS_ENABLED or not config.OSS_BUCKET:
        print("❌ OSS 未启用：请先在 .env 配置 OSS_ENABLED=1 / OSS_BUCKET 等并重建容器。")
        return 2

    s3 = boto3.client(
        "s3",
        endpoint_url=config.OSS_ENDPOINT or None,
        aws_access_key_id=config.OSS_ACCESS_KEY,
        aws_secret_access_key=config.OSS_SECRET_KEY,
        region_name=config.OSS_REGION or "us-east-1",
        config=Config(signature_version="s3v4",
                      s3={"addressing_style": config.OSS_ADDRESSING_STYLE or "auto"}),
    )
    try:
        s3.head_bucket(Bucket=config.OSS_BUCKET)
    except EndpointConnectionError:
        print(f"❌ OSS 连不通：{config.OSS_ENDPOINT}")
        return 2
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchBucket"):
            print(f"❌ bucket {config.OSS_BUCKET} 不存在，请先在控制台创建。")
        else:
            print(f"❌ bucket 访问失败：{code}（AK/SK 或权限问题）")
        return 2

    media_root = Path(config.DATA_DIR) / "media"
    print(f"源目录: {media_root}")
    print(f"目标:   bucket={config.OSS_BUCKET} prefix={config.OSS_PREFIX} "
          f"endpoint={config.OSS_ENDPOINT or '(AWS 默认)'}")

    pool = await asyncpg.create_pool(dsn=config.POSTGRES_DSN_RESOLVED,
                                     min_size=1, max_size=3)
    total = ok = skip = miss = 0
    miss_rows: list[str] = []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT uid, media_key FROM cloud_media ORDER BY uid, media_key")
        total = len(rows)
        print(f"索引共 {total} 条媒体。\n")

        for i, row in enumerate(rows, 1):
            uid, key = row["uid"], row["media_key"]
            src = media_root / str(uid) / key[:2] / key
            dst = oss_media_key(uid, key)
            if not src.exists():
                miss += 1
                if len(miss_rows) < 10:
                    miss_rows.append(f"users/{uid}/media/{key}")
                continue
            if dry_run:
                ok += 1
                continue
            if _exists(s3, dst):
                skip += 1
                continue
            data = src.read_bytes()
            # mime 用通用值：读取走 BFF 307 + 浏览器嗅探，LocalBlob 时代也不存 mime
            s3.put_object(Bucket=config.OSS_BUCKET, Key=dst, Body=data,
                          ContentType="application/octet-stream")
            ok += 1
            if i % 100 == 0:
                print(f"  进度 {i}/{total}（上传 {ok}，跳过 {skip}）")
    finally:
        await pool.close()

    print(f"\n{'[dry-run] ' if dry_run else ''}完成：索引 {total} 条，"
          f"{'可上传' if dry_run else '已上传/已存在'} {ok}，"
          f"目标已存在跳过 {skip}，本地缺文件 {miss}。")
    if miss_rows:
        print("缺文件示例（索引有、卷里没有，多为历史清理残留，不影响未缺部分）：")
        for m in miss_rows:
            print(f"  - {m}")
    if not dry_run:
        print("提示：本脚本幂等，可重复执行；确认网站素材全部可见后再清理本地卷。")
    return 0


def _exists(s3, key: str) -> bool:
    try:
        s3.head_object(Bucket=config.OSS_BUCKET, Key=key)
        return True
    except ClientError:
        return False


def oss_media_key(uid: int, key: str) -> str:
    """与 app/oss.py media_key() 完全一致的键构造。"""
    p = config.OSS_PREFIX.strip().strip("/")
    return f"{p}/users/{uid}/media/{key}" if p else f"users/{uid}/media/{key}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="本地媒体字节 → 对象存储 一次性搬迁")
    ap.add_argument("--dry-run", action="store_true", help="只统计不上传")
    sys.exit(asyncio.run(main(ap.parse_args().dry_run)))
