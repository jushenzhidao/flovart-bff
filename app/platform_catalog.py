"""平台共享服务目录 + 「下架闸门」—— 单一事实源。

## 为什么需要这个模块（飞哥 2026-09-16 需求原文）

> 「我发布过的 AI 服务，我管理员删除掉了，用户使用的时候还是可以看到；
>   我希望加一个下架按钮、且直接删除后用户这边也不应该看到，
>   就算用户没有刷新拉取，直接使用也是提示用户模型下架了」

**问题的本质**：平台服务在用户侧是**本地影子条目**（`App.tsx` 把服务端清单
注入用户 keyVault，`extraConfig.platformSource='1'`）。它是**持久化**的，
只在「登录 / 冷启动」时与 服务端对齐一次。于是管理员删除或下架后：

- 已经打开的页面（没刷新）→ 本地影子条目还在 → 模型照样出现在选择器里、照样能调；
- 就算刷新了，只要该模型在上游网关渠道里还存在，用户仍然能选中并调用成功。

**结论**：靠前端拉取只能治「显示」，治不了「能不能用」。准入判定必须落在**服务端**。

## 设计：撤回集（revoked）而不是允许集

直觉方案是「模型必须在平台已发布清单里才放行」，但这会**误杀两条正常链路**：

1. **平台 Key 池**（`/api/me/ensure-key` + `/api/models`）：那条链路给的模型来自
   用户分组下的网关渠道目录，**根本不在**平台服务清单里 → 全部被拦，平台整体不可用。
2. **BYOK 外呼**（`_gateway`）：用户自己配的端点，与平台无关。

所以反过来记：**只记「曾经发布过、现在没了」的模型名**（`revoked`）。
判定规则极简：

    下架/删除后新增的撤回名 = (历史撤回集 ∪ 上次已发布集) − 本次已发布集

好处：
- 管理员**重新发布**同名模型 → 它出现在「本次已发布集」→ 自动从撤回集移除（自愈）；
- 从没发布过的模型（网关渠道目录、用户 BYOK）→ 不在撤回集里 → 不受影响；
- 「删除」不需要管理员额外操作 —— PUT 是整体覆盖，diff 自动算出被删掉的那些。

## 数据形状（复用 cloud_docs，uid=0 / scope='platform' / doc_key='services'）

```jsonc
{
  "services": [ { "id": "...", "name": "...", "models": [...],
                  "suspended": false, ... } ],
  "revoked": { "gemini-2.5-flash-image-preview": 1789541248.5 }   // 模型名(小写) → 时间戳
}
```

`revoked` 会随每次 PUT 重算并落盘；超过 `REVOKED_TTL_DAYS` 或超过
`REVOKED_MAX` 条时按时间裁剪，避免无限膨胀。

⚠️ 没有「撤回」写入权限的旁路：`revoked` 只由 `PUT /api/platform/services`
（require_admin）维护，用户无法构造。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterable

from . import cloudstore

logger = logging.getLogger("bff.platform")

# 保留维度：uid=0 不是合法 new-api 用户（用户 id 从 1 起），
# 且用户侧 uid 全部来自服务端签发的加密会话 —— 不可能被伪造成 0。
PLATFORM_UID = 0
PLATFORM_SCOPE = "platform"
PLATFORM_DOC_KEY = "services"

# 缓存：每次任务提交/模型目录查询都读一次 DB 没必要（单行不变），
# 但**不能缓存太久** —— 管理员刚点下架，用户下一次请求就该被拦。
CACHE_TTL_SECONDS = 3.0

# 撤回集保留时限 / 上限（超期或超量按时间裁剪，防无限膨胀）
#
# ⚠️ 上限只是「异常写入」的保险丝，**不是业务策略** —— 绝不能小到会裁掉
# 正常的撤回记录，否则被裁掉的那部分模型会**静默漏过闸门、照旧可调用**。
#
# 2026-09-16 实测踩坑：一条平台服务携带的是**整个网关目录**
# （本地实测 741 / 743 个模型）。把服务全部下架 → 743 条撤回记录，
# 而原值 500 会裁掉 243 条 → 这 243 个模型仍能被调用，
# 直接违背「下架了就一定拦得住」。故提到现实中不可能触发的量级
# （万位），且真触发时打 ERROR 而不是静默丢弃。
REVOKED_TTL_DAYS = 180
REVOKED_MAX = 20000

_CACHE: dict[str, Any] = {"at": 0.0, "state": None}


class ModelSuspendedError(Exception):
    """模型已被平台下架。由 main.py 的全局处理器转成 409 + 统一响应壳。"""

    code = "MODEL_SUSPENDED"

    def __init__(self, model: str, service_name: str = "", reason: str = "suspended"):
        self.model = model
        self.service_name = service_name
        # reason: suspended=后台下架 / removed=服务条目已删除
        self.reason = reason
        suffix = f"（原服务：{service_name}）" if service_name else ""
        why = "已被平台下架" if reason == "suspended" else "所属平台服务已被删除"
        super().__init__(
            f"模型「{model}」{why}{suffix}，暂时无法使用。"
            "请刷新页面后重新选择可用模型，或联系管理员。")
        self.message = str(self)


def norm(model: Any) -> str:
    """模型名归一化：小写 + 去空白。撤回集与判定都用这个键。"""
    return str(model or "").strip().lower()


def service_models(service: dict) -> list[str]:
    """取一条服务的模型清单（去重保序，兼容 models / customModels 两个字段）。"""
    if not isinstance(service, dict):
        return []
    raw: Iterable[Any] = service.get("models") or service.get("customModels") or []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for m in raw:
        name = str(m or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def is_suspended(service: dict) -> bool:
    """该服务是否处于下架状态（缺字段 = 上架，兼容存量数据）。"""
    return bool(isinstance(service, dict) and service.get("suspended"))


def service_suspended_set(service: dict) -> set[str]:
    """单条服务里被**模型级下架**的模型名（小写集合，2026-09-17 飞哥：下架要解耦到模型）。

    `suspendedModels` 只在服务整体上架时生效；服务整体下架时它的全部模型
    本来就不可用，无需再看这份清单。
    """
    if not isinstance(service, dict):
        return set()
    raw = service.get("suspendedModels")
    if not isinstance(raw, list):
        return set()
    return {norm(m) for m in raw if str(m or "").strip()}


def published_models(services: list) -> dict[str, str]:
    """当前**已发布**的模型名 → 服务展示名。

    不计入：①整体下架的条目；②上架条目里被模型级下架（`suspendedModels`）的模型。
    """
    out: dict[str, str] = {}
    for s in services:
        if not isinstance(s, dict) or is_suspended(s):
            continue
        name = str(s.get("name") or "").strip()
        hidden = service_suspended_set(s)
        for m in service_models(s):
            if norm(m) in hidden:
                continue
            out.setdefault(norm(m), name)
    return out


def suspended_models(services: list) -> dict[str, str]:
    """当前**不可用**的模型名 → 服务展示名（用于把错误说清楚）。

    两部分并集：①整体下架条目的全部模型；②上架条目里被模型级下架的模型。
    """
    out: dict[str, str] = {}
    for s in services:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or "").strip()
        if is_suspended(s):
            for m in service_models(s):
                out.setdefault(norm(m), name)
            continue
        hidden = service_suspended_set(s)
        for m in service_models(s):
            if norm(m) in hidden:
                out.setdefault(norm(m), name)
    return out


def _prune_revoked(revoked: dict[str, float], now: float) -> dict[str, float]:
    """按时间裁剪撤回集（超期先丢，再按上限截断最旧的）。

    ⚠️ 走到「按上限截断」就说明保险丝烧了 —— 被丢掉的模型会**漏过闸门**，
    所以这里打 ERROR 而不是静默，方便从日志里立刻发现。
    """
    floor = now - REVOKED_TTL_DAYS * 86400
    kept = {m: float(t) for m, t in revoked.items() if isinstance(t, (int, float)) and float(t) >= floor}
    if len(kept) > REVOKED_MAX:
        dropped = len(kept) - REVOKED_MAX
        newest = sorted(kept.items(), key=lambda kv: kv[1], reverse=True)[:REVOKED_MAX]
        kept = dict(newest)
        logger.error(
            "撤回集超过上限 REVOKED_MAX=%d，已丢弃 %d 条最旧记录 —— "
            "这些模型将不再被下架闸门拦住，请立即检查是否异常写入（如把整个网关目录当模型清单发布）",
            REVOKED_MAX, dropped)
    return kept


def prune_revoked(revoked: Any) -> dict[str, float]:
    """对外暴露的裁剪入口（PUT 写盘前先裁一次，避免旧数据里的过期项被永久保留）。"""
    if not isinstance(revoked, dict):
        return {}
    return _prune_revoked(revoked, time.time())


def merge_revoked(
    *,
    prev_published: dict[str, str],
    next_published: dict[str, str],
    revoked_old: dict[str, float],
    now: float | None = None,
) -> dict[str, float]:
    """算出新的撤回集 —— 模块头「设计」段的公式，纯函数便于单测。

    `(历史撤回集 ∪ 上次已发布集) − 本次已发布集`：
    - 被删掉 / 被下架的模型 → 进撤回集；
    - 重新发布的模型 → 从撤回集移出（自愈）。
    """
    now = time.time() if now is None else now
    merged: dict[str, float] = {}
    for m, t in revoked_old.items():
        merged[norm(m)] = float(t)
    for m in prev_published:
        merged.setdefault(norm(m), now)
    for m in next_published:
        merged.pop(norm(m), None)
    return _prune_revoked(merged, now)


async def read_state(force: bool = False) -> dict:
    """读取平台目录状态（带短 TTL 缓存）。

    返回 `{"services", "revoked", "published", "suspended", "revision", "updated_at"}`：
    - `services`  原始条目（含 suspended 标记），供路由下发/管理台使用；
    - `revoked`   撤回集（模型名小写 → 时间戳）；
    - `published` 当前已发布模型 → 服务名；
    - `suspended` 当前下架条目的模型 → 服务名。
    """
    now = time.monotonic()
    cached = _CACHE.get("state")
    if not force and cached is not None and (now - float(_CACHE.get("at") or 0.0)) < CACHE_TTL_SECONDS:
        return cached
    state = await _load_state()
    _CACHE["state"] = state
    _CACHE["at"] = now
    return state


async def _load_state() -> dict:
    """真读一次 DB。任何异常都兜底成「空目录 + 空撤回集」——
    宁可放行也不要把整个平台拦死（目录读不到时的可用性优先）。"""
    try:
        doc = await cloudstore.doc_get(PLATFORM_UID, PLATFORM_SCOPE, PLATFORM_DOC_KEY)
    except Exception as e:  # noqa: BLE001
        logger.warning("平台目录读取失败，本次按空目录处理: %s", e)
        doc = None
    payload = (doc or {}).get("payload") or {}
    services = payload.get("services")
    services = services if isinstance(services, list) else []
    revoked_raw = payload.get("revoked")
    revoked = _prune_revoked(revoked_raw, time.time()) if isinstance(revoked_raw, dict) else {}
    return {
        "services": services,
        "revoked": revoked,
        "published": published_models(services),
        "suspended": suspended_models(services),
        "revision": (doc or {}).get("revision", 0),
        "updated_at": (doc or {}).get("updated_at"),
    }


def invalidate() -> None:
    """写入平台服务后必须调用，否则下架最长 3 秒后才生效。"""
    _CACHE["state"] = None
    _CACHE["at"] = 0.0


async def revoked_models() -> dict[str, float]:
    return dict((await read_state())["revoked"])


async def assert_model_available(model: Any) -> None:
    """闸门：模型被下架/删除则抛 `ModelSuspendedError`，否则静默返回。

    ⚠️ 只拦撤回集里的模型 —— 网关渠道目录（平台 Key 池）与用户 BYOK 的模型
    从来没进过撤回集，天然不受影响。
    """
    key = norm(model)
    if not key:
        return
    state = await read_state()
    if key in state["suspended"]:
        raise ModelSuspendedError(str(model), state["suspended"][key], reason="suspended")
    if key in state["revoked"]:
        # 条目已被删除 → 撤回集里没有服务名可回填（那时条目已不在 list 里）
        raise ModelSuspendedError(str(model), "", reason="removed")


async def filter_available(models: Iterable[Any]) -> list[str]:
    """把已下架/已删除的模型从模型目录里剔掉（保序去重）。

    用在 `GET /api/models`：用户侧模型列表因此自动不再出现下架模型 ——
    即便用户本地 keyVault 里的影子条目还没被清掉，选择器里也不会再列出来。
    """
    state = await read_state()
    blocked = set(state["revoked"]) | set(state["suspended"])
    out: list[str] = []
    seen: set[str] = set()
    for m in models:
        name = str(m or "").strip()
        k = norm(name)
        if not k or k in blocked or k in seen:
            continue
        seen.add(k)
        out.append(name)
    return out


def summarize(state: dict) -> str:
    """一行摘要，用于日志。"""
    return (f"services={len(state['services'])} "
            f"published={len(state['published'])} "
            f"suspended={len(state['suspended'])} "
            f"revoked={len(state['revoked'])}")
