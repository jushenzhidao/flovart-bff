import asyncio, json, sys
sys.path.insert(0, r'D:/code/flovart-bff/flovart-bff')

async def main():
    from app import platform_catalog as catalog
    state = await catalog.read_state(force=True)
    services = state.get("services", [])
    print("=== 平台服务条目总数:", len(services))
    for s in services:
        caps = s.get("capabilities") or []
        models = s.get("models") or s.get("customModels") or []
        print("\n--- 条目:", s.get("name"), "| id:", s.get("id"))
        print("    capabilities:", caps)
        print("    models:", models)
        print("    defaultModel:", s.get("defaultModel"))
        print("    suspended:", s.get("suspended"))
        print("    baseUrl:", s.get("baseUrl") or s.get("gatewayBaseUrl"))
        k = s.get("apiKey") or s.get("key") or ""
        print("    apiKey前缀:", (k[:8] + "...len=" + str(len(k))) if k else "(空)")
    from app.routers import platform_services as ps
    try:
        mode = await ps.get_key_mode()
        print("\n=== platform-key-mode:", mode)
    except Exception as e:
        print("key mode err:", e)

asyncio.run(main())
