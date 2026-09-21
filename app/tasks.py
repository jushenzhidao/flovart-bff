"""图片任务：BFF 同时兼容「同步 / 异步」，并在每次提交时落一条请求日志。

设计（契约见 IMAGE-ASYNC-TASKS-CONTRACT.md §1、§4）：
- 各 task type 由 TASK_TYPES 指定 mode 与网关端点：
    * async：真正执行方是网关侧（new-api 兼容）。BFF 透传「提交→轮询→取消」，
             任务状态由网关持有；BFF 不跑 worker、不存任务表。
    * sync ：网关部分模型更适合同步直出（转异步成本高）。BFF 阻塞调用网关同步
             接口、拿到结果后立即把产物落 BFF 云盘，并把 result 回写请求日志。
- 请求日志（按用户 request_id 落 PG/本地兜底）：记录「用户调用模型时的请求结构+参数」
  （payload_json）、task_id、网关 request_id（对齐网关 /api/log 全站日志）、状态、结果。
  供后续「拉日志对齐」——前端可经 GET /api/me/requests 拉取，BFF 与网关日志用
  gateway_request_id 串联。

## 产物统一归 BFF 盘（2026-09-07 拍板）
任务成功（async 轮询到 succeeded / sync 直出）时，BFF 把 result 内的图片产物
（url 下载 / base64 解码）落进 cloud_media（与画布 projects / 上传素材 / 生成历史
共用同一套 BFF 存储），并把 result 改写为指向 BFF media 地址（附 _bffMediaKey）。
换设备恢复画布时，图不再依赖网关有效期，全部自 BFF 盘取回。
- 视频产物现已接入 BFF 代理（video-gen，与 image-gen 同构走 new-api 平台透传）；
  仅「非 new-api 平台的第三方视频网关 BYOK 直连」仍由前端拿 blob 后 POST /api/me/media 回存 BFF。
- 落盘为异步 `await cloudstore.media_put`（BFF→OSS put_object + PG 索引 upsert）。
- 进程内 _PERSISTED 做幂等：同进程重复 GET 不重复落盘；重启重落无害（多一份相同字节）。
"""
import asyncio
import base64
import json
import logging
import re
import uuid
from typing import Any

import httpx

from . import config, cloudstore, image_model_modes, newapi_client as na, user_keys
from .newapi_client import NewApiError

logger = logging.getLogger("bff.tasks")


# ---------------------------------------------------------------------------
# 用户 sk- 取用（2026-09-15 关键修复）
# ---------------------------------------------------------------------------
# 🔴 为什么不能用 request_as_user（管理员 PAT）打任务端点：
#   new-api 的 **`/v1/*` 只认 API Key(`sk-`)，不认 PAT**（PAT 仅适用于管理类 `/api/*`）。
#   用 PAT 打 `v1/video/generations` / `v1/images/generations` 一律 **401 Invalid token**，
#   前端表现为「生成失败（凭证已失效）」——即使登录态完全正常。
#   ⚠️ 这是架构层约束，与登录状态无关；chat.py 早已用同一机制规避（按需发放用户 sk-）。
#
# 取用顺序：① 复用 user_keys 已存的 sk-；② 管理员凭证代建（无需用户 PAT）并持久化。
# 轮换：请求遇 401 时删除旧 key、重新代建并重试一次（与 chat.py 同款自愈）。
async def _user_api_key(uid: int, *, rotate: bool = False) -> str:
    """拿到该用户用于 `/v1` 的 sk-（复用已存 / 管理员代建），加密持久化。"""
    if rotate:
        user_keys.delete_key(uid)
    key = user_keys.get_key(uid)
    if key:
        return key
    key = await na.admin_mint_user_api_key(uid)  # 失败抛 NewApiError，由上层转 502
    user_keys.set_key(uid, key)
    return key


async def _gw_call(method: str, path: str, uid: int, *,
                   json: Any = None, params: dict | None = None,
                   client: "httpx.AsyncClient | None" = None) -> Any:
    """以该用户自己的 sk- 调网关 `/v1` 端点（计费/隔离落到该 uid）。

    401 时自动轮换 sk- 重试一次（key 被用户在网关撤销 / 换账号等场景自愈）。
    """
    key = await _user_api_key(uid)
    headers = {"Authorization": f"Bearer {key}"}
    try:
        return await na.request(method, path, headers=headers, json=json,
                                params=params, client=client)
    except NewApiError as e:
        if e.status_code != 401:
            raise
        logger.warning("用户 sk- 被拒(401)，轮换后重试 uid=%s path=%s", uid, path)
        key = await _user_api_key(uid, rotate=True)
        return await na.request(method, path, headers={"Authorization": f"Bearer {key}"},
                                json=json, params=params, client=client)

# 任务类型 → 执行模式 + 网关端点 / 第三方 Provider。
# provider:
#   "gateway"     → 走 new-api 网关（image-gen/upscale/remove-background/split-layers/video-gen）
#   "thirdparty"  → 直连第三方 API（multi-angle 等网关未接入的能力），tp 指定服务商
# mode=async 走异步（网关 tasks / 第三方 prediction 轮询）；mode=sync 走同步直出。
# image-gen 的 mode 由 GATEWAY_IMAGE_GEN_MODE 决定（默认 async），可 env 切 sync。
TASK_TYPES: "dict[str, dict]" = {
    "image-gen": {
        "provider": "gateway",
        "mode": config.GATEWAY_IMAGE_GEN_MODE,  # "async" | "sync"
        "async_path": config.GATEWAY_IMAGE_TASKS_PATH,
        "sync_path": config.GATEWAY_SYNC_IMAGE_PATH,
    },
    "upscale": {"provider": "gateway", "mode": "sync", "sync_path": config.GATEWAY_SYNC_UPSCALE_PATH},
    "remove-background": {"provider": "gateway", "mode": "sync", "sync_path": config.GATEWAY_SYNC_REMOVE_BG_PATH},
    # 图片编辑类（原纯 BYOK 直连，现走 BFF→网关，用户免 Key）：
    # 扩展画面 / 编辑蒙版 / 标注涂鸦 / 打光面板 / 通用编辑（换装等）。
    # 均为 gateway sync 直出；参数（image/prompt/mask/variant）原样透传网关同步端点。
    "outpaint": {"provider": "gateway", "mode": "sync", "sync_path": config.GATEWAY_SYNC_OUTPAINT_PATH},
    "mask": {"provider": "gateway", "mode": "sync", "sync_path": config.GATEWAY_SYNC_MASK_PATH},
    "annotate": {"provider": "gateway", "mode": "sync", "sync_path": config.GATEWAY_SYNC_ANNOTATE_PATH},
    "relight": {"provider": "gateway", "mode": "sync", "sync_path": config.GATEWAY_SYNC_RELIGHT_PATH},
    "edit": {"provider": "gateway", "mode": "sync", "sync_path": config.GATEWAY_SYNC_EDIT_PATH},
    "split-layers": {"provider": "thirdparty", "tp": "wavespeed", "mode": "async"},
    # 直连第三方：多角度（wave speed，FLUX Kontext Max Multi 保主体一致性）
    "multi-angle": {"provider": "thirdparty", "tp": "wavespeed", "mode": "async"},
    # 视频生成：与 image-gen 同构，走 new-api 平台透传（异步）。new-api 用「统一 tasks 端点」
    # 按 type 分流（视频/图片同端点，BFF 不感知具体端点）。默认 async；产物为 mp4，落 BFF 盘
    # 时 kind="video"。端点路径用 GATEWAY_VIDEO_TASKS_PATH（默认与图片 tasks 端点一致）。
    "video-gen": {"provider": "gateway", "mode": "async", "async_path": config.GATEWAY_VIDEO_TASKS_PATH},
}


