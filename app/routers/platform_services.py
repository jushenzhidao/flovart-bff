"""平台 AI 服务（全局共享配置）。

## 为什么需要它（飞哥 2026-09-11 拍板；2026-09-14 语义重塑）

用户诉求原文：「普通账号看不到管理员配置的服务……你可以把这个管理配置的 AI 服务
上传存储好，所有用户都可以拉取使用这个配置的」。

### ⭐ 2026-09-14 语义重塑（飞哥二次拍板，**当前唯一正确语义**）

原语义（选 B）：管理员把「完整服务条目（含 key）」下发，用户侧持 key 直连 →
**平台自担成本、不经 new-api 计费**。

新语义（**已替换旧语义**）：
- 平台服务 = **网关配置 + 模型清单**，**不含密钥**。
- 管理员在设置页手填「模型列表」（如 `gpt-image-2` / `doubao-seedream-4.0`）。
- 用户调用时用**自己的默认 Key**（`/api/me/ensure-key` 签发的 new-api sk-），
  **基址固定为 BFF 的 new-api 网关** → 计费自然落在**该用户自己的配额**。
- 用户「自己配网关+模型+Key」= 原有 BYOK，**完全不动**（两条链路互不干扰）。

为什么要这样改：
1. 旧语义把管理员 key 明文下发给所有人，泄漏面等同「任何登录用户都能拿到它」；
2. 旧语义**绕开 new-api 计费**，平台白付成本，用户用量无法计量；
3. 飞哥要的其实是「管理员决定**能选哪些模型**，用户用自己配额调用」——
   这正是「平台 Key 池 + 管理员指定模型白名单」的组合。

### 与「平台 Key 池」的关系（不再是两条独立链路，而是同一条的两段）
- 平台 Key 池（`extraConfig.flovart_platform='1'`）：注入**用户自己的** sk- + 网关基址。
- 平台服务（`extraConfig.platformSource='1'`）：**只提供模型白名单/展示名**，
  真正的 key 与 baseUrl **复用平台 Key 池那份**（用户自己的 sk-）。
- 两者都不带 `_gateway`（**绝不能带** —— 带了就变成 BFF 直连外部网关、绕开计费）。

## 存储与鉴权（未变）

- **存储**：复用 `cloud_docs` 表，用**保留 uid=0 + scope='platform'** 存全局配置
  （`doc_key='services'`）。不新增表 —— Pg/Local 两套后端自动生效，多副本共享同一 PG。
  uid=0 不可能是真实 new-api 用户（uid 从 1 起），且用户路由的 uid 全部来自
  加密会话 Cookie（服务端签发），**用户无法伪造成 0**，不存在越权读用户数据的问题。
- **读写权限**：
  - `GET  /api/platform/services` —— require_session（**所有登录用户可读**）
  - `PUT  /api/platform/services` —— require_admin（**仅管理员可写**）

## 数据形状（2026-09-14 起）

```jsonc
{
  "services": [
    {
      "id": "uuid",                    // 前端生成，用于去重/更新
      "name": "平台模型",               // 展示名（普通用户看到的芯片名）
      "capabilities": ["image"],       // 支持能力，决定 PromptBar 出现在哪个 tab
      "models": ["gpt-image-2", "..."],// ⭐ 管理员手填的模型清单（具体可选模型）
      "defaultModel": "gpt-image-2",   // 默认选中项
      "provider": "openai_compatible",
      "updatedBy": "admin",            // 审计：最后修改者用户名
      "updatedAt": 1789106662000
    }
  ],
  "revision": 3,
  "updated_at": "..."
}
```

⚠️ **不再包含 `key` / `baseUrl`**：密钥与基址都由 BFF 侧统一（用户自己的 sk- +
`config.API_BASE_URL`）。`_ALLOWED_FIELDS` 也已移除这两个字段 ——
即使前端误传，也不会落库、更不会下发。
"""
import logging

from fastapi import APIRouter, Body, Depends, HTTPException

from .. import cloudstore, config
from ..resp import ok
from ..security import require_admin, require_session

logger = logging.getLogger("bff.platform")

router = APIRouter()

# 保留维度：uid=0 不是合法 new-api 用户（用户 id 从 1 起），
# 且用户侧 uid 全部来自服务端签发的加密会话 —— 不可能被伪造成 0。
PLATFORM_UID = 0
PLATFORM_SCOPE = "platform"
PLATFORM_DOC_KEY = "services"

# 单条服务允许下发的字段白名单。
#
# ⚠️ 2026-09-14 起**移除 `key` 与 `baseUrl`**：平台服务不含密钥，
#    调用一律用「用户自己的默认 Key + BFF 网关基址」。
#    白名单是最后一道闸 —— 即使前端误传 key，也不会落库、不会下发。
_ALLOWED_FIELDS = (
    "id", "provider", "name", "capabilities",
    "models", "defaultModel", "customModels",
    "imageGenModel", "imageGenMode", "videoGenModel", "videoGenMode",
    "routeMappings", "extraConfig", "status", "websiteUrl",
)

_LIST_FIELDS = ("capabilities", "models", "customModels", "routeMappings")

# 兼容旧数据：这两个字段即便在库里存在，也不下发（见 _sanitize 的 pop）。
_FORBIDDEN_FIELDS = ("key", "baseUrl")


