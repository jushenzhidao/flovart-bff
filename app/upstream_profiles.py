"""上游能力档案 —— flovart 能力 ↔ 上游（baidu/wenxin）模型的映射与参数适配。

背景（baidu-wenxin 接入 flovart 实施规格书 v1.0 · 2026-09-24）：
  baidu.1task.cn 的 16 个 wenxin 模型全是「图进图出」的图片编辑能力。与 chatfire
  一样，上游只认 `POST v1/images/generations`（OpenAI 形态），因此这里与
  GATEWAY_SYNC_*_PATH 统一路径的设计同构：**路径不变，靠 body 语义字段区分能力**。

设计原则（照抄 tasks.resolve_split_model 的范式）：
  · 真实上游模型名只在本模块出现（PROFILES.model），前端只发语义值（task kind）；
  · 本模块是**唯一映射点**，路由层与提交层共用，避免两处判断漂移；
  · 纯函数无 IO（除图片压缩），便于单测（tests/test_upstream_profiles.py）。

零影响红线（规格书 §5.3）：
  开关（env `PROFILE_<KIND_UPPER>=off|baidu`）默认全 `off`；off / 未注册档案时
  `adapt()` **原样返回 params**（同一对象），既有链路行为逐字节等价。
  只有显式 `PROFILE_XXX=baidu` 才启用适配。

结构错配的三个解法（规格书 §1）：
  1. 能力→上游模型映射：PROFILES（flovart 7 个编辑 type 全打同一路径、只靠
     variant/task 区分，new-api 是忠实转发器不会看这些语义 → 必须在 BFF 把
     params.model 换成真实 `wenxin:*`）；
  2. 参数归一化：flovart 的 image/mask 可能是 `{data,mimeType}` 对象 / dataURI /
     裸 base64 / URL / list，baidu 只收字符串 → to_data_uri()（实测对象形态必
     400 missing_input）；
  3. 预压缩：走百度老接口的能力硬限 640px/40KB → shrink_for_legacy()（实测
     1024²/80KB 连续失败曾被误判「IP 被封」，压缩后直接出图）。

上游契约速查（/capabilities 实时拉取，以实测为准）：
  · 响应：{created, model, data:[{mime, b64_json, size}]} —— BFF _normalize_sync_result
    已兼容（data[] → images），**零改动**；
  · image 字段三形态：dataURI / http(s) URL / 裸 base64；
  · mask：黑底白框，白色=要处理的区域，尺寸须与原图一致（⚠️ 待逐像素验证，
    见规格书 §6.4 —— 纯白/纯黑双测，若语义相反需在 BFF 反相）；
  · size（仅 expand）：比例 1:1/4:3/3:4/16:9/9:16/3:2/2:3；
  · style（仅 restyle）：17 项枚举，id 或中文标签等价，表由上游服务端下发勿硬编码；
  · 错误：{error:{code,...}}；code=missing_input 参数层拒绝（零消耗可自检）。
"""
import base64
import re
import threading
from dataclasses import dataclass
from io import BytesIO
from typing import Callable

from PIL import Image

from . import config, store


# ---------------------------------------------------------------------------
# 参数归一化（规格书 §6.1）—— 所有能力通用
# ---------------------------------------------------------------------------
def to_data_uri(value) -> "str | None":
    """把 flovart 的 image/mask 字段统一成上游接受的字符串形态。

    接受的输入：
      · str 且以 http(s):// 开头     → 原样（上游接受 URL）
      · str 且以 data: 开头          → 原样
      · str 其它（裸 base64）        → 补 data:image/png;base64, 前缀
      · list                         → 取第一个非空元素递归处理
      · dict {data, mimeType}        → f"data:{mimeType};base64,{data}"
                                       （data 本身已是 dataURI 时直接用）
      · dict {url}                   → 取 url
    无法识别 → None（调用方给清晰错误，不静默发空值 —— 实测空 image 必 400）。
    """
    if value is None:
        return None
    if isinstance(value, list):
        for item in value:
            got = to_data_uri(item)
            if got:
                return got
        return None
    if isinstance(value, dict):
        if isinstance(value.get("data"), str) and value["data"].strip():
            data = value["data"].strip()
            if data.startswith("data:"):
                return data
            mime = value.get("mimeType") or "image/png"
            return f"data:{mime};base64,{data}"
        if isinstance(value.get("url"), str) and value["url"].strip():
            return value["url"].strip()
        return None
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        if v.startswith(("http://", "https://", "data:")):
            return v
        return f"data:image/png;base64,{v}"
    return None