def resolve_split_model(params: dict) -> str:
    """split-layers 的模型语义值 → 真实模型 id。

    前端只发 model:'pro'（语义值），真实模型名永远不进前端、由 BFF 在此映射。
    本函数是**唯一映射点**：router 的管理员闸门与 _submit_thirdparty 提交共用，
    防止两处判断漂移。其余/缺省值一律回默认 qwen 模型（普通用户通道）。
    """
    raw = str(params.get("model") or "").strip().lower()
    if raw in ("pro", "seedream", "seedream-pro"):
        return config.WAVESPEED_SPLIT_MODEL_PRO
    return config.WAVESPEED_SPLIT_MODEL

# 代理只需快速转发（提交/查询/取消都应立即返回），用较长但非无限的超时。
_PROXY_CLIENT: "httpx.AsyncClient | None" = None
# 同步调用网关（可能较长）的独立 client，超时更长。
_SYNC_CLIENT: "httpx.AsyncClient | None" = None
# 下载网关产物（绝对 url）用的独立 client，无 base_url。
# 超时给足余量：图片通常秒级，但视频产物（mp4）可能数 MB~数十 MB，下载需更久。
_DL_CLIENT: "httpx.AsyncClient | None" = None
# 进程内幂等：task_id -> 已改写的 result（避免重复落盘）。
_PERSISTED: "dict[str, dict]" = {}
# 后台外部网关调用的强引用（asyncio 只持弱引用，不留引用会被 GC 掉，任务静默消失）。
_BACKGROUND_TASKS: "set[asyncio.Task]" = set()


