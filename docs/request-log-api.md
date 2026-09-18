# 用户调用日志查询接口（管理员）

> 落库范围：image / video / chat 三类用户调用的请求与返回，写入 `cloud_request_log` 表（Pg / Local SQLite 双后端）。
> 本文档对应 2026-09-18 的改动：管理员查询接口 + chat 全程落库 + 409 拦截留痕 + b64 剥离。

## 鉴权

- 管理员接口：`is_admin()` 通过才能调（上游 new-api `role >= 10` **或** `BFF_ADMIN_USERNAMES` 名单）。未登录 401，非管理员 403。
- 用户自查接口：任意登录用户，只能看到自己的记录。

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/console/requests` | 管理员：全站用户调用日志列表，支持筛选 |
| GET | `/api/console/requests/{request_id}` | 管理员：单条详情（完整 payload + result） |
| GET | `/api/me/requests?limit=&offset=` | 用户：查自己的日志列表（最新优先） |
| GET | `/api/me/requests/{request_id}` | 用户：查自己某条详情 |

---

## 1. GET /api/console/requests（管理员 · 列表）

### 查询参数（可任意组合）

| 参数 | 类型 | 说明 |
|---|---|---|
| `username` | string | 按用户名**精确**筛选；BFF 自动去 new-api 解析成 uid，解析不到返回 404 |
| `uid` | int | 按用户 id 筛选（与 username 二选一，都传以 uid 为准） |
| `kind` | string | 调用类型：`image` / `video` / `chat` |
| `status` | string | `submitted` / `succeeded` / `failed` / `cancelled` / `processing` |
| `model` | string | 模型名**子串**匹配，如 `model=gemini` 捞出所有 gemini 调用 |
| `limit` | int | 每页条数，默认 50，最大 200 |
| `offset` | int | 翻页偏移 |

### 响应

```json
{
  "success": true,
  "data": {
    "total": 123,
    "items": [
      {
        "request_id": "b1c2...",
        "uid": 7,
        "username": "fly",
        "kind": "image",
        "provider": "gw",
        "model": "gemini-3-pro-image-preview",
        "status": "failed",
        "task_id": "t-xxx",
        "gateway_request_id": "gw-xxx",
        "mode": "sync",
        "created_at": "2026-09-18T10:00:00",
        "updated_at": "2026-09-18T10:00:05"
      }
    ]
  }
}
```

说明：

- 列表是**摘要行**，不含 payload/result 大字段，要看详情走接口 2。
- `username` 由 BFF 批量向 new-api 反查回填；上游限流时降级为空串，**不会 500**。
- `status=failed` + `mode=blocked` = 被平台下架闸门拦截的请求（result 里有 `code=MODEL_SUSPENDED` 与原因）。

---

## 2. GET /api/console/requests/{request_id}（管理员 · 详情）

### 响应

```json
{
  "success": true,
  "data": {
    "request_id": "b1c2...",
    "uid": 7,
    "username": "fly",
    "kind": "chat",
    "model": "gpt-4.1-mini",
    "status": "succeeded",
    "payload": { "...": "提交时的请求参数" },
    "result": { "reply": "模型回复文本", "finish_reason": "stop", "usage": {} },
    "created_at": "...",
    "updated_at": "..."
  }
}
```

说明：

- **b64 剥离**：payload/result 中超长的 `b64_json` / `b64` / `image_base64` / `base64` 字段及 data-uri 图片会被替换为 `<base64:N chars>` 占位符，避免几 MB 图片撑爆响应。要看图走任务产物链接，不走日志。
- `request_id` 不存在返回 404。

---

## 3. GET /api/me/requests（用户 · 查自己的）

只带 `limit`（默认 20）/ `offset`，其余字段固定为当前登录用户，返回结构与接口 1 相同。

---

## 快捷用法

### 浏览器 F12（管理员登录测试站后，控制台直接跑）

```js
// 最近 50 条全站调用
fetch('/api/console/requests?limit=50').then(r => r.json()).then(console.log)

// 按用户名查
fetch('/api/console/requests?username=fly&limit=50').then(r => r.json()).then(console.log)

// 组合：某用户的 gemini 失败调用
fetch('/api/console/requests?username=fly&model=gemini&status=failed').then(r => r.json()).then(console.log)

// 单条详情
fetch('/api/console/requests/<request_id>').then(r => r.json()).then(console.log)
```

### curl

```bash
curl -c c.txt -X POST https://flovart.oneapis.cn/api/user/login \
  -H 'Content-Type: application/json' -d '{"username":"管理员","password":"***"}'
curl -b c.txt 'https://flovart.oneapis.cn/api/console/requests?limit=20'
```

### 完全不登录的兜底（服务器上直接读容器 SQLite）

```bash
docker exec flovart-bff python -c "
import sqlite3
conn = sqlite3.connect('/data/flovart_cloud.db'); conn.row_factory = sqlite3.Row
for r in conn.execute('SELECT request_id,uid,kind,model,status,substr(created_at,1,19) t FROM cloud_request_log ORDER BY created_at DESC LIMIT 20'):
    print(dict(r))
"
```

---

## 落库时机对照（哪一步能查到什么）

| 时机 | kind | status | result 内容 |
|---|---|---|---|
| 生图/视频任务提交成功 | image/video | `submitted` | — |
| 任务轮询到终态 | image/video | `succeeded` / `failed` / `cancelled` | 产物信息或错误 |
| 模型被下架闸门拦截（409） | image/video | `failed`（`mode=blocked`） | `code=MODEL_SUSPENDED` + 原因 |
| chat 提交 | chat | `submitted` | — |
| chat 流正常结束 | chat | `succeeded` | `reply`（截尾 8000 字）+ `finish_reason` + `usage` |
| chat 上游报错 / 发放用户 Key 失败 | chat | `failed` | `error` 信息 |
| chat 用户中途关页面 | chat | `cancelled` | — |

## 字段含义

| 字段 | 含义 |
|---|---|
| `request_id` | 本条日志主键；image/video 场景同时是任务 id |
| `kind` | 调用类型：image / video / chat |
| `mode` | `sync` / `async` / `stream` / `blocked` |
| `provider` | 网关标识（`gw` = new-api 网关；`byok` = 用户自带 Key 直连，BYOK 不落库） |
| `gateway_request_id` | 上游网关任务 id，用于对账 |
| `upstream_empty_result` | result 里的标记：上游 200 但没返回图（常见于渠道模型配置名不对） |
| `error.detail` | **底层真实原因**（httpx 异常类型+原文 / 上游非 JSON 响应预览）；`message` 是给用户的通用文案，`detail` 才是排障用的 |

## 已知边界（截至 2026-09-18）

- BYOK（请求带 `_gateway`，用户自带 Key 直连）**不经过平台闸门、不落库**。
- 30 天保留清理策略**未实现**（量大后再做）。
- 生效前提：本地改动需构建 BFF 新镜像部署到目标环境；`GET /api/console/requests` 404 说明该环境还是旧镜像。
- **启动清扫**：进程重启会丢在途后台同步任务 → 相关行永远停在 submitted。BFF 启动时自动把超过 6 小时仍 `submitted` 的行标为 `failed`，result.error.type=`stale_submitted`（提示「任务因服务重启丢失，请重试」）。