_DATA_URI_RE = re.compile(r"^data:([^;,]+);base64,(.*)$", re.S)


def shrink_for_legacy(data_uri: str, max_side: int = 640, max_bytes: int = 40000) -> str:
    """把 dataURI 压进百度老接口硬限（/readyz.checks.legacy_limits）。

    · 长边缩放到 <= max_side（保持比例，LANCZOS）；
    · 仍超 max_bytes 则逐级降 JPEG 质量（95 → 85 → 75 → 65），必要时再缩边；
    · 返回 data:image/jpeg;base64,... 。压不进限制抛 ValueError（不静默发超大图）。
    · 实测教训：1024²/80KB 连续失败曾被误判「出口 IP 被封」，压缩后 600×400/11.4KB 直接出图。
    """
    m = _DATA_URI_RE.match(data_uri or "")
    if not m:
        # URL 无法本地压缩（下载再压超出本模块纯函数边界），直接放行给上游报错
        if (data_uri or "").startswith(("http://", "https://")):
            return data_uri
        raise ValueError("老接口压缩仅支持 base64 dataURI 输入")
    raw = base64.b64decode(m.group(2))
    img = Image.open(BytesIO(raw))
    img.load()
    if img.mode in ("RGBA", "LA", "P", "PA"):
        img = img.convert("RGB")
    w, h = img.size
    scale = min(1.0, max_side / float(max(w, h)))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = BytesIO()
    for q in (95, 85, 75, 65):
        buf = BytesIO()
        img.save(buf, "JPEG", quality=q)
        if buf.tell() <= max_bytes:
            break
    while buf.tell() > max_bytes and max(img.size) > 64:
        w2, h2 = img.size
        img = img.resize((max(1, int(w2 * 0.8)), max(1, int(h2 * 0.8))), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, "JPEG", quality=65)
    if buf.tell() > max_bytes:
        raise ValueError("图片无法压缩到老接口限制内（640px/40KB），请换更小的图")
    return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"


# ---------------------------------------------------------------------------
# 能力档案
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CapabilityProfile:
    kind: str                       # flovart task type
    model: str                      # 上游模型名（唯一真源）
    legacy: bool                    # 走百度老接口（640px/40KB 硬限）→ 需预压缩
    build: Callable[[dict], dict]   # params -> 上游 body（只含上游白名单字段）
    requires: tuple[str, ...]       # 必需输入字段（缺失时给清晰错误，不静默发空值）


def _norm_in(params: dict, field: str) -> str:
    """取并归一化必需输入；缺失抛清晰 ValueError（上游 400 缺乏上下文难排障）。"""
    got = to_data_uri(params.get(field))
    if not got:
        raise ValueError(f"wenxin 适配：缺少输入 {field}（或字段形态无法识别）")
    return got


def _legacy(params: dict, field: str) -> str:
    """取输入并按需压缩到老接口限制（仅 legacy 能力调用）。"""
    return shrink_for_legacy(_norm_in(params, field))


def _build_simple(model: str, legacy: bool, *fields: str) -> Callable[[dict], dict]:
    """通用 build：image(/mask) 归一化（+压缩）+ prompt 透传，输出上游白名单字段。"""
    def build(params: dict) -> dict:
        body: dict = {"model": model}
        for f in fields:
            if f == "prompt":
                if str(params.get("prompt") or "").strip():
                    body["prompt"] = str(params["prompt"])
                continue
            body[f] = _legacy(params, f) if legacy else _norm_in(params, f)
        return body
    return build


def _build_outpaint(params: dict) -> dict:
    """outpaint → wenxin:expand：补 size 比例（上游把 size 解释为比例，7 个合法值）。

    方向信息目前在前端 params.direction（无则兜底 1:1 四周扩）。上游不解析 prompt。
    ⚠️ 主链状态存疑（规格书 §6.3：verified=true 但 2026-09-24 实测主链曾只回编辑器
    链接）—— 开此开关前必须先跑一次真实出图验收（§9.2）。
    """
    body = {
        "model": "wenxin:expand",
        "image": _norm_in(params, "image"),
        "size": _DIRECTION_TO_RATIO.get(
            str(params.get("direction") or "").strip().lower(), "1:1"),
    }
    if str(params.get("prompt") or "").strip():
        body["prompt"] = str(params["prompt"])
    return body


