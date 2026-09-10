# flovart-bff · Flovart 在线创作站架构与落地方案

> 轻量级应用 = **Flovart web 前端（在线创作站）+ BFF 层**。
> BFF 职责：用户登录、数据持久化、对接 new-api 的**用户 / 渠道 / 模型**。
> 参考实现：`D:\code\hewapi-bff\newapi-bff`（FastAPI BFF，成熟可用，直接移植通用件）。

---

## 1. 产品定位（已确认）

- **定位**：Flovart 在线创作站 —— 保留 Flovart 根 React 应用的工作台
  （Workflow 节点编排 / Table 媒体预处理 / Agent 空间）做图、做视频。
- **账号**：BFF 独立注册/登录，**注册 = 管理员在 new-api 影子建号 + 自动登录 +
  注册赠送积分**（hewapi 同款模式）。用户不再自带 Key —— 模型统一走运营方
  new-api 渠道，前端以 BFF 签发的会话拿配额/建 API Key。
- **持久化**：与 hewapi-bff 保持一致 —— BFF 自身业务状态落 **JSON 文件原子写**
  （tmp + os.replace + 锁），不用数据库。new-api 侧的用户/余额/令牌/日志由
  new-api 自己持久化。

## 2. 架构总览

```
浏览器 / Flovart React SPA(在线创作站)
   │  HTTP(S) 同域 /api/*（生产经 Nginx 反代，单域）
   ▼
┌──────────────────────── flovart-bff (FastAPI, 本仓库) ────────────────────────┐
│  app/main.py           组装：lifespan / 异常处理 / 探针 / 路由挂载              │
│  app/routers/auth.py   注册·登录·登出·self·站点配置         （BFF 逻辑）        │
│  app/routers/keys.py   API Key 列表/创建/明文/删除          （代理 new-api）    │
│  app/routers/usage.py  调用日志 + 用量统计                  （代理+积分换算）   │
│  app/routers/console.py 管理台：用户/渠道/模型/总览          （代理 new-api 管理 API）│
│  app/security.py       加密会话 Cookie（AES-256-GCM，移植自 hewapi）            │
│  app/newapi_client.py  new-api 对接：PAT+uid 双头/登录即归还/管理员三通道凭证    │
│  app/promo.py          注册赠送（状态文件幂等）              （BFF 自有状态）    │
│  app/store.py          JSON 原子写通用件                    （BFF 自有状态）    │
│  app/config.py         环境变量配置                          （同 hewapi 约定）  │
└──────────────────────────────┬──────────────────────────────────────────────────┘
                               ▼
                  new-api 网关（你的实例，已配渠道/模型）
                  用户表 / 渠道表 / 令牌 / 调用日志 / quota 余额
```

### 谁存什么（数据归属清晰，不重复造）

| 数据 | 存哪 | 说明 |
|---|---|---|
| 账号、密码、余额(quota)、令牌、日志 | **new-api** | 影子建号，一份数据源 |
| 会话（含用户 PAT） | **BFF Cookie** | 加密、HttpOnly、7 天，无服务端存储 |
| 注册赠送记录（幂等） | BFF `data/signup_bonus.json` | 防止重复发积分 |
| 管理员 PAT 缓存 | BFF `data/admin_cred.json` | 冷启复用，减少 login 会话消耗 |
| 站点/品牌/积分档配置 | 环境变量（`.env`） | 预留 `data/settings.json` 动态覆盖位 |
| 用户作品/项目 | **v1 仍在前端本地**（IndexedDB） | Flovart 本地优先；云端作品存储列 M4 待评估 |

> 注意：用户密码**不进 BFF** —— 登录密码只用于当场向 new-api 换 PAT；
> 影子建号密码只发往 new-api，BFF 不落盘。这是 hewapi 已验证的安全边界。

## 3. 仓库布局

