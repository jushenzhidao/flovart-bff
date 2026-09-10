"""图片模型「同步 / 异步」配置（按模型名）—— 生图请求方式的单一真相源。

- 存储：``data/image_model_modes.json`` —— ``{model_name: "sync" | "async"}``
- 原子写（store.update_json，tmp + os.replace + fsync）。
- 单 worker 部署哲学；多副本需换共享存储（见 ARCHITECTURE.md §8）。
- image-gen 提交时由 app/tasks.py 按 params.model 查此项覆盖全局
  GATEWAY_IMAGE_GEN_MODE：sync=阻塞直出 / async=POST 提交 + GET 轮询。
"""
import threading
from typing import Optional

from . import config, store

_PATH: Optional[str] = None
_lock = threading.Lock()

_IMAGE_KEYS = (
    "seedream", "gpt-image", "gpt-image-1", "flux", "doubao-seed",
    "stable-diffusion", "sd-", "sd3", "imagen", "midjourney", "wan",
    "kolors", "cogview", "dall", "gemini",
    "layer", "upscale", "remove-bg", "matting",
)


def _path() -> str:
    global _PATH
    if _PATH is None:
        _PATH = f"{config.DATA_DIR}/image_model_modes.json"
    return _PATH


def get_all() -> dict:
    """返回全部已配置模型 -> 模式 的映射。"""
    return store.load_json(_path(), {})


def get_mode(model: str) -> Optional[str]:
    """查某模型的配置模式；未配置返回 None（调用方回退 GATEWAY_IMAGE_GEN_MODE）。"""
    return get_all().get(model)


def set_mode(model: str, mode: str) -> dict:
    """设置某模型的同步/异步模式，返回全量映射。"""
    if mode not in ("sync", "async"):
        raise ValueError("mode must be 'sync' or 'async'")
    if not model:
        raise ValueError("model is required")

    def fn(data: dict) -> dict:
        data[model] = mode
        return data

    return store.update_json(_path(), {}, fn)


def delete_mode(model: str) -> dict:
    """删除某模型的显式配置（回退全局默认）。"""

    def fn(data: dict) -> dict:
        data.pop(model, None)
        return data

    return store.update_json(_path(), {}, fn)


def is_image_model(name: str) -> bool:
    """启发式判断模型名是否为图片类（含分层/放大/去背景）。"""
    n = (name or "").lower()
    return any(k in n for k in _IMAGE_KEYS)