def _build_restyle(params: dict) -> dict:
    """restyle：不认 prompt，只认 style（17 项枚举，id/中文标签等价）。

    风格表由上游 /capabilities 的 style_table 服务端下发，**不要在前端硬编码**。
    """
    style = str(params.get("style") or "").strip()
    if not style:
        raise ValueError("wenxin:restyle 需要显式指定 style（17 项枚举，见上游 /capabilities）")
    return {"model": "wenxin:restyle", "image": _norm_in(params, "image"), "style": style}


# expand 的合法比例集合（上游实测，非法值会被拒）
_DIRECTION_TO_RATIO = {
    "left": "4:3", "right": "4:3",
    "top": "3:4", "bottom": "3:4",
    "expand": "1:1",   # 兜底：四周扩
}


# A 组：替换现有能力的后端（规格书 §4.1）。⚠️ annotate / relight 无语义等价能力，
# edit 的 prompt→style 无法自动映射 —— 三者**不建档案**，保持现状。
# mask→replace 的蒙版语义「名义一致但必须逐像素验证」（§6.4），开关默认 off。
PROFILES: "dict[str, CapabilityProfile]" = {
    # ---- A 组（替换后端） ----
    "remove-background": CapabilityProfile(
        kind="remove-background", model="wenxin:matting", legacy=False,
        build=_build_simple("wenxin:matting", False, "image"), requires=("image",)),
    "upscale": CapabilityProfile(
        # ⚠️ 有损：固定 4×（无法按需指定 targetLongEdge）、输出 JPEG、重绘式放大。
        # 忽略 options；接之前必须先做真实质量对比，不达标就不切（规格书 §6.2）。
        kind="upscale", model="wenxin:clarity", legacy=False,
        build=_build_simple("wenxin:clarity", False, "image"), requires=("image",)),
    "outpaint": CapabilityProfile(
        kind="outpaint", model="wenxin:expand", legacy=False,
        build=_build_outpaint, requires=("image",)),
    "mask": CapabilityProfile(
        kind="mask", model="wenxin:replace", legacy=True,  # 主链不支持遮罩 ⇒ 上游经老接口执行
        build=_build_simple("wenxin:replace", True, "image", "mask", "prompt"),
        requires=("image", "mask")),
    # ---- B 组（新增独立能力入口，规格书 §4.2） ----
    "dewatermark": CapabilityProfile(
        kind="dewatermark", model="wenxin:dewatermark", legacy=True,
        build=_build_simple("wenxin:dewatermark", True, "image"), requires=("image",)),
    "removetext": CapabilityProfile(
        kind="removetext", model="wenxin:removetext", legacy=False,
        build=_build_simple("wenxin:removetext", False, "image"), requires=("image",)),
    "erase": CapabilityProfile(
        kind="erase", model="wenxin:erase", legacy=True,  # 主链不支持遮罩 ⇒ 经老接口执行
        build=_build_simple("wenxin:erase", True, "image", "mask"), requires=("image", "mask")),
    "beauty": CapabilityProfile(
        kind="beauty", model="wenxin:beauty", legacy=False,
        build=_build_simple("wenxin:beauty", False, "image"), requires=("image",)),
    "filter": CapabilityProfile(
        kind="filter", model="wenxin:filter", legacy=False,
        build=_build_simple("wenxin:filter", False, "image"), requires=("image",)),
    "restore": CapabilityProfile(
        kind="restore", model="wenxin:restore", legacy=False,
        build=_build_simple("wenxin:restore", False, "image"), requires=("image",)),
    "sketch": CapabilityProfile(
        kind="sketch", model="wenxin:sketch", legacy=False,  # 全表最慢 20–25s，超时留足
        build=_build_simple("wenxin:sketch", False, "image"), requires=("image",)),
    "textreplace": CapabilityProfile(
        kind="textreplace", model="wenxin:textreplace", legacy=False,
        build=_build_simple("wenxin:textreplace", False, "image"), requires=("image",)),
    "bgreplace": CapabilityProfile(
        kind="bgreplace", model="wenxin:bgreplace", legacy=True,
        build=_build_simple("wenxin:bgreplace", True, "image", "mask", "prompt"),
        requires=("image", "mask", "prompt")),
    "redraw": CapabilityProfile(
        kind="redraw", model="wenxin:redraw", legacy=True,  # legacy-only
        build=_build_simple("wenxin:redraw", True, "image"), requires=("image",)),
    "similar": CapabilityProfile(
        kind="similar", model="wenxin:similar", legacy=True,  # legacy-only
        build=_build_simple("wenxin:similar", True, "image"), requires=("image",)),
    # restyle（显式选风格版换风格）走独立入口，不接受自由 prompt 硬凑
    "restyle": CapabilityProfile(
        kind="restyle", model="wenxin:restyle", legacy=False,
        build=_build_restyle, requires=("image", "style")),
}