```
D:\code\flovart-bff\            # 本仓库（BFF + 文档）
├── ARCHITECTURE.md             # 本文件
├── README.md                   # 启动/配置说明
├── requirements.txt / pyproject.toml / .env.example / .gitignore
├── app/
│   ├── main.py                 # FastAPI 组装 + /healthz /readyz
│   ├── config.py               # 环境变量配置
│   ├── security.py             # 会话加解密（hewapi 原样移植）
│   ├── newapi_client.py        # new-api 客户端（hewapi 精简 + 管理域扩展）
│   ├── observability.py        # 可观测性接缝（当前 no-op，Logfire 后续可接）
│   ├── store.py                # JSON 原子读写
│   ├── promo.py                # 注册赠送（幂等）
│   ├── resp.py                 # ok/fail/client_ip 响应助手
│   └── routers/{auth,keys,usage,console}.py
├── data/                       # 运行期状态（JSON，不入库）
└── tests/                      # pytest 冒烟测试
```

> 前端（Flovart 在线创作站）为另一个仓库：fork `D:\code\Flovart`
> （github.com/avabbbb/Flovart）→ 建议命名 `D:\code\flovart-web`，M2 阶段开工。
> BFF 与前端**同域部署**（前端静态资源 + BFF `/api` 由同一 Nginx 反代），
> 规避 CORS，Cookie 直接可用 —— 与 hewapi 单域发布哲学一致。

## 4. API 契约

响应壳统一：`{success: bool, message: str, data?: any}`；金额一律 **积分** 口径，
`quota` 不外泄（`config.quota_to_points*` 换算，1 元 = 500000 quota = 默认 10000 积分）。

### 4.1 认证与用户（auth.py）—— 面向创作站用户

| 方法/路径 | 说明 | new-api 依赖 |
|---|---|---|
| GET `/api/config` | 站点/品牌/积分/功能开关（免登录） | - |
| POST `/api/user/register` | 注册：**影子建号 + 注册赠送 + 自动登录** | 管理员 `POST /api/user/` + search 反查 uid；`POST /api/user/manage` add_quota |
| POST `/api/user/login` | 密码登录 → 换 PAT → 归还会话 → 签 Cookie | `POST /api/user/login` → `GET /api/user/token` → `DELETE /api/user/sessions/{sid}` |
| GET `/api/user/logout` | 清 Cookie | - |
| GET `/api/user/self` | 本人信息（积分余额/累计消耗/调用数/角色） | `GET /api/user/self` |

### 4.2 API Key（keys.py）—— 前端 BYOK 直连 new-api 的钥匙

| 方法/路径 | 说明 |
|---|---|
| GET `/api/token` | 我的 Key 列表（掩码） |
| POST `/api/token` | 建 Key（`{name}`），创建后自动取明文一次性返回 |
| POST `/api/token/{id}/key` | 查看明文 Key |
| DELETE `/api/token/{id}` | 删除 |

> Flovart 前端 Provider 适配层支持"自带 Key"形态：在线版把 new-api 网关
> `{base}/v1` 配成 Provider 地址、把这里创建的 Key 注入 `keyVault`，即完成
> "平台供 Key"改造，不动 Flovart 的 Provider 调用链。

### 4.3 用量（usage.py）

| 方法/路径 | 说明 |
|---|---|
| GET `/api/log/self?p=&page_size=` | 我的调用日志（单条明细积分保留小数） |
| GET `/api/log/self/stat` | 用量统计（总消耗积分/rpm/tpm） |

### 4.4 管理台（console.py，`require_admin`）—— 用户 / 渠道 / 模型

