# 自动化记忆：方案 B 生图镜像路由落地

## 2026-09-23 首次执行：✅ 全部完成
- BFF 镜像路由 4 条 + pytest 8 用例全过，全量 189 passed（tests/test_mirror_image_routes.py 新增）。
- 前端 imageTask.ts/aiGateway.ts 接入 mirrorMode（开关 USE_MIRROR_IMAGE_ROUTES=true），platformSuspension 21/21（更新了一处跨仓源码文本断言）、storageNamespaceIsolation 47/48（仅剩已知历史遗留 tasks:386）。
- BFF 8300 / vite 37522 已重启并探活。
- 真实生图冒烟两笔全成功：async 镜像 ≈42s succeeded、sync 镜像 ≈66s succeeded，产物落 BFF 盘可访问。
- 坑：BFF_COOKIE_SECURE 默认 true，httpx 脚本直连需手动带 Cookie 头；caplog 在 TestClient 线程下抓不到日志，用 spy 替代。
- 完整记录：`.workbuddy/memory/2026-09-23.md` 15:40 段。git 由飞哥自己 commit。
