"""一次性 e2e：复刻 BYOK（_gateway）形态的 upscale 请求，验证改道 wavespeed。"""
import asyncio, base64, json

png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')
dataurl = 'data:image/png;base64,' + base64.b64encode(png).decode()

from app import tasks, config
print('WAVESPEED_ENABLED =', config.WAVESPEED_ENABLED)

params = {
    'model': 'gemini-3.1-flash-image-preview',
    'task': 'upscale',
    'image': [dataurl],
    'options': {'targetLongEdge': 4096, 'algorithm': 'high'},
    'mode': 'sync',
    '_gateway': {'base_url': 'https://workbuddy.oneapis.cn/v1', 'api_key': 'sk-dummy-for-e2e'},
}


async def main():
    view = await tasks.submit(183, 'image-gen', params)
    print('submit view:', json.dumps({k: v for k, v in view.items() if k != 'result'}, ensure_ascii=False))
    rid = view['id']
    for i in range(40):
        t = await tasks.get_task(rid, 183)
        st = t.get('status')
        if st in ('succeeded', 'failed'):
            r = t.get('result') or {}
            imgs = r.get('images') or []
            print('final status:', st)
            print('image url:', imgs[0].get('url') if imgs else '(none)')
            if not imgs:
                print('result:', json.dumps(r, ensure_ascii=False)[:500])
            return
        await asyncio.sleep(3)
    print('timeout still processing')


asyncio.run(main())