def _sanitize(service: dict, username: str) -> dict:
    """字段白名单 + 类型兜底。非法输入（非 dict）在调用处已拦。"""
    if not isinstance(service, dict):
        raise HTTPException(status_code=400, detail="服务条目必须是对象")
    out = {k: service[k] for k in _ALLOWED_FIELDS if k in service}
    for field in _LIST_FIELDS:
        v = out.get(field)
        out[field] = v if isinstance(v, list) else []
    # 模型清单：`models` 是主字段；`customModels` 为前端历史字段，保持同步，
    # 避免「管理员填了但 PromptBar 读不到」这类字段名不一致的坑。
    if not out["models"] and out["customModels"]:
        out["models"] = list(out["customModels"])
    if out["models"] and not out["customModels"]:
        out["customModels"] = list(out["models"])
    # 清掉空串项，避免前端渲染出空白选项。
    out["models"] = [str(m).strip() for m in out["models"] if str(m).strip()]
    out["customModels"] = [str(m).strip() for m in out["customModels"] if str(m).strip()]
    # extraConfig 必须是纯字符串字典：前端 keyVault 存字符串，混入对象会污染展示。
    extra = out.get("extraConfig")
    if not isinstance(extra, dict):
        extra = {}
    # ⚠️ flovart_platform 是**平台 Key 池**的标记（走 BFF 代发）。
    # 平台服务复用的是「用户自己的 sk- 走 new-api」，两者链路一致但标记必须分开：
    # 若带上该标记，前端会把「服务条目」误判成「Key 池条目」，注入逻辑串台。
    extra.pop("flovart_platform", None)
    # 防御：_gateway 是「BFF 直连外部网关」的开关，绝不能从服务端配置里带出来
    # —— 那会让所有用户绕开 new-api 计费打外部地址。
    extra.pop("_gateway", None)
    out["extraConfig"] = {str(k): str(v) for k, v in extra.items()}
    for f in _FORBIDDEN_FIELDS:
        out.pop(f, None)
    out["name"] = str(out.get("name") or "平台模型")
    out["defaultModel"] = str(out.get("defaultModel") or "")
    out["updatedBy"] = username
    return out


def _redact(service: dict) -> dict:
    """下发给前端的版本。

    2026-09-14 起**不再下发 key**：平台服务不含密钥，用户用自己的默认 Key 调用。
    仍显式剔除 `key`/`baseUrl`（兼容库里可能残留的旧数据）。
    """
    out = dict(service)
    for f in _FORBIDDEN_FIELDS:
        out.pop(f, None)
    # 模型清单归一（兼容旧数据只有 customModels 的情况）。
    if not out.get("models") and out.get("customModels"):
        out["models"] = list(out["customModels"])
    return out


async def _read_raw() -> dict:
    doc = await cloudstore.doc_get(PLATFORM_UID, PLATFORM_SCOPE, PLATFORM_DOC_KEY)
    if not doc:
        return {"services": [], "revision": 0, "updated_at": None}
    payload = doc.get("payload") or {}
    services = payload.get("services")
    return {
        "services": services if isinstance(services, list) else [],
        "revision": doc.get("revision", 0),
        "updated_at": doc.get("updated_at"),
    }


@router.get("/api/platform/services")
async def list_platform_services(_s: dict = Depends(require_session)):
    """所有登录用户可读：平台共享的**模型清单**（不含密钥）。

    返回的条目用于「PromptBar 里列出管理员指定的可选模型」；
    真正调用时前端用**用户自己的默认 Key** + `gatewayBaseUrl`（本响应的另一个字段）
    走 new-api，计费落该用户配额。
    """
    state = await _read_raw()
    return ok({
        "items": [_redact(s) for s in state["services"]],
        "revision": state["revision"],
        "updated_at": state["updated_at"],
        # ⭐ 网关基址由 BFF 统一给出（= new-api 的 /v1）。前端不得自行拼接，
        #    这样「用户默认 Key + 网关基址」必定同源，不会出现跨网关 401。
        "gatewayBaseUrl": config.API_BASE_URL,
    })


@router.put("/api/platform/services")
async def put_platform_services(
    body: dict = Body(...),
    session: dict = Depends(require_admin),
):
    """仅管理员：整体覆盖平台服务列表（前端「发布到平台」按钮调用）。

    整体覆盖（而非逐条增删）的理由：管理员在前端就是编辑一个列表，
    逐条 API 会引入 id 生命周期/并发增删的一致性问题；这里对齐
    cloud_docs 的 last-write-wins 语义，配合 revision 乐观锁即可。
    """
    raw = body.get("services")
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="services 必须为数组")
    if len(raw) > 20:
        raise HTTPException(status_code=400, detail="平台服务最多 20 条")

    username = str(session.get("username") or "admin")
    services = [_sanitize(s, username) for s in raw]

    # 乐观锁：管理员可能在两个标签页各改一次，避免后写静默覆盖先写。
    base_revision = body.get("base_revision")
    current = await _read_raw()
    if base_revision is not None and int(base_revision) != int(current["revision"]):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=409,
            content={"success": False, "message": "平台服务已被其他会话更新",
                     "data": {"current_revision": current["revision"]}},
        )

    result = await cloudstore.doc_put(
        PLATFORM_UID, PLATFORM_SCOPE, PLATFORM_DOC_KEY, {"services": services})
    logger.info("平台服务已更新 by=%s count=%d revision=%s",
                username, len(services), result.get("revision"))
    return ok({"count": len(services), **result}, "已发布到平台")