| 方法/路径 | 说明 | new-api 依赖（管理 API） |
|---|---|---|
| GET `/api/console/overview` | 总览：用户数/渠道数/可用模型数/注册赠送总数 | `/api/user/`、`/api/channel/` |
| GET `/api/console/users` | 用户列表/搜索（分页） | `GET /api/user/`、`GET /api/user/search` |
| POST `/api/console/users` | 直接建用户（不走注册赠送） | `POST /api/user/` |
| PUT `/api/console/users/{uid}` | 改用户名/密码/昵称 | `PUT /api/user/` |
| POST `/api/console/users/{uid}/quota` | 增/减积分 `{points, mode}` | `POST /api/user/manage` add_quota |
| DELETE `/api/console/users/{uid}` | 删用户 | `DELETE /api/user/{id}` |
| GET `/api/console/channels` | 渠道列表 | `GET /api/channel/` |
| POST `/api/console/channels` | 新增渠道（透传） | `POST /api/channel/` |
| PUT `/api/console/channels/{id}` | 更新渠道（透传） | `PUT /api/channel/` |
| DELETE `/api/console/channels/{id}` | 删除渠道 | `DELETE /api/channel/{id}` |
| POST `/api/console/channels/{id}/status` | 启用/停用 `{enabled}` | `POST /api/channel/{id}/status` `{status:1\|2}` |
| POST `/api/console/channels/{id}/test` | 渠道连通测试 | `GET /api/channel/test/{id}` |
| GET `/api/console/models` | 可用模型目录（启用渠道 models 合并去重） | `GET /api/channel/models_enabled` |

> ✅ **M1 已实测（2026-09-03，对接实例 newapi-bff.oneapis.cn）**：用户列表
> `/api/user/`（字段含 password 密文，BFF 白名单输出）、渠道列表 `/api/channel/`
> （**models 为逗号分隔字符串**、status 1/2、key 不下发）、启停
> `POST /api/channel/{id}/status`（body `{"status":1|2}`）、模型目录
> `GET /api/channel/models_enabled`（返回启用模型名数组）、全站日志
> `GET /api/log/` 均已核对。渠道**写操作**（启停/测试/建改删）涉及生产改动，
> 建议 M2 前端联调时人工点验一次。

### 4.5 探针

- GET `/healthz` —— 进程存活，永远 200。
- GET `/readyz` —— 语义就绪：SECRET_KEY 已配置（非弱值且 ≥32 字符）、
  管理员凭证已配置（PAT+UID 或账密）、数据目录可写；不通过返回 503。

## 5. 关键设计（均移植自 hewapi-bff，经过生产验证）

1. **会话 = AES-256-GCM 加密 Cookie**（`security.py` 原样移植）：载荷含用户
   PAT，必须加密而非仅签名；HKDF 派生密钥 + nonce 随机 + GCM tag 防篡改。
2. **PAT 代持 + 登录即归还会话**：new-api 会话上限 50 且硬拒绝不淘汰；BFF
   只在换 PAT 时登录一次，随后立刻 `DELETE /api/user/sessions/{sid}`。
3. **管理员凭证三通道**：`NEWAPI_ADMIN_PAT+UID`（推荐，不碰会话系统）>
   `data/admin_cred.json` 落盘缓存 > 账密 login 兜底；401 自动重登一次。
4. **管理员双头**：所有上游调用带 `Authorization: Bearer <PAT>` +
   `New-Api-User: <uid>`；**admin_request 与 user request 用同一套**
   （hewapi 实测两头的必要性）。
5. **注册赠送幂等**：`promo.py` 状态文件先占位后发放、失败回滚 —— new-api
   `add_quota` 无幂等键。
6. **注册=影子建号**（hewapi 兑换码建号同款）：用户名即 new-api 用户名，
   密码透传给上游建号接口；管理员角色判定 `role>=10` 或静态名单
   `BFF_ADMIN_USERNAMES`。
7. **积分口径**：所有出口 `quota→积分`（整数给余额/聚合，4 位小数给单条日志），
   避免"总额有值、每条都空"的换算失真。
8. **单 worker 部署哲学**：uvicorn 单进程 + JSON 状态文件即可；多副本须换
   共享存储/Redis（文档标注，M4 再说）。

