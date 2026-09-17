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

## 上下架（飞哥 2026-09-16 追加）

**问题**：管理员删掉/下架一条服务后，用户那边照样能看到、照样能调。
原因不是前端「没刷新」这么简单 —— 它是「显示」与「准入」两件事：

- **显示**：用户在本地 keyVault 里有一份**持久化影子条目**（`platformSource='1'`），
  只在登录/冷启动对齐一次 → 页面开着就一直在。
- **准入**：这个模型只要在上游网关渠道里存在，用户拿着自己的平台 Key
  照样调得通。所以光从列表里删掉**根本拦不住使用**。

⇒ 本文件的 PUT 除了存 `services`，还会维护一份**撤回集 `revoked`**（曾经发布过、
现在没有了的模型名），由 `app/platform_catalog.py` 的闸门在任务提交/聊天/模型目录
三处统一拦截。判定规则：`(历史撤回 ∪ 上次已发布) − 本次已发布` ——
删除自动记录，重新发布自动解除。详见 `app/platform_catalog.py` 模块头。

`GET` **会下发下架条目**（带 `suspended: true`，凭据已剥除）：管理员需要看到
并把它们「重新上架」，而不必再开一个管理员专属接口；用户侧由前端过滤，不参与选型。

## 数据形状

```jsonc
{
  "services": [
    {
      "id": "uuid",                  // 前端生成，用于去重/更新
      "provider": "openai_compatible",
      "name": "平台模型",             // 展示名（普通用户看到的就是这个）
      "models": ["gpt-image-2"],     // ⭐ 模型清单（PromptBar 读这个）
      "baseUrl": "https://api.chatfire.cn/v1",
      "key": "sk-...",               // 加密存储，读时解密
      "capabilities": ["image"],
      "customModels": ["gpt-image-2"],  // 与 models 双向同步（兼容旧前端读取路径）
      "defaultModel": "gpt-image-2",
      "imageGenModel": "gpt-image-2",
      "imageGenMode": "async",
      "videoGenModel": "",
      "videoGenMode": "async",
      "routeMappings": [...],
      "extraConfig": {...},          // 不含 flovart_platform（由前端注入时打标）
      "suspended": false,            // ⭐ 下架标记：true = 用户侧不注入 + 调用被闸门拒绝
      "suspendedModels": [],         // ⭐ 模型级下架（2026-09-17）：上架条目里被单独下架的模型名
      "updatedBy": "admin",          // 审计：最后修改者用户名
      "updatedAt": 1789106662000
    }
  ],
  "revoked": {"模型名": 1789541248.5},  // 撤回集：曾发布过、现已删除/下架的模型
  "revision": 3,
  "updated_at": "..."
}
```
"""
import logging

from fastapi import APIRouter, Body, Depends, HTTPException

from .. import cloudstore, config
from .. import platform_catalog as catalog
from ..resp import ok
from ..security import require_admin, require_session

logger = logging.getLogger("bff.platform")

router = APIRouter()

# 保留维度与文档键统一来自 platform_catalog（单一事实源，避免两处各写一份漂移）。
PLATFORM_UID = catalog.PLATFORM_UID
PLATFORM_SCOPE = catalog.PLATFORM_SCOPE
PLATFORM_DOC_KEY = catalog.PLATFORM_DOC_KEY

# 单条服务允许下发的字段白名单。**不要**把整个 body 原样存/发 ——
# 前端 keyVault 里的 UserApiKey 含 id/createdAt 等本地字段，且未来可能加
# 内部标记；白名单能保证共享出去的东西是「可预期的一小组」。
_ALLOWED_FIELDS = (
    "id", "provider", "name", "baseUrl", "key", "capabilities",
    "models", "customModels", "defaultModel", "imageGenModel", "imageGenMode",
    "videoGenModel", "videoGenMode", "routeMappings", "extraConfig",
    "status", "websiteUrl", "suspended", "suspendedModels", "updatedAt",
)

_LIST_FIELDS = ("capabilities", "models", "customModels", "routeMappings")
# ⚠️ suspendedModels 不进 _LIST_FIELDS：它有专属兜底（非列表=剔除键，而非强转空表），
#    见 _sanitize 尾部 —— 否则字符串输入会被先转成 []，掩盖调用方的脏数据。


def _sanitize(service: dict, username: str) -> dict:
    """字段白名单 + 类型兜底。非法输入（非 dict / 空 key）在调用处已拦。"""
    if not isinstance(service, dict):
        raise HTTPException(status_code=400, detail="服务条目必须是对象")
    out = {k: service[k] for k in _ALLOWED_FIELDS if k in service}
    for field in _LIST_FIELDS:
        v = out.get(field)
        out[field] = v if isinstance(v, list) else []
    # ⭐ models 与 customModels 必须**双向同步**：
    #   前端 PromptBar 历史上读 customModels，新代码读 models；
    #   只存一边会让另一条读取路径拿到空清单（表现：「发布成功但用户选不到模型」）。
    if not out["models"] and out["customModels"]:
        out["models"] = list(out["customModels"])
    if not out["customModels"] and out["models"]:
        out["customModels"] = list(out["models"])
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
    # 下架标记归一化：前端可能传 true / 'true' / 1，统一成 bool（缺省 = 上架）。
    out["suspended"] = bool(out.get("suspended"))
    # ⭐ 模型级下架清单（2026-09-17 飞哥：下架要解耦到具体模型）：
    #   只保留非空字符串并去重保序；闸门侧用 platform_catalog.service_suspended_set 归一。
    sm = out.get("suspendedModels")
    if isinstance(sm, list):
        seen: list[str] = []
        for m in sm:
            name = str(m or "").strip()
            if name and name not in seen:
                seen.append(name)
        out["suspendedModels"] = seen
    else:
        out.pop("suspendedModels", None)
    out["updatedBy"] = username
    return out


def _redact(service: dict) -> dict:
    """下发给前端的版本：保留 key（选 B 语义，用户要能直接调用），
    但补一个 `keyPresent` 布尔，前端无需自行判断空串。

    ⚠️ **下架条目一律剥掉 key/baseUrl**（2026-09-16）：下架 = 用户不可用，
    没有任何理由继续下发凭据；也堵死「旧前端拿残留 key 绕过下架」这条路。
    """
    out = dict(service)
    if out.get("suspended"):
        out.pop("key", None)
        out.pop("baseUrl", None)
        out["keyPresent"] = False
        return out
    if not out.get("key"):
        out["keyPresent"] = False
        out.pop("key", None)
    else:
        out["keyPresent"] = True
    return out


async def _read_raw() -> dict:
    """读原始文档。**不走 catalog 的 TTL 缓存** —— PUT 的乐观锁要比对最新 revision。"""
    doc = await cloudstore.doc_get(PLATFORM_UID, PLATFORM_SCOPE, PLATFORM_DOC_KEY)
    if not doc:
        return {"services": [], "revoked": {}, "revision": 0, "updated_at": None}
    payload = doc.get("payload") or {}
    services = payload.get("services")
    revoked = payload.get("revoked")
    return {
        "services": services if isinstance(services, list) else [],
        "revoked": revoked if isinstance(revoked, dict) else {},
        "revision": doc.get("revision", 0),
        "updated_at": doc.get("updated_at"),
    }


@router.get("/api/platform/services")
async def list_platform_services(_s: dict = Depends(require_session)):
    """所有登录用户可读：平台共享的 AI 服务列表（见模块头说明）。

    普通用户拿到的条目用于「注入自己的 keyVault + 正常调用」，但**不提供
    编辑入口** —— 前端 SettingsPanel 对 isPlatformSource 的条目只读展示。

    ⭐ 2026-09-16：**下架条目也下发**（`suspended: true`，凭据已剥除）。
    管理员需要看到它们才能「重新上架」，而那不需要再开一个管理员专属接口；
    用户侧由前端过滤，不进入模型选择器。
    """
    state = await _read_raw()
    return ok({
        "items": [_redact(s) for s in state["services"]],
        "revision": state["revision"],
        "updated_at": state["updated_at"],
        # 网关基址（= new-api 的 /v1）：用户用自己的 sk- 打这里。
        # 前端 `platformGatewayBaseUrl` 优先读它，缺失时回落站点 config。
        "gatewayBaseUrl": config.API_BASE_URL,
    })


@router.put("/api/platform/services")
async def put_platform_services(
    body: dict = Body(...),
    session: dict = Depends(require_admin),
):
    """仅管理员：整体覆盖平台服务列表（前端「发布到平台 / 下架 / 删除」调用）。

    整体覆盖（而非逐条增删）的理由：管理员在前端就是编辑一个列表，
    逐条 API 会引入 id 生命周期/并发增删的一致性问题；这里对齐
    cloud_docs 的 last-write-wins 语义，配合 revision 乐观锁即可。

    ⭐ 2026-09-16：整体覆盖还带来一个额外好处 —— **撤回集可以纯靠 diff 算出来**。
    上一次「已发布模型集」减去这一次的，就是被删掉/被下架的模型；
    重新发布的会自动从撤回集移出（自愈）。管理员点「删除」时不需要任何
    额外接口，服务端自然知道「它刚才供给过哪些模型」。
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

    # ⭐ 撤回集：diff 出「曾经发布、这次没了」的模型，交给闸门拦截。
    revoked_old = catalog.prune_revoked(current["revoked"])
    prev_published = catalog.published_models(current["services"])
    next_published = catalog.published_models(services)
    revoked_new = catalog.merge_revoked(
        prev_published=prev_published,
        next_published=next_published,
        revoked_old=revoked_old,
    )
    newly_revoked = sorted(set(revoked_new) - set(revoked_old))

    result = await cloudstore.doc_put(
        PLATFORM_UID, PLATFORM_SCOPE, PLATFORM_DOC_KEY,
        {"services": services, "revoked": revoked_new})
    # 写入后立刻失效缓存：下架必须**马上**生效，不能等 TTL 过去。
    catalog.invalidate()
    if newly_revoked:
        logger.warning("平台服务下架/删除，新增撤回模型 by=%s models=%s",
                       username, newly_revoked)
    logger.info("平台服务已更新 by=%s count=%d published=%d revoked=%d revision=%s",
                username, len(services), len(next_published), len(revoked_new),
                result.get("revision"))
    return ok({
        "count": len(services),
        "published_models": sorted(next_published),
        "revoked_models": sorted(revoked_new),
        "newly_revoked": newly_revoked,
        **result,
    }, "已发布到平台")
