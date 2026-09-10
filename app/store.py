"""JSON 文件持久化通用件（BFF 无 DB，单 worker 部署哲学，同 hewapi-bff）。

- 原子写：tmp + os.replace，避免半截文件；
- 进程内锁：单进程内并发安全（单 worker uvicorn 足够）；
- 多副本部署时状态文件不共享 —— 上量后须换 Redis/DB（见 ARCHITECTURE.md §8）。
"""
import json
import logging
import os
import threading
from typing import Any, Callable

logger = logging.getLogger("bff.store")

_lock = threading.Lock()


def ensure_data_dir() -> str:
    """确保 data 目录存在（幂等），返回目录路径。"""
    from . import config

    os.makedirs(config.DATA_DIR, exist_ok=True)
    return config.DATA_DIR


def load_json(path: str, default: Any) -> Any:
    """读 JSON；文件不存在或损坏时返回 default（不抛错）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json_atomic(path: str, obj: Any) -> None:
    """原子写 JSON：写临时文件后 os.replace。失败抛 OSError，调用方决定影响。"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def update_json(path: str, default: Any, fn: Callable[[Any], Any]) -> Any:
    """锁内读-改-写。fn 接收当前值（不存在时为 default），返回新值。"""
    with _lock:
        data = load_json(path, default)
        new_data = fn(data)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        save_json_atomic(path, new_data)
        return new_data
