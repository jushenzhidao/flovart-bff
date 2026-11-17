# -*- coding: utf-8 -*-
"""模拟浏览器直连形态：只带 Authorization 不带 New-Api-User，验证是否 401。"""
import asyncio

import httpx

from app import newapi_client as na
from app import config


async def main():
    uid = 1
    pat = na._admin_cache['pat']
    d = await na.list_tokens(pat, uid, page=1, size=1)
    t0 = d['items'][0]
    plain = await na.get_token_key(pat, uid, t0['id'])
    base = config.NEWAPI_BASE_URL.rstrip('/')
    body = {'model': 'claude-sonnet-4-6',
            'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 8}

    async with httpx.AsyncClient(timeout=30) as cli:
        # 形态 A：浏览器现状 —— 只带 Authorization
        r = await cli.post(f'{base}/v1/chat/completions',
                           headers={'Authorization': f'Bearer {plain}'}, json=body)
        print(f'A single-header (browser-like): HTTP {r.status_code} {r.text[:150]}')

        # 形态 B：双头（BFF 服务端标准形态）
        r = await cli.post(f'{base}/v1/chat/completions',
                           headers={'Authorization': f'Bearer {plain}', 'New-Api-User': str(uid)},
                           json=body)
        print(f'B dual-header (BFF-like):      HTTP {r.status_code} {r.text[:150]}')


asyncio.run(main())
