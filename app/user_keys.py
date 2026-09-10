"""每用户 new-api API Key（sk-）的持久化存储。

为什么需要它：
- /v1/chat/completions 只认 sk-，不认 PAT；BFF 代用户发放 sk- 后必须自己保存，
  否则用户每次聊天都要重新发放（且明文 key 网关只返回一次）。
- 存放的 sk- 等同该用户的调用凭证，必须加密落盘（复用 security.encrypt_secret
  的 AES-256-GCM），文件权限 0o600。
- 单 worker 内存即可，但进程重启后需复用，故落本地 JSON（多副本请换 Redis/PG，
  与全局存储策略一致）。读取走内存无锁（dict 读安全）；写串行化避免并发写损坏。

Key 归属：sk- 属于该 new-api 用户，聊天计费落到该用户配额，与管理员账隔离。
"""
import json
import os
import threading
from typing import Optional

from . import config
from .security import decrypt_secret, encrypt_secret

_FILE = os.path.join(config.DATA_DIR, "user_keys.json")
_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    os.makedirs(os.path.dirname(_FILE), exist_ok=True)
    tmp = _FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, _FILE)
    try:
        os.chmod(_FILE, 0o600)
    except OSError:
        pass


def get_key(uid: int) -> Optional[str]:
    """返回该用户的明文 sk-（若无则为 None）。密文损坏/密钥错返回 None。"""
    enc = _load().get(str(uid))
    if not enc:
        return None
    return decrypt_secret(enc)


def set_key(uid: int, key: str) -> None:
    """保存（覆盖）该用户的 sk-，加密落盘。"""
    with _lock:
        d = _load()
        d[str(uid)] = encrypt_secret(key)
        _save(d)


def delete_key(uid: int) -> None:
    """删除该用户的 sk-（轮换或用户在前端撤销 token 时调用）。"""
    with _lock:
        d = _load()
        if str(uid) in d:
            del d[str(uid)]
            _save(d)