# 明确不接（上游已转「跳转交互式编辑器」形态，18 次调用全部只回编辑器链接，非临时故障）：
#   wenxin:matting-pro / wenxin:ps / wenxin:removeperson
# 不做自动重试（上游限速 4s/次、8次/分，撞风控冷却 10min；铁律：失败一次即停）


def profile_for(kind: str) -> "CapabilityProfile | None":
    """查该能力的档案；无档案返回 None（= 保持现状，不做任何改动）。"""
    return PROFILES.get(kind)


# ---------------------------------------------------------------------------
# 开关存储：双层 —— 管理台覆盖（data/upstream_profiles.json）> env 默认。
# 2026-09-24：从 env-only 改为运行时可配（管理台 /api/console/upstream-profiles），
# 沿用 image_model_modes.json 的原子写范式（store.update_json，单 worker 哲学）。
# env `PROFILE_<KIND_UPPER>` 仅作为初始默认值；管理台改过一次后以文件为准。
# ---------------------------------------------------------------------------
_OVERRIDES_PATH = None
_lock = threading.Lock()


def _overrides_path() -> str:
    global _OVERRIDES_PATH
    if _OVERRIDES_PATH is None:
        _OVERRIDES_PATH = f"{config.DATA_DIR}/upstream_profiles.json"
    return _OVERRIDES_PATH


def get_overrides() -> dict:
    """管理台覆盖层：{kind: "baidu" | "off"}，未覆盖的 kind 不在表里。"""
    return store.load_json(_overrides_path(), {})


def _raw(kind: str) -> str:
    """解析某能力当前生效值：覆盖层 > env 默认。"""
    overrides = get_overrides()
    if kind in overrides:
        return str(overrides[kind])
    return config.UPSTREAM_PROFILES.get(kind, "off")


def is_enabled(kind: str) -> bool:
    """该能力的上游开关是否打开（管理台覆盖 > env PROFILE_<KIND_UPPER>；默认 off）。"""
    return _raw(kind) == "baidu"


def set_override(kind: str, value: str) -> dict:
    """管理台设置开关（"baidu"|"off"），返回全量覆盖表。非法 kind 拒绝。"""
    if kind not in PROFILES:
        raise ValueError(f"unknown capability kind: {kind}")
    if value not in ("baidu", "off"):
        raise ValueError("value must be 'baidu' or 'off'")
    with _lock:

        def fn(data: dict) -> dict:
            data[kind] = value
            return data

        return store.update_json(_overrides_path(), {}, fn)


def capability_views() -> list:
    """管理台列表视图：全部已注册能力 + 生效状态 + 元信息（中文说明/是否 legacy）。"""
    labels = {
        "remove-background": "抠图/去背景", "upscale": "高清放大(固定4×)",
        "outpaint": "扩展画面", "mask": "局部替换",
        "dewatermark": "去水印", "removetext": "去文字", "erase": "消除",
        "beauty": "人像美颜", "filter": "风格滤镜", "restore": "老照片修复",
        "sketch": "提取线稿(最慢20-25s)", "textreplace": "文字替换",
        "bgreplace": "背景替换", "redraw": "AI重绘", "similar": "相似图生成",
        "restyle": "换风格",
    }
    return [
        {
            "kind": p.kind,
            "label": labels.get(p.kind, p.kind),
            "model": p.model,
            "legacy": bool(p.legacy),
            "requires": list(p.requires),
            "enabled": is_enabled(p.kind),
        }
        for p in PROFILES.values()
    ]


def adapt(kind: str, params: dict) -> dict:
    """把 flovart params 适配成上游可接受的 body。

    未命中档案 / 开关关闭 → **原样返回 params**（同一对象，逐字节等价 —— 回归红线）。
    开关打开 → 生成仅含上游白名单字段的新 body（model 换成真实 wenxin:*，
    多余参数如 mode/options 留在原 params、不外发，避免上游拒收）。
    """
    profile = PROFILES.get(kind)
    if profile is None or not is_enabled(kind):
        return params
    return profile.build(params)
