"""平台 AI 服务（全局共享配置）。

## 为什么需要它（飞哥 2026-09-11 拍板）

用户诉求原文：「普通账号看不到管理员配置的服务……你可以把这个管理配置的 AI 服务
上传存储好，所有用户都可以拉取使用这个配置的」。

现状缺口：管理员在设置页配的 AI 服务（Provider/Base URL/Key/模型/能力/映射）
只存在**该管理员浏览器**的 keyVault 里，其他账号永远看不到。平台侧只有
「平台 Key 池」（`/api/me/ensure-key`，走 new-api 渠道），**没有「服务条目」这一层**
—— 于是管理员配的自定义端点（如 `api.chatfire.cn` + `gpt-image-2`）无法共享。

## 设计（选 B：连密钥一起共享，但用户侧只读）

- **存储**：复用 `cloud_docs` 表，用**保留 uid=0 + scope='platform'** 存全局配置
  （`doc_key='services'`）。不新增表 —— Pg/Local 两套后端自动生效，多副本共享同一 PG。
  uid=0 不可能是真实 new-api 用户（uid 从 1 起），且用户路由的 uid 全部来自
  加密会话 Cookie（服务端签发），**用户无法伪造成 0**，不存在越权读用户数据的问题。
- **读写权限**（这是「隔离但不干扰」的关键）：
  - `GET  /api/platform/services` —— require_session（**所有登录用户可读**）
  - `PUT  /api/platform/services` —— require_admin（**仅管理员可写**）
  - 普通用户拿到的是「可用但不可改」的服务定义。
- **密钥处理**：管理员提交的 `key`（sk-...）加密落盘（AES-256-GCM，复用
  security.encrypt_secret），读取时解密后随配置一起下发给前端 —— 因为选 B 就是要
  让别人的浏览器也能用这把 key 调用。**风险已知并接受**：这是平台自有 key，
  泄漏面等同「任何登录用户都能拿到它」，需配合网关侧额度/白名单控制。
  管理员如需更安全，应改用「平台 Key 池」模式（不下发密钥，走 BFF 代发）。

## 数据形状

```jsonc
{
  "services": [
    {
      "id": "uuid",                  // 前端生成，用于去重/更新
      "provider": "openai_compatible",
      "name": "平台模型",             // 展示名（普通用户看到的就是这个）
      "baseUrl": "https://api.chatfire.cn/v1",
      "key": "sk-...",               // 加密存储，读时解密
      "capabilities": ["image"],
      "customModels": ["gpt-image-2"],
      "defaultModel": "gpt-image-2",
      "imageGenModel": "gpt-image-2",
      "imageGenMode": "async",
      "videoGenModel": "",
      "videoGenMode": "async",
      "routeMappings": [...],
      "extraConfig": {...},          // 不含 flovart_platform（由前端注入时打标）
      "updatedBy": "admin",          // 审计：最后修改者用户名
      "updatedAt": 1789106662000
    }
  ],
  "revision": 3,
  "updated_at": "..."
}
```
"""
import logging

from fastapi import APIRouter, Body, Depends, HTTPException

from .. import cloudstore
from ..resp import ok
from ..security import require_admin, require_session

logger = logging.getLogger("bff.platform")

router = APIRouter()

# 保留维度：uid=0 不是合法 new-api 用户（用户 id 从 1 起），
# 且用户侧 uid 全部来自服务端签发的加密会话 —— 不可能被伪造成 0。
PLATFORM_UID = 0
PLATFORM_SCOPE = "platform"
PLATFORM_DOC_KEY = "services"

# 单条服务允许下发的字段白名单。**不要**把整个 body 原样存/发 ——
# 前端 keyVault 里的 UserApiKey 含 id/createdAt 等本地字段，且未来可能加
# 内部标记；白名单能保证共享出去的东西是「可预期的一小组」。
_ALLOWED_FIELDS = (
    "id", "provider", "name", "baseUrl", "key", "capabilities",
    "customModels", "defaultModel", "imageGenModel", "imageGenMode",
    "videoGenModel", "videoGenMode", "routeMappings", "extraConfig",
    "status", "websiteUrl",
)

_LIST_FIELDS = ("capabilities", "customModels", "routeMappings")


def _sanitize(service: dict, username: str) -> dict:
    """字段白名单 + 类型兜底。非法输入（非 dict / 空 key）在调用处已拦。"""
    if not isinstance(service, dict):
        raise HTTPException(status_code=400, detail="服务条目必须是对象")
    out = {k: service[k] for k in _ALLOWED_FIELDS if k in service}
    for field in _LIST_FIELDS:
        v = out.get(field)
        out[field] = v if isinstance(v, list) else []
    # extraConfig 必须是纯字符串字典：前端 keyVault 存字符串，混入对象会污染展示。
    extra = out.get("extraConfig")
    if not isinstance(extra, dict):
        extra = {}
    # ⚠️ flovart_platform 是**平台 Key 池**的标记（走 BFF 代发）。
    # 共享服务是「用户侧持 key 直连」，与平台 Key 池是两条不同链路：
    # 若带上该标记，前端 aiGateway.isHostedPlatform() 会误判为平台代发，
    # 反而不用这把 key、也绕开 new-api 计费。故此处强制剔除。
    extra.pop("flovart_platform", None)
    out["extraConfig"] = {str(k): str(v) for k, v in extra.items()}
    out["key"] = str(out.get("key") or "")
    out["baseUrl"] = str(out.get("baseUrl") or "")
    out["updatedBy"] = username
    return out


def _redact(service: dict) -> dict:
    """下发给前端的版本：保留 key（选 B 语义，用户要能直接调用），
    但补一个 `keyPresent` 布尔，前端无需自行判断空串。"""
    out = dict(service)
    if not out.get("key"):
        out["keyPresent"] = False
        out.pop("key", None)
    else:
        out["keyPresent"] = True
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
    """所有登录用户可读：平台共享的 AI 服务列表（含密钥，见模块头说明）。

    普通用户拿到的条目用于「注入自己的 keyVault + 正常调用」，但**不提供
    编辑入口** —— 前端 SettingsPanel 对 isPlatformSource 的条目只读展示。
    """
    state = await _read_raw()
    return ok({
        "items": [_redact(s) for s in state["services"]],
        "revision": state["revision"],
        "updated_at": state["updated_at"],
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
