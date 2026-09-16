# deployment（部署 / 容器化 / 与 hewapi 的同机隔离 · 2026-09-16 建立）

> MEMORY.md 只留摘要，本文件是完整清单与踩坑记录。

## 一、两套 BFF 的隔离契约（flovart-bff vs hewapi-bff）

**前提**：两者**部署在同一台机器**上（飞哥 2026-09-16 确认），且同源
（`D:/code/hewapi-bff/newapi-bff` 是参考实现）。凡有固定名的资源必须改名。

| 资源 | hewapi 线上 | flovart-bff | 不改的后果 |
|---|---|---|---|
| compose project | 目录名推导 | `name: flovart-bff`（显式） | 卷/网络名撞车 |
| `container_name` | `newapi-bff` | `flovart-bff` | **同名冲突，第二个容器起不来** |
| `image` | `newapi-bff:${APP_VERSION}` | `flovart-bff:${APP_VERSION}` | 镜像互相覆盖 |
| volume | `bff-data` | `flovart-bff-data`（带绝对 `name:`） | 数据串味（PAT / 用户密钥混） |
| 宿主端口 | `${BFF_PORT:-8000}` | `${FLOVART_BFF_PORT:-8300}` | 端口占用 |
| Logfire token | `pylf_v1_us_2tpyfKl…` | **必须换独立项目** | trace 混成一坨 |
| Logfire environment | `local`（线上也是，属疏忽） | `flovart-prod` | 同上 |
| Logfire service_name | `newapi-bff`（**代码硬编码**） | `config.SERVICE_NAME`（可覆盖） | 同上 |

**目录层级的影响已被消除**：compose 顶层写了 `name: flovart-bff`，卷写了
`name: flovart-bff-data`（绝对名，不带 project 前缀）→ 项目放哪个目录、几层深，
项目名与卷名都不变。这样将来从「不同机器」合并到「同机」时零改动，也不会
因为改目录名导致「数据卷凭空消失」。

**数据面天然不撞**：hewapi **完全不用 PG / OSS**（纯 JSON 文件 + `/data` 卷，
`grep -cE "POSTGRES|OSS_ENABLED" app/config.py` = 0）。flovart 用 PG/OSS 时，
只要 database 名（`POSTGRES_DB=flovart`）与对象前缀（`OSS_PREFIX=flovart`）保持
专属即可。

**同机还需注意**：Nginx 反代要分别指向 8000（hewapi）与 8300（flovart）；
new-api 管理员账号必须各自独立（PAT 是账号级的，共用会互踢 401）。

## 二、日志落地方式（2026-09-16 已接入 Logfire）

**刻意不写文件日志**，两条腿：

1. **stdout** → compose `logging: json-file, max-size 10m, max-file 5`
   → 宿主机 `/var/lib/docker/containers/<id>/<id>-json.log`
   → 查看 `docker compose logs -f --tail=200 bff`
2. **Logfire** → `app/observability.py`（116 行真实现）

### 为什么不能用 FileHandler
compose 里 `read_only: true`（根文件系统只读）。往容器内写日志文件直接抛
`OSError: [Errno 30] Read-only file system`。要写就写 `/data` 卷 —— 但设计上
选择不写，交给 docker 轮转 + Logfire。

### observability.py 的降级契约（改动时别破坏）
- 未配 `LOGFIRE_TOKEN` → **不 import SDK**、`setup()` 返回 `False`、零副作用
- 任何一步失败只 `logger.warning`，绝不抛 —— 可观测性不能拖垮业务启动
- `_scrub` 回调：字段路径命中 authorization/cookie/token/pat/password/secret/
  session/access_key/api_key/key/api-key/apikey/x-api- → `[scrubbed]`
  （`key` 会连带命中 `mediaKey` 这类非敏感键，是刻意接受的过杀）
- 幂等（`_configured`）：重复 setup 不抛 `already instrumented`
- `console=False`：stdout 已有 logging，避免重复打印淹没真实日志
- `service_name` **从 `config.SERVICE_NAME` 读，绝不写死**（hewapi 写死
  `"newapi-bff"` 就是这个坑的来源）

## 三、容器化踩坑（`read_only: true` 相关）

### ⚠️ 最坑的一条：这两个路径**不跟随** `BFF_DATA_DIR`
`app/config.py` 里：
- `ADMIN_CRED_FILE` 默认 = `仓库根/data/admin_cred.json`（硬编码，非 DATA_DIR）
- `SIGNUP_STATE_FILE` 默认 = `仓库根/data/signup_bonus.json`（同上）