def _proxy_client() -> httpx.AsyncClient:
    global _PROXY_CLIENT
    if _PROXY_CLIENT is None:
        _PROXY_CLIENT = httpx.AsyncClient(
            base_url=config.NEWAPI_BASE_URL,
            timeout=httpx.Timeout(config.GATEWAY_PROXY_TIMEOUT, connect=10.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
            trust_env=False,  # 不走本机代理
        )
    return _PROXY_CLIENT


def _sync_client() -> httpx.AsyncClient:
    global _SYNC_CLIENT
    if _SYNC_CLIENT is None:
        _SYNC_CLIENT = httpx.AsyncClient(
            base_url=config.NEWAPI_BASE_URL,
            timeout=httpx.Timeout(config.GATEWAY_SYNC_TIMEOUT, connect=10.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
            trust_env=False,
        )
    return _SYNC_CLIENT


def _dl_client() -> httpx.AsyncClient:
    global _DL_CLIENT
    if _DL_CLIENT is None:
        _DL_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(180.0, connect=10.0),
            follow_redirects=True,
            trust_env=False,
        )
    return _DL_CLIENT


async def close() -> None:
    """lifespan 关闭时归还连接池。"""
    global _PROXY_CLIENT, _SYNC_CLIENT, _DL_CLIENT
    for c in (_PROXY_CLIENT, _SYNC_CLIENT, _DL_CLIENT):
        if c is not None:
            await c.aclose()
    _PROXY_CLIENT = _SYNC_CLIENT = _DL_CLIENT = None


def _unwrap(body: Any) -> Any:
    """容忍网关 new-api 风格 {success,data} 与裸 Task 两种返回，恒返回内层对象。"""
    if isinstance(body, dict) and "data" in body and isinstance(body["data"], (dict, list)):
        return body["data"]
    return body


def _normalize_task(task: Any) -> Any:
    """轻量对齐：确保 task_id 字段存在（网关若返 id 也认）。"""
    if isinstance(task, dict) and "task_id" not in task and "id" in task:
        task = {**task, "task_id": task["id"]}
    return task


def _normalize_sync_result(raw: Any) -> "dict | None":
    """把网关同步直出响应归一化成 _iter_outputs 可识别的 result 形状。

    兼容常见网关返回：
      - OpenAI 图片风 {data:[{url|b64_json}, ...]}
      - {image:{...}} / {url|...} 单图
      - 分层 {layers:[{...}, ...], image:{...}}
    返回 result dict（含 data/image/layers/url 之一），或 None。
    """
    raw = _unwrap(raw)
    if isinstance(raw, list):
        return {"images": raw}
    if not isinstance(raw, dict):
        return None
    if "data" in raw and isinstance(raw["data"], list):
        # OpenAI 风格 {data:[...]} → 统一成 {images:[...]}
        # 前端 extractImageOutputs 只扫描 image/images/layers，不认 data，
        # 不转换会导致「任务未返回媒体」、画面无图（chatfire 等网关实测返回 data）。
        return {"images": raw["data"], **({k: v for k, v in raw.items() if k != "data"})}
    if "layers" in raw:
        return {"layers": raw["layers"], **({"image": raw["image"]} if "image" in raw else {})}
    if "image" in raw:
        return {"image": raw["image"]}
    if "url" in raw or "b64_json" in raw or "base64" in raw:
        return raw
    # 兜底：原样当作 result（_iter_outputs 顶层 take 仍可能命中）
    return raw


def _extract_gw_request_id(raw: Any) -> "str | None":
    """从网关响应里取 request_id（对齐网关全站日志 /api/log 的 request_id 字段）。"""
    if isinstance(raw, dict):
        for k in ("request_id", "requestId", "requestID", "id"):
            v = raw.get(k)
            if isinstance(v, str) and v:
                return v
    return None


# ---------------------------------------------------------------------------
# 产物落盘：把 result 内的图片落 BFF cloud_media，改写 result 指向 BFF media
# ---------------------------------------------------------------------------
def _iter_outputs(result: dict) -> "list[dict]":
    """收集 result 内所有可作为产物的对象（原地引用，便于改写 url）。"""
    outs: "list[dict]" = []

    def take(o: Any) -> None:
        if isinstance(o, dict) and (
            o.get("url") or o.get("b64_json") or o.get("base64")
            or o.get("dataUrl") or o.get("data_url")
        ):
            outs.append(o)

    take(result)  # 顶层可能直接带 url / b64
    for k in ("data", "image", "images", "layers", "video", "videos"):
        v = result.get(k)
        if isinstance(v, list):
            for it in v:
                take(it)
        elif isinstance(v, dict):
            take(v)
    if isinstance(result.get("outputs"), list):  # string[] url 列表
        for u in result["outputs"]:
            if isinstance(u, str):
                outs.append({"url": u})
    return outs


def _upstream_usage(raw: Any) -> Any:
    """取上游 usage，用于诊断「200 但无产物」（completion_tokens=0 ⇒ 模型侧未产出）。"""
    if isinstance(raw, dict):
        return raw.get("usage")
    return None


def _no_media_error(raw: Any) -> dict:
    """上游 HTTP 200 但归一化后无任何产物 → 生成可诊断的失败记录。

    实测（2026-09-16，chatfire /v1/images/generations）：
    gemini 系列图片模型会以 **HTTP 200** 返回
    `{"created":…,"usage":{"prompt_tokens":5,"completion_tokens":0,…}}`，
    **完全不带 data** —— 即模型侧确实没出图（直连同一把 key 可复现，
    非 BFF 解析问题；实测偶发率约 1/4）。

    ⚠️ 此前该响应被归一化成 `{"created","usage"}` 并按 `status=succeeded` 落库，
    前端拿不到任何产物 → 只能显示兜底文案「任务未返回媒体」，
    上游「空产出」这个真实原因彻底丢失，排查时无从下手。
    故此处必须记 failed，并把 usage / 上游顶层键一并留痕。
    """
    return {
        "error": {
            "message": "上游返回成功但未产出图片（模型侧空结果，属上游偶发失败，请重试）",
            "type": "upstream_empty_result",
            "stage": "normalize",
            "usage": _upstream_usage(raw),
            "upstream_keys": sorted(raw.keys()) if isinstance(raw, dict) else None,
        }
    }


def _decode_b64(s: str) -> "tuple[bytes | None, str | None]":
    s = (s or "").strip()
    mime: "str | None" = None
    if s.startswith("data:"):
        m = re.match(r"data:([^;]+);base64,(.*)", s, re.S)
        if m:
            mime, s = m.group(1), m.group(2)
    try:
        return base64.b64decode(s, validate=False), mime
    except Exception:
        return None, None


async def _resolve_blob(obj: dict, uid: int) -> "tuple[bytes | None, str | None]":
    """从产物对象解析出二进制 blob + mime；无法解析返回 (None, None)。"""
    url = obj.get("url") or obj.get("href") or obj.get("image_url") or obj.get("imageUrl")
    if url and isinstance(url, str):
        try:
            async with _dl_client().stream(
                "GET", url, headers={"New-Api-User": str(uid)}
            ) as r:
                if r.status_code != 200:
                    logger.warning("下载网关产物失败(http=%s)，跳过落盘: %s", r.status_code, url)
                    return None, None
                data = await r.aread()
                mime = (r.headers.get("content-type") or "").split(";")[0] or None
                return data, mime
        except Exception as e:
            logger.warning("下载网关产物异常，跳过落盘: %s (%s)", url, e)
            return None, None
    for key in ("b64_json", "base64", "dataUrl", "data_url"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            blob, mime = _decode_b64(val)
            if blob:
                return blob, mime
    return None, None


async def _persist_outputs(task: dict, task_id: str, uid: int, kind: str = "image",
                           request_id: "str | None" = None) -> dict:
    """任务成功：把 result 内图片/视频落 BFF cloud_media，改写 result 指向 BFF media。

    幂等：同进程已落过直接复用缓存的改写 result，不重复落盘。
    kind 用于决定落盘媒体类型：video-gen → "video"，其余（含分层透明 png）→ "image"。
    request_id 用于血缘：产物 media 行记 source_request_id，关联 cloud_request_log。
    """
    if task_id in _PERSISTED:
        task["result"] = _PERSISTED[task_id]
        return task
    result = task.get("result")
    if not isinstance(result, dict):
        return task
    outs = _iter_outputs(result)
    if not outs:
        return task

    media_kind = "video" if kind == "video-gen" else "image"
    bff: "list[dict]" = []
    changed = False
    for obj in outs:
        blob, mime = await _resolve_blob(obj, uid)
        if not blob:
            continue
        if not mime:
            mime = obj.get("mime") or obj.get("mimeType") or (
                "video/mp4" if media_kind == "video" else "image/png")
        try:
            info = await cloudstore.media_put(uid, media_kind, mime, blob)
        except ValueError as e:
            logger.warning("落盘产物失败(配额/大小)，跳过: %s", e)
            continue
        obj["url"] = info["url"]
        obj["_bffMediaKey"] = info["media_key"]
        bff.append({"mediaKey": info["media_key"], "mime": info["mime"], "url": info["url"]})
        changed = True

    if changed:
        result["bffMedia"] = bff
        _PERSISTED[task_id] = result
    return task


# ---------------------------------------------------------------------------
# 视图 + 日志
# ---------------------------------------------------------------------------
def _task_view(request_id: str, task_id: "str | None", kind: str, mode: str,
               status: str, gw: Any) -> dict:
    gw = gw if isinstance(gw, dict) else {}
    return {
        "id": request_id,
        "taskId": task_id,
        "kind": kind,
        "mode": mode,
        "status": status,
        "result": gw.get("result"),
        "gatewayTask": gw,  # 透传网关原始对象（含 progress 等字段）
    }


async def _log_submitted(uid: int, request_id: str, kind: str, mode: str,
                         params: dict, task_id: "str | None" = None) -> None:
    provider = params.get("provider")
    model = params.get("model") or params.get("model_name")
    await cloudstore.request_log_put(
        uid, request_id, kind, provider, model, params, status="submitted", mode=mode)
    if task_id:
        await cloudstore.request_log_update(request_id, status="processing", task_id=task_id)


# ---------------------------------------------------------------------------
# 对外 API 辅助（被 routers/tasks.py 调用）
# ---------------------------------------------------------------------------
async def submit(uid: int, kind: str, params: dict) -> dict:
    """提交任务：按 TASK_TYPES[kind] 分流。

    - provider="gateway"：async 透传网关 tasks / sync 阻塞直出。
    - provider="thirdparty"：直连第三方 API（提交→轮询→落 BFF 盘）。

    返回统一视图 {id, taskId, kind, mode, status, result?, gatewayTask}；
    前端用 id 轮询 GET /api/tasks/{id}。
    """
    spec = TASK_TYPES.get(kind)
    if spec is None:
        raise ValueError(f"unsupported task type: {kind}")
    request_id = uuid.uuid4().hex
    provider = spec.get("provider", "gateway")

    if provider == "thirdparty":
        return await _submit_thirdparty(uid, request_id, kind, spec, params)

    mode = spec["mode"]
    # 所有网关 task 均支持按请求参数覆盖模式（与 image-gen 一致）：
    # 1) 前端 AI 服务弹窗显式指定了 params.mode（最优先）—— 用户在每个 AI 服务里配置的
    #    同步/异步随 params.mode 带来，调用该模型时完全按此配置来。
    # 2) 否则按模型名查 BFF 全局 image_model_modes（管理台兜底，兼容旧配置）
    # 3) 否则回退 TASK_TYPES 里的全局默认。
    if params.get("mode") in ("sync", "async"):
        mode = params["mode"]
    else:
        model = params.get("model") or params.get("model_name")
        if model:
            override = image_model_modes.get_mode(model)
            if override in ("sync", "async"):
                mode = override
    # 诊断日志：先看清 mode 到底是怎么定的
    logger.info("tasks.submit kind=%s model=%s params.mode=%s resolved_mode=%s", kind, params.get("model"), params.get("mode"), mode)
    # 外部 OpenAI 兼容网关（按服务路由）：前端带 _gateway，BFF 直连该服务、用其自有 key。
    if params.get("_gateway"):
        return await _run_external(uid, request_id, kind, spec, params)
    if mode == "async":
        # Chatfire 风格真异步生图（doubao-seedream 等）：独立分支，路径/载荷/终态判定都不同。
        if kind == "image-gen" and _is_chatfire_async_image_model(
                params.get("model") or params.get("model_name") or ""):
            return await _submit_chatfire_async_image(uid, request_id, params)
        try:
            # 网关异步分支此前缺 request_log_put，导致外部/异步请求无留痕；此处补上（无 _gateway，无需掩码）。
            await cloudstore.request_log_put(
                uid, request_id, kind, "gateway",
                params.get("model") or params.get("model_name") or "",
                {**params}, status="submitted", mode="async")
            body = {"type": kind, "params": params}
            # ⚠️ 必须用用户 sk- 打 /v1（PAT 不被 /v1 接受，会 401）—— 见 _gw_call 注释。
            raw = await _gw_call(
                "POST", spec["async_path"], uid, json=body, client=_proxy_client())
        except NewApiError:
            await cloudstore.request_log_update(request_id, status="failed")
            raise
        task = _normalize_task(_unwrap(raw))
        task_id = task.get("task_id") or task.get("id")
        await cloudstore.request_log_update(request_id, status="processing", task_id=task_id)
        return _task_view(request_id, task_id, kind, "async",
                          task.get("status", "processing"), task)

    # sync：调用网关同步接口。
    # ⚠️ 2026-09-15 两处修复：
    #  ① 必须**先 request_log_put 落一条 submitted 记录**，否则 _run_sync 内部的
    #     request_log_update（UPDATE）打在不存在的行上 → 静默 0 行 → 表现为
    #     「提交返回 succeeded，但轮询 GET /api/tasks/{id} 恒 404 / status=None」。
    #     （async 分支一直有这次 put，sync 分支此前漏了。）
    #  ② 改为**非阻塞**（与 _run_external 同款）：网关同步出图常 60s+，
    #     若在此 await 到底，nginx `proxy_read_timeout`（默认 60s）会先掐断连接，
    #     前端收到裸 Network Error 而**上游其实已出图**（用户白扣费看不到图）。
    #     故此处只落日志 + 起后台任务后立即返回 `processing`，
    #     前端拿 request_id 走既有 GET /api/tasks/{id} 轮询（mode=sync 分支直接读结果）。
    await cloudstore.request_log_put(
        uid, request_id, kind, "gateway",
        params.get("model") or params.get("model_name") or "",
        {**params}, status="submitted", mode="sync")
    _spawn_sync_call(uid, request_id, kind, spec["sync_path"], params)
    return _task_view(request_id, request_id, kind, "sync", "processing", {})


def _spawn_sync_call(uid: int, request_id: str, kind: str, path: str, params: dict) -> None:
    """把网关同步调用丢到后台跑，跑完/失败都回写请求日志（进程内，不依赖 worker）。

    与 `_spawn_external_call` 同款：POST 立即返回，后台协程继续 await 网关长耗时。
    ⚠️ 进程重启会丢在途后台任务；此时日志停在 processing，前端轮询超时后重试即可。
    """
    task = asyncio.create_task(_run_sync_safe(uid, request_id, kind, path, params))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _run_sync_safe(uid: int, request_id: str, kind: str, path: str, params: dict) -> None:
    """后台包装：吞掉一切异常（_run_sync 内部已留痕），避免 create_task 抛未捕获异常。"""
    try:
        await _run_sync(uid, request_id, kind, path, params)
    except Exception:  # noqa: BLE001 —— 内部已写 failed 状态与原因，此处仅防未捕获告警
        logger.exception("同步任务后台执行异常 uid=%s req=%s path=%s", uid, request_id, path)


# Chatfire 异步任务的失败态（文档未穷举，按常见命名 + 实测保守覆盖）。
_CHATFIRE_ASYNC_FAILED = {"FAILED", "ERROR", "CANCELLED", "CANCELED", "TIMEOUT", "DELETED", "EXPIRED"}


def _is_chatfire_async_image_model(model: str) -> bool:
    """Chatfire 风格「真异步」生图模型（2026-09-20 接入，doubao-seedream 等）。

    与 new-api 统一 tasks 端点的 async 不同：POST/GET 同路径 `async/v1/images/generations`、
    裸 {model,prompt,image[]} 体、完成响应以 data[] 出现为终态（无 succeeded 状态字段）。
    """
    m = (model or "").strip().lower()
    return any(m.startswith(p) for p in config.GATEWAY_ASYNC_IMAGE_MODELS)


async def _submit_chatfire_async_image(uid: int, request_id: str, params: dict) -> dict:
    """Chatfire 异步生图提交：POST {model,prompt,image[]} → 202 {task_id,status=QUEUED}。

    ⚠️ 网关对该路径盲透传（不校验模型/渠道），提交 202 ≠ 模型可用；
    真正的渠道路由校验发生在查询阶段（503 model_not_found → 前端轮询可见失败）。
    只透传文档三字段；size 等扩展参数留存在 request_log.params 里不外发，避免上游拒收。
    """
    model = params.get("model") or params.get("model_name") or ""
    await cloudstore.request_log_put(
        uid, request_id, "image-gen", "gateway-async-image",
        model, {**params}, status="submitted", mode="async")
    body = {
        "model": model,
        "prompt": str(params.get("prompt") or ""),
        "image": params.get("image") if isinstance(params.get("image"), list) else [],
    }
    try:
        raw = await _gw_call(
            "POST", config.GATEWAY_ASYNC_IMAGE_SUBMIT_PATH, uid,
            json=body, client=_proxy_client())
    except NewApiError:
        await cloudstore.request_log_update(request_id, status="failed")
        raise
    task = _unwrap(raw)
    if not isinstance(task, dict):
        task = {}
    task = _normalize_task(task)
    task_id = task.get("task_id") or task.get("id")
    await cloudstore.request_log_update(request_id, status="processing", task_id=task_id)
    return _task_view(request_id, task_id, "image-gen", "async",
                      str(task.get("status") or "processing"), task)


async def _poll_chatfire_async_image(request_id: str, log: dict, uid: int) -> dict:
    """Chatfire 异步生图轮询。

    终态判定：响应出现非空 data[] 即完成（chatfire 完成响应**不带** status 字段）；
    处理中为 202 {status:QUEUED|IN_PROGRESS,...}（无 data）；error 体或 FAILED 系状态为失败。
    完成后复用 sync 管线归一化（data→images）+ 落 BFF cloud_media。
    """
    task_id = log["task_id"]
    kind = log["kind"]
    raw = await _gw_call(
        "GET", f"{config.GATEWAY_ASYNC_IMAGE_SUBMIT_PATH}/{task_id}",
        uid, client=_proxy_client())
    body = raw if isinstance(raw, dict) else {}
    if isinstance(body.get("data"), list):
        # data 键出现即终态（处理中响应无 data 键）；空 data = 无产物失败
        gw_req_id = _extract_gw_request_id(body)
        result = _normalize_sync_result(body) if body["data"] else None
        if result is not None and _iter_outputs(result):
            task = {"result": result}
            task = await _persist_outputs(task, task_id, uid, kind, request_id=request_id)
            await cloudstore.request_log_update(
                request_id, status="succeeded", gateway_request_id=gw_req_id,
                result=task.get("result"))
            return _task_view(request_id, task_id, kind, "async", "succeeded", task)
        # 200 但 data 空/不可解析：绝不记 succeeded（同 _run_sync 的无产物防线）
        err = _no_media_error(body)
        await cloudstore.request_log_update(
            request_id, status="failed", gateway_request_id=gw_req_id, result=err)
        return _task_view(request_id, task_id, kind, "async", "failed", {"result": err})
    gstatus = str(body.get("status") or "").upper()
    if isinstance(body.get("error"), dict) or gstatus in _CHATFIRE_ASYNC_FAILED:
        inner = body["error"] if isinstance(body.get("error"), dict) else {
            "message": f"upstream task status: {gstatus or 'unknown'}",
            "type": "gateway_async_error"}
        # 统一包一层 {"error": ...}，对齐 _error_record 的落库形状（前端按 result.error 展示）
        err = {"error": inner}
        await cloudstore.request_log_update(request_id, status="failed", result=err)
        return _task_view(request_id, task_id, kind, "async", "failed", {"result": err})
    await cloudstore.request_log_update(request_id, status="processing")
    return _task_view(request_id, task_id, kind, "async",
                      str(body.get("status") or "processing").lower(), body)


def _is_gemini_image_model(model: str) -> bool:
    """gemini 系图片模型（Nano Banana 等）：走网关 /v1beta 原生端点而非 images 端点。

    2026-09-18 血案：chatfire 的 /v1/images/generations 只认「图片模型分类」，
    gemini-*-image 全部 400（images endpoint requires an image model）；且网关渠道
    （Gemini 类型）只吃原生 generateContent 格式。
    """
    m = (model or "").strip().lower()
    return m.startswith("gemini") and "image" in m


def _guess_url_mime(url: str) -> str:
    """从 URL 路径扩展名猜 mime（presigned URL 的 query 不参与）。"""
    path = (url or "").split("?", 1)[0].lower()
    ext = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
    return {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp",
            "heic": "image/heic", "heif": "image/heif"}.get(ext, "image/png")


async def _gemini_inline_parts(images: Any) -> list:
    """把 OpenAI 风格 image[]（data URL / http URL）转成 Gemini parts。

    - data URL → inlineData（base64）
    - 公网 http(s) URL → fileData.fileUri 直传（Google 官方支持 public/signed URL，
      省去 BFF 下载+base64 双重开销；实测前先走 fileData，若上游不支持再兜底下载转 inline）。
    """
    parts: list = []
    for href in images if isinstance(images, list) else []:
        if not isinstance(href, str) or not href.strip():
            continue
        href = href.strip()
        m = re.match(r"^data:([^;,]+)?;base64,(.*)$", href, re.S)
        if m:
            parts.append({"inlineData": {
                "mimeType": m.group(1) or "image/png", "data": m.group(2)}})
            continue
        if href.startswith(("http://", "https://")):
            # Gemini API 支持公网/签名 URL（fileData.fileUri），上游自行拉取，
            # 避免 BFF 中转下载与 base64 膨胀（70MB 请求体血案同源）。
            parts.append({"fileData": {
                "fileUri": href, "mimeType": _guess_url_mime(href)}})
    return parts


def _gemini_response_to_openai(raw: Any) -> Any:
    """Gemini generateContent 响应 → OpenAI images 风格 {data:[{b64_json}]}。

    非 Gemini 形状（错误体 / 上游兜底返回 OpenAI 格式）原样透传，不影响既有解析。
    """
    if not isinstance(raw, dict) or not raw.get("candidates"):
        return raw
    imgs, text_bits = [], []
    for part in ((raw["candidates"][0].get("content") or {}).get("parts") or []):
        inline = part.get("inlineData") or part.get("inline_data")
        if isinstance(inline, dict) and inline.get("data"):
            imgs.append({"b64_json": inline["data"]})
        elif isinstance(part.get("text"), str) and part["text"]:
            text_bits.append(part["text"])
    if not imgs:
        return raw
    out: dict = {"data": imgs}
    if text_bits:
        out["gemini_text"] = "".join(text_bits)
    return out


async def _run_sync(uid: int, request_id: str, kind: str, path: str, params: dict) -> dict:
    # 同 _run_external：失败必须留痕（catch 所有异常 + 写 result），否则 status=failed 却查不到原因。
    try:
        # ⚠️ 同异步分支：/v1 只认用户 sk-，不能用管理员 PAT（否则 401）。
        model = (params.get("model") or params.get("model_name") or "")
        use_gemini = _is_gemini_image_model(model)
        if use_gemini:
            # Gemini 原生协议：网关渠道为 Gemini 类型，只吃 generateContent 格式
            # （chatfire 对 gemini 图片模型：images 端点 400「requires an image model」）。
            path = f"v1beta/models/{model}:generateContent"
            payload: dict = {"contents": [{
                "role": "user",
                "parts": [{"text": str(params.get("prompt") or "")}]
                + await _gemini_inline_parts(params.get("image")),
            }]}
        else:
            payload = params
        raw = await _gw_call("POST", path, uid, json=payload, client=_sync_client())
        if use_gemini:
            raw = _gemini_response_to_openai(raw)
    except Exception as e:  # noqa: BLE001
        err = _error_record(e, "gateway_sync_error", "request", path=path, uid=uid)
        logger.warning("网关同步调用失败 uid=%s req=%s path=%s err=%s detail=%s",
                       uid, request_id, path, err["error"]["message"],
                       err["error"].get("detail", ""))
        await cloudstore.request_log_update(request_id, status="failed", result=err)
        raise
    gw_req_id = _extract_gw_request_id(raw)
    try:
        result = _normalize_sync_result(raw)
        task = {"result": result} if isinstance(result, dict) else {}
        if result is not None:
            task = await _persist_outputs(task, request_id, uid, kind, request_id=request_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("网关同步结果处理失败 uid=%s req=%s path=%s", uid, request_id, path)
        err = _error_record(e, "gateway_result_error", "normalize/persist", path=path)
        await cloudstore.request_log_update(
            request_id, status="failed", gateway_request_id=gw_req_id, result=err)
        raise
    if not isinstance(result, dict) or not _iter_outputs(result):
        # 200 但无产物：绝不能记 succeeded，否则前端只剩「任务未返回媒体」。
        err = _no_media_error(raw)
        logger.warning("上游同步返回无产物 uid=%s req=%s path=%s usage=%s",
                       uid, request_id, path, err["error"]["usage"])
        await cloudstore.request_log_update(
            request_id, status="failed", gateway_request_id=gw_req_id, result=err)
        return _task_view(request_id, request_id, kind, "sync", "failed", {"result": err})
    await cloudstore.request_log_update(
        request_id, status="succeeded", task_id=request_id,
        gateway_request_id=gw_req_id, result=task.get("result"))
    return _task_view(request_id, request_id, kind, "sync", "succeeded", task)


def _error_record(exc: BaseException, err_type: str, stage: str,
                  *, url: "str | None" = None, path: "str | None" = None,
                  uid: "int | None" = None) -> dict:
    """把异常转成可落库的错误详情（供 request_log.result 与前端展示）。

    ⚠️ 属性名要兼容：NewApiError 用 `.message` / `.status_code`，
    httpx 异常用 `.status`，其它异常只有 `str(e)`。此前手写 getattr 链条时
    误用了 `.detail`（NewApiError 并无该属性），导致 status 丢失 —— 统一收敛到此处。
    """
    msg = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
    status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    detail = getattr(exc, "detail", None) or ""
    err = {
        "error": {
            "message": str(msg),
            "type": err_type,
            "status": status_code,
            "stage": stage,
        }
    }
    if detail:
        # 底层真实原因（httpx 异常类型+原文 / 上游非 JSON 响应预览），排障专用
        err["error"]["detail"] = str(detail)
    if url:
        err["error"]["url"] = url
    if path:
        err["error"]["path"] = path
    if uid is not None:
        err["error"]["uid"] = uid
    return err


async def _run_external(uid: int, request_id: str, kind: str, spec: dict, params: dict) -> dict:
    """外部 OpenAI 兼容网关直连（如 chatfire）：用服务自带 sk- key，按 baseUrl 拼端点。

    前端在 params._gateway 里带 {base_url, api_key}；BFF 直连该服务、不走 new-api admin PAT。

    ⚠️ 非阻塞提交（飞哥 2026-09-11 拍板，务必保持）：
    外部网关（chatfire gpt-image 系列）**出图耗时常在 60s+**，若在此 await 到底，
    nginx `proxy_read_timeout`（默认 60s）会先掐断连接 → 前端收到
    `TypeError: Network Error`（不是「网关报错」），而**上游其实已经出图成功**，
    用户白扣费还看不到图。
    故此处只做「落请求日志 + 建后台任务」后立即返回 `processing`，
    真正的网关调用交给 `asyncio.create_task` 在后台跑完并回写请求日志；
    前端拿 request_id 走既有 GET /api/tasks/{id} 轮询（mode=sync 分支直接读日志结果）。
    这样 POST 恒为毫秒级返回，与生图耗时彻底解耦。
    """
    gw = params.get("_gateway") or {}
    base_url = (gw.get("base_url") or "").strip().rstrip("/")
    api_key = gw.get("api_key") or ""
    if not base_url or not api_key:
        raise ValueError("external gateway 需要 base_url 与 api_key")
    # ⚠️ 兜底值必须带 `v1/` 前缀（2026-09-15 修复）：external 网关同样是
    #    new-api 兼容层，OpenAI 端点全挂在 `/v1/*`；漏掉前缀会打到不存在的
    #    `{base}/images/generations` → nginx 兜底返回前端 SPA 的 HTML → 502。
    path = spec.get("sync_path") or "v1/images/generations"
    url = f"{base_url}/{path}"
    # 诊断日志：外部网关请求也要留痕（掩码 key），便于排查「无图返回」类问题。
    # 注意：params 仍含 _gateway（明文 api_key），落库前必须掩码。
    _log_params = {**params}
    _gw_log = _log_params.get("_gateway")
    if isinstance(_gw_log, dict):
        _log_params["_gateway"] = {**_gw_log, "api_key": "***"}
    # mode 仍记 sync：语义是「网关同步直出」，只是 BFF 侧改为后台等待，
    # 前端轮询行为不变（get_task 的 mode=='sync' 分支直接回读存储结果）。
    await cloudstore.request_log_put(
        uid, request_id, kind, "external",
        params.get("model") or params.get("model_name") or "",
        _log_params, status="submitted", mode="sync")
    # 网关不关心 _gateway 字段，发前剥离；也避免明文 key 误入任何日志。
    payload = {k: v for k, v in params.items() if k != "_gateway"}
    logger.info("外部网关后台提交 uid=%s req=%s url=%s kind=%s", uid, request_id, url, kind)
    _spawn_external_call(uid, request_id, kind, url, api_key, payload)
    return _task_view(request_id, request_id, kind, "sync", "processing", {})


def _spawn_external_call(uid: int, request_id: str, kind: str,
                         url: str, api_key: str, payload: dict) -> None:
    """把外部网关调用丢到后台跑，跑完/失败都回写请求日志（进程内，不依赖 worker）。

    单 worker + asyncio 事件循环下，create_task 即「非阻塞后台执行」：
    POST 立刻返回，后台协程继续 await 网关（长超时也不影响其它请求）。
    ⚠️ 进程重启会丢在途后台任务（极端场景）；此时请求日志停在 processing，
    前端轮询到超时后重试即可 —— 与「网关侧任务丢失」的处理一致，不会产生脏数据。
    """
    task = asyncio.create_task(_external_call_and_record(uid, request_id, kind, url, api_key, payload))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _external_call_and_record(uid: int, request_id: str, kind: str,
                                    url: str, api_key: str, payload: dict) -> None:
    """后台：直连外部网关 → 归一化 → 落 BFF 盘 → 回写请求日志（成功/失败都留痕）。"""
    # 失败必须留痕：此前只 catch NewApiError 且不写 result，导致「status=failed 但 result 为空」
    # 完全无法定位（飞哥 2026-09-11 反馈普通用户文生图失败，DB 里查不到任何原因）。
    # 现在统一捕获【所有】异常（含超时 / 解析 / 落盘失败等非 NewApiError），
    # 把错误信息写进 result（前端 GET /api/me/requests 直接可见）并记日志。
    try:
        raw = await na.request_external("POST", url, api_key=api_key, json=payload)
    except Exception as e:  # noqa: BLE001 —— 必须吞掉一切并留痕，否则失败原因丢失
        err = _error_record(e, "external_gateway_error", "request", url=url, uid=uid)
        logger.warning("外部网关调用失败 uid=%s req=%s url=%s err=%s",
                       uid, request_id, url, err["error"]["message"])
        await cloudstore.request_log_update(request_id, status="failed", result=err)
        return
    gw_req_id = _extract_gw_request_id(raw)
    try:
        result = _normalize_sync_result(raw)
        task = {"result": result} if isinstance(result, dict) else {}
        if result is not None:
            task = await _persist_outputs(task, request_id, uid, kind, request_id=request_id)
    except Exception as e:  # noqa: BLE001 —— 归一化/落盘失败同样要留痕
        logger.exception("外部网关结果处理失败 uid=%s req=%s url=%s", uid, request_id, url)
        err = _error_record(e, "external_result_error", "normalize/persist", url=url)
        await cloudstore.request_log_update(
            request_id, status="failed", gateway_request_id=gw_req_id, result=err)
        return
    if not isinstance(result, dict) or not _iter_outputs(result):
        # 同 _run_sync：外部网关（chatfire 等）同样会 200 空产出，必须记 failed。
        err = _no_media_error(raw)
        logger.warning("外部网关返回无产物 uid=%s req=%s url=%s usage=%s",
                       uid, request_id, url, err["error"]["usage"])
        await cloudstore.request_log_update(
            request_id, status="failed", gateway_request_id=gw_req_id, result=err)
        return
    await cloudstore.request_log_update(
        request_id, status="succeeded", task_id=request_id,
        gateway_request_id=gw_req_id, result=task.get("result"))
    logger.info("外部网关调用成功 uid=%s req=%s url=%s", uid, request_id, url)


# ---------------------------------------------------------------------------
# 第三方直连 Provider 分支（multi-angle 等网关未接入的能力）
# ---------------------------------------------------------------------------
async def _submit_thirdparty(uid: int, request_id: str, kind: str, spec: dict, params: dict) -> dict:
    """直连第三方提交：调对应 tp 适配器拿第三方任务 id，落请求日志后返回 processing 视图。"""
    tp = spec.get("tp")
    if tp == "wavespeed":
        from .thirdparty import wavespeed as ws

        if not config.WAVESPEED_ENABLED:
            raise RuntimeError("WAVESPEED_API_KEY 未配置，第三方能力不可用")
        source = params.get("source_media_key")
        if not source:
            raise ValueError(f"{kind} 需要 source_media_key（源图 BFF media key）")
        try:
            if kind == "multi-angle":
                model = params.get("model") or config.WAVESPEED_MULTIANGLE_MODEL
                tp_task_id = await ws.submit_multi_angle(
                    uid, source,
                    rotate=params.get("rotate", 0), tilt=params.get("tilt", 0),
                    scale=params.get("scale", "medium"),
                    extra_prompt=params.get("prompt", ""), model=model,
                    num_images=int(params.get("num_images", 1)),
                )
            elif kind == "split-layers":
                model = resolve_split_model(params)
                tp_task_id = await ws.submit_split_layers(
                    uid, source,
                    num_layers=int(params.get("num_layers", 4)),
                    prompt=params.get("prompt", ""), model=model,
                    resolution=params.get("resolution"),
                )
            else:
                raise ValueError(f"unsupported thirdparty kind: {kind}")
        except Exception:
            await cloudstore.request_log_update(request_id, status="failed")
            raise
        log_params = {**params, "provider": "thirdparty", "model": model}
        await cloudstore.request_log_put(
            uid, request_id, kind, "thirdparty", model, log_params,
            status="submitted", mode="async")
        await cloudstore.request_log_update(request_id, status="processing", task_id=tp_task_id)
        return _task_view(request_id, tp_task_id, kind, "async", "processing",
                          {"status": "processing", "provider": "thirdparty"})
    raise ValueError(f"unsupported thirdparty provider: {tp}")


async def _poll_thirdparty(kind: str, tp_task_id: str) -> "tuple[str, list[str]]":
    """调第三方适配器轮询，返回 (status, image_urls)。"""
    tp = TASK_TYPES.get(kind, {}).get("tp")
    if tp == "wavespeed":
        from .thirdparty import wavespeed as ws

        return await ws.get_status(tp_task_id)
    raise ValueError(f"unsupported thirdparty provider: {tp}")


async def get_task(request_id: str, uid: int) -> dict:
    """查询任务（轮询）。

    - sync 任务：结果已落请求日志，直接返回存储的 result（status=succeeded/failed）。
    - async 任务：转发网关查询；succeeded 时把图片产物落 BFF cloud_media 并改写 result。
      网关按 New-Api-User 隔离，越权/不存在返回 404（NewApiError）→ router 原样透传前端。
    """
    log = await cloudstore.request_log_get(request_id)
    if not log or log["uid"] != uid:
        raise NewApiError("任务不存在", 404)
    kind, mode, status = log["kind"], log["mode"], log["status"]

    if mode == "sync":
        result = log.get("result")
        return _task_view(request_id, request_id, kind, "sync", status,
                          {"result": result} if result is not None else {})

    # async：可能是网关或第三方，按 provider 分流。
    task_id = log["task_id"]
    if not task_id:
        return _task_view(request_id, None, kind, "async", status, {})

    if (log.get("provider") or "gateway") == "gateway-async-image":
        # Chatfire 风格异步生图：查询路径/终态判定与统一 tasks 端点不同。
        return await _poll_chatfire_async_image(request_id, log, uid)

    if (log.get("provider") or "gateway") == "thirdparty":
        tp_status, urls = await _poll_thirdparty(kind, task_id)
        if tp_status in ("completed", "succeeded"):
            # 分层任务：每层一个节点，label 带图层序号；其他任务 label 留空。
            images = [
                {"url": u, "label": f"图层 {i + 1}" if kind == "split-layers" else ""}
                for i, u in enumerate(urls)
            ]
            result = {"images": images}
            task = {"result": result}
            if urls:
                task = await _persist_outputs(task, task_id, uid, kind, request_id=request_id)
            await cloudstore.request_log_update(
                request_id, status="succeeded", result=task.get("result"))
            return _task_view(request_id, task_id, kind, "async", "succeeded", task)
        if tp_status in ("failed", "error", "cancelled", "timeout", "deleted"):
            await cloudstore.request_log_update(request_id, status="failed")
            return _task_view(request_id, task_id, kind, "async", "failed", {})
        return _task_view(request_id, task_id, kind, "async", tp_status or "processing", {})

    # gateway async：拉网关最新状态。
    # ⚠️ 轮询同样走 /v1，必须用用户 sk-（PAT 会 401）。
    raw = await _gw_call(
        "GET", f"{TASK_TYPES[kind]['async_path']}/{task_id}", uid, client=_proxy_client())
    task = _normalize_task(_unwrap(raw))
    gstatus = task.get("status")
    if gstatus == "succeeded":
        task = await _persist_outputs(task, task_id, uid, kind, request_id=request_id)
        await cloudstore.request_log_update(request_id, status="succeeded", result=task.get("result"))
    elif gstatus in ("failed", "error"):
        await cloudstore.request_log_update(request_id, status="failed")
    return _task_view(request_id, task_id, kind, "async", gstatus or status, task)


async def cancel_task(request_id: str, uid: int) -> dict:
    """取消任务：async 转网关 DELETE / 第三方尽力取消；sync 已即时完成，无需取消。"""
    log = await cloudstore.request_log_get(request_id)
    if not log or log["uid"] != uid:
        raise NewApiError("任务不存在", 404)
    if log["mode"] == "sync":
        raise NewApiError("同步任务已即时完成，无需取消", 400)
    task_id = log["task_id"]
    if task_id and (log.get("provider") or "gateway") == "thirdparty":
        # 第三方尽力取消（不支持则仅标记 cancelled）。
        from .thirdparty import wavespeed as ws

        try:
            await ws.cancel(task_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("第三方取消失败(忽略): %s", e)
        await cloudstore.request_log_update(request_id, status="cancelled")
        return {"cancelled": True}
    if task_id:
        # ⚠️ 2026-09-15 实测：new-api 的 video-router **未注册 DELETE 路由**
        #   （只有 POST /v1/video/generations 与 GET /v1/video/generations/:task_id；
        #     /v1/videos/:id 亦只有 GET/remix）→ DELETE 打过去是 404。
        #   取消语义在网关侧本就不支持，故此处**容忍失败**：
        #   仅记 warning，不回抛，随后照常把 BFF 侧日志标记 cancelled
        #   （前端体验为「已取消」，不会再看到 404 报错）。
        try:
            await _gw_call(
                "DELETE", f"{TASK_TYPES[log['kind']]['async_path']}/{task_id}", uid,
                client=_proxy_client())
        except Exception as e:  # noqa: BLE001 —— 网关不支持取消，尽力而为
            logger.warning("网关取消任务失败(忽略，网关可能不支持 DELETE): %s", e)
    await cloudstore.request_log_update(request_id, status="cancelled")
    return {"cancelled": True}