## 6. Flovart 前端适配清单（M2，在 flovart-web fork 上做）

| # | 改动 | 落点 |
|---|---|---|
| 1 | Hosted 模式探测：启动时 `GET /api/config` 成功即进入在线模式 | `index.tsx`/`RouterHost.tsx` |
| 2 | 登录/注册门禁（在线模式下未登录先进登录页） | 新增 `components/hosted/LoginPage.tsx`；参照 `components/auth/AuthModal.tsx` |
| 3 | 余额/积分顶栏 + 用量入口 | `AppShell`/`SettingsPanel` 挂 `GET /api/user/self` + `/api/log/self/stat` |
| 4 | 平台供 Key：创建 Key 后写入 `keyVault`，Provider 地址指向 new-api `{base}/v1` | `SettingsPanel.tsx` + `services/aiGateway.ts` 改造 |
| 5 | Vite dev 代理 `/api` → `127.0.0.1:8300` | `vite.config.ts` |
| 6 | 管理台页面（渠道/用户/模型表格），复用 `components/enterprise` 的 antd Table 范式 | 新增 `components/enterprise/...` 或独立 hosted 路由 |
| 7 | 文案/品牌从 `/api/config` 拉取，不硬编码 | 全站 |
| 8 | 保留本地优先作为降级：无 BFF 时仍可 BYOK 纯本地跑 | Hosted 探测兜底 |

> 技术栈对照：Flovart 根应用 React19+antd6+zustand+react-router7(Hash)+Tailwind；
> hewapi 前端是手写原生 SPA —— 本项目不复用 hewapi 前端，只复用它的 BFF 设计。

## 7. 里程碑

| 阶段 | 内容 | 交付物 | 状态 |
|---|---|---|---|
| **M0** | BFF 工程骨架：配置/会话/客户端/存储/探针 + 认证用户域（注册影子建号、登录、self、赠送） | 本仓库代码 + 冒烟测试 | ✅ 本次完成 |
| **M1** | 管理台路由（用户/渠道/模型/总览）**+ 对真实 new-api 实测契约**并沉淀头部注释 | console.py 定稿 + 实测记录（用户 54/渠道 17/模型 43 的实例已核对） | ✅ 已实测（2026-09-03）；渠道写操作留 M2 前端联调人工点验 |
| **M2** | `flovart-web` fork 前端改造（见 §6），联调登录/供 Key/余额/创作链路 | 在线创作站可跑通"注册→登录→做图/视频" | ⏳ |
| **M3** | 生产化：Docker 多阶段构建 + compose（`${VAR:?}` 必填校验、非 root、只读根 FS、`/data` 卷、HEALTHCHECK、单 worker `--proxy-headers`）、Nginx 反代 + HTTPS | 可部署产物 | ⏳ |
| **M4** | 可选增强：作品云存储、动态站点配置（`settings.json`）、Redis 化多副本、Logfire 可观测、邀请/兑换码 | 按运营需求 | ⏳ |

## 8. 风险与既定对策

| 风险 | 对策 |
|---|---|
| new-api 渠道/模型管理契约未在本仓库实测 | M1 逐条实测并沉淀注释；代码已按"透传 + 归一"最小假设书写 |
| 管理员会话打满 50 导致 BFF 瘫痪 | PAT+UID 直供为主通道（不碰会话系统）+ 账密兜底（hewapi 已验证方案） |
| 注册并发重复赠送 / add_quota 无幂等 | promo.py 状态文件"先占位后发放、失败回滚" |
| Cookie 明文 HTTP 泄露 PAT | Secure=True 默认，仅本地开发显式关 |
| 前端接入误伤本地优先能力 | Hosted 探测 + 本地降级保留（改动点收敛在少量文件） |
| 多副本部署状态文件不共享 | 文档明示单 worker 哲学；上量后换 Redis（M4） |