容器里就是 `/app/data/…`，而 `/app` 只读 → 写管理员 PAT 缓存或首次注册赠送时
**直接抛 Errno 30**。故 Dockerfile 与 compose **都必须显式覆盖**：
```
BFF_ADMIN_CRED_FILE=/data/admin_cred.json
BFF_SIGNUP_STATE_FILE=/data/signup_bonus.json
```

### 其余写盘点（审计结论：全部落在 /data，read_only 安全）
- `cloudstore.py:350` `makedirs(DATA_DIR)`
- `cloudstore.py:710/716` `DATA_DIR/media/<uid>/<xx>/`（本地 blob 回落）
- `newapi_client.py:131/133` `dirname(ADMIN_CRED_FILE)` → 靠上面的显式覆盖
- `store.py:22/39/41/53` `DATA_DIR` 及其父目录
- `user_keys.py:34/36` `DATA_DIR/user_keys.json`（硬编码跟随 DATA_DIR，**没有独立 env**）
- `store.py:53` 的 `makedirs(dirname(path) or ".")`：`.` 已存在时 `exist_ok=True`
  不会尝试创建，故不报错 —— 但前提是 path 都带目录（flovart 的调用点确实都带）

### 其余取值
- `/tmp` tmpfs 给 **64m**（hewapi 只给 16m）：FastAPI `UploadFile` 超过 1MB 会
  spool 到 `/tmp`，本项目的 PSD / 多图层上传容易越线。tmpfs **计入 memory 限额**。
- memory limit **1G**（hewapi 512M）：有 Pillow / psd-tools 图像转码路径，
  大图解码是内存峰值来源，512M 下大 PSD 易被 OOM Kill。
- 不装 curl：健康检查用 `python -c urllib.request`，避免构建依赖 apt 源。
- 只 `COPY app/`：**没有** static/（前端独立仓库）、**没有** docs/ 运行时依赖。
  ⚠️ 别照抄 hewapi 的 `COPY static/ ./static/` 与 `COPY docs/ ./docs/`。

## 四、文件清单（2026-09-16 新增/改动）

新增：
- `Dockerfile`（多阶段、非 root、read_only 兼容、单 worker）
- `docker-compose.yml`（全套改名隔离 + Logfire 独立项目说明）
- `.dockerignore`（`.env` / `data/` / 缓存 / 文档 / 测试 / 脚本）

改动：
- `app/observability.py`：no-op 桩 → 真实现
- `app/config.py`：新增 `SERVICE_NAME`（读 `BFF_SERVICE_NAME`，默认 `flovart-bff`）
- `app/main.py`：`/healthz` 的服务名从硬编码改为 `config.SERVICE_NAME`
- `requirements.txt`：新增 `logfire[fastapi,httpx]==4.41.0`
- `.env.example`：补 Logfire 独立项目说明 + `BFF_SERVICE_NAME` + 容器部署段；
  修正过时注释（`GATEWAY_IMAGE_GEN_MODE` 早就默认 sync，文件里还写着 async）

## 五、验证记录（2026-09-16）
- `python -m compileall app` → 通过
- `pytest tests/` → **44 passed**
- 无 token：`setup()` 返回 `False`、`instrument_httpx()` 零副作用
- 假 token：`setup()` 返回 `True`，日志 `service=flovart-bff environment=flovart-prod
  fastapi_traces=on`；二次 setup 幂等；`instrument_httpx(object())` 只 warning 不抛
- `docker compose config` → 语法通过，`name/container_name/image/volume/published`
  全部为 flovart-bff 专属值
- **模拟容器环境**（只拷 `app/` 到干净目录启动）→ `/healthz` 200、
  `/readyz` 三项全 ok（secret_key/admin_cred/state_dir_writable）、`/` 200
  → **证实只拷 `app/` 就够**

## 六、待办
- 飞哥：去 Logfire **新建项目**取新 token，并改 `.env`：
  `LOGFIRE_TOKEN=<新>`、`LOGFIRE_ENVIRONMENT=flovart-prod`（现为 `local`）
- 服务器上 hewapi 的部署目录名、以及 flovart 打算放的目录（显式 name 后已不影响
  卷名，但 Nginx 反代与运维习惯仍需知道）
- Docker daemon 本机未运行，镜像 build 与真容器 run **尚未实测**（配置层已全验）
