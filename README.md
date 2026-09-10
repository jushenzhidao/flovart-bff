# flovart-bff

Flovart 在线创作站的 BFF 层（FastAPI）：
**用户登录 / 数据持久化 / 对接 new-api 的用户·渠道·模型**。

完整方案见 [ARCHITECTURE.md](ARCHITECTURE.md)。参考实现为
`D:\code\hewapi-bff\newapi-bff`，本仓库移植了其已验证的通用件
（加密会话、new-api 客户端、注册影子建号、JSON 原子写持久化）。

## 启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip install ...
cp .env.example .env                        # 填入 BFF_SECRET_KEY / NEWAPI_ADMIN_*
uvicorn app.main:app --host 127.0.0.1 --port 8300 --reload
```

本地开发起服务时务必加 `BFF_COOKIE_SECURE=0`（明文 http 下 Secure Cookie
会被浏览器丢弃，登录会静默失败）。

## 冒烟测试

```bash
.venv/bin/pytest tests/ -q
```

## 目录

| 路径 | 职责 |
|---|---|
| `app/main.py` | FastAPI 组装 + `/healthz` `/readyz` |
| `app/config.py` | 环境变量配置（hewapi 同款解析约定） |
| `app/security.py` | AES-256-GCM 加密会话 Cookie（hewapi 原样移植） |
| `app/newapi_client.py` | new-api 客户端：PAT+uid 双头 / 登录即归还会话 / 管理员三通道 |
| `app/routers/` | auth（注册登录）/ keys（API Key）/ usage（日志用量）/ console（用户·渠道·模型管理台） |
| `app/promo.py` | 注册赠送（JSON 状态文件保证幂等） |
| `data/` | 运行期状态 JSON（不入库） |

## 当前进度

- [x] M0：工程骨架 + 认证用户域（注册=影子建号+赠送+自动登录，登录，self）
- [x] M0：keys / usage / console 路由
- [x] M1：console 契约对真实实例实测（用户 54 / 渠道 17 / 模型 43）并修正
      （渠道 models 字符串拆分、启停 POST /:id/status、测试 GET /test/:id、
      models_enabled 端点、用户/渠道列表白名单脱敏）
- [ ] M2：Flovart 前端 fork 适配（见 ARCHITECTURE.md §6）
- [ ] M3：生产化部署（Docker 多阶段 + compose + Nginx/HTTPS）
