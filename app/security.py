"""加密 Cookie 会话（无状态 BFF）。

会话载荷：{"uid": int, "username": str, "pat": str, "role": int}
pat = new-api 的 Personal Access Token（长期有效）。

## 为什么是加密而不只是签名

早期版本用 itsdangerous 的 URLSafeTimedSerializer，它只做**签名**不做**加密**
—— 载荷是 base64 明文 JSON，任何能读到 Cookie 字符串的人无需密钥即可解出
其中的 PAT。现在改为 AES-256-GCM：密文 + 认证标签，兼具机密性与完整性，
GCM tag 校验同时替代了签名提供的防篡改能力。

## 密钥派生

SECRET_KEY 经 HKDF-SHA256 派生 32 字节 AES 密钥，info 做域分离，避免同一
SECRET_KEY 在未来别处复用时产生密钥重合。不缓存派生结果：测试会 monkeypatch
SECRET_KEY，缓存会让改写不生效。

## 兼容性

不兼容旧格式 Cookie —— 解析失败即返回 None（表现为 401 重新登录）。
"""
import base64
import json
import os
import secrets
import time
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import HTTPException, Request, Response

from . import config

# 版本前缀：将来换算法/换派生方式靠它区分格式，而不是靠解析失败去猜。
_SCHEME = b"v2"
_NONCE_LEN = 12  # GCM 推荐 96-bit nonce，每次加密必须重新随机生成


def _aes_key() -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"bff-session-aead-v2",
    ).derive(config.SECRET_KEY.encode("utf-8"))


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded)


def _encrypt(payload: dict) -> str:
    # 签发时间随载荷一起加密，服务端判过期 —— 不靠浏览器执行 Cookie max_age，
    # 否则攻击者拿到 Cookie 值后可以无限期重放。
    body = dict(payload)
    body["iat"] = int(time.time())
    plaintext = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    nonce = os.urandom(_NONCE_LEN)
    # nonce 作为 AAD 一并认证，防止密文与 nonce 被拆开重组
    ciphertext = AESGCM(_aes_key()).encrypt(nonce, plaintext, _SCHEME)
    return f"{_SCHEME.decode()}.{_b64e(nonce)}.{_b64e(ciphertext)}"


def _decrypt(token: str) -> Optional[dict]:
    try:
        scheme, nonce_b64, ct_b64 = token.split(".", 2)
    except ValueError:
        return None
    if scheme.encode() != _SCHEME:
        return None
    try:
        plaintext = AESGCM(_aes_key()).decrypt(_b64d(nonce_b64), _b64d(ct_b64), _SCHEME)
        body = json.loads(plaintext)
    except (InvalidTag, ValueError, TypeError, json.JSONDecodeError):
        # InvalidTag 覆盖篡改/错密钥/错 nonce；其余是畸形 base64/非 JSON。
        # 一律按无效会话处理，不区分原因，避免泄露「密钥错 vs 被篡改」。
        return None
    if not isinstance(body, dict):
        return None
    iat = body.pop("iat", None)
    if not isinstance(iat, int) or time.time() - iat > config.COOKIE_MAX_AGE:
        return None
    # 载荷结构校验：缺字段的会话在业务层会炸 KeyError（500），
    # 不如在这里判定未登录（401）。
    if not all(k in body for k in ("uid", "username", "pat")):
        return None
    return body


def set_session(response: Response, payload: dict) -> None:
    response.set_cookie(
        key=config.COOKIE_NAME,
        value=_encrypt(payload),
        max_age=config.COOKIE_MAX_AGE,
        httponly=True,
        samesite=config.COOKIE_SAMESITE,
        secure=config.COOKIE_SECURE,
        path="/",
    )


def clear_session(response: Response) -> None:
    # 属性需与 set_session 一致：浏览器按 name+path+domain 匹配删除，
    # 属性不一致会被当成另一条 Cookie，导致旧会话残留、登出失效。
    response.delete_cookie(
        config.COOKIE_NAME,
        path="/",
        httponly=True,
        samesite=config.COOKIE_SAMESITE,
        secure=config.COOKIE_SECURE,
    )


def read_session(request: Request) -> Optional[dict]:
    token = request.cookies.get(config.COOKIE_NAME)
    if not token:
        return None
    return _decrypt(token)


def require_session(request: Request) -> dict:
    session = read_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return session


# new-api 的角色常量（common/constants.go）：普通 1 / 管理员 10 / root 100。
ROLE_ADMIN = 10


def is_admin(session: dict) -> bool:
    """上游 role >= 10，或在静态名单（BFF_ADMIN_USERNAMES）内。

    role 缺失按**非管理员**处理 —— 默认放行会让存量会话瞬间获得管理权限。
    名单是兜底通道（上游不返回 role 的实例），见 config.ADMIN_USERNAMES。
    """
    role = session.get("role")
    if isinstance(role, int) and not isinstance(role, bool) and role >= ROLE_ADMIN:
        return True
    username = session.get("username")
    return bool(username) and username in config.ADMIN_USERNAMES


def require_admin(request: Request) -> dict:
    """管理员依赖：未登录 401；已登录非管理员 403。"""
    session = require_session(request)
    if not is_admin(session):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return session


# ==================== 自包含短时效 ticket（复用同款加密设施）====================
# 与会话独立：10 分钟、链接持有、内编码 uid+pat，导出类功能无需服务端存储。
_SETUP_SCHEME = b"v2t"
_SETUP_TTL = 10 * 60


def issue_setup_ticket(uid: int, pat: str, ttl: int = _SETUP_TTL) -> str:
    body = {
        "uid": uid,
        "pat": pat,
        "exp": int(time.time()) + ttl,
        "jti": secrets.token_hex(8),
    }
    plaintext = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(_aes_key()).encrypt(nonce, plaintext, _SETUP_SCHEME)
    return f"{_SETUP_SCHEME.decode()}.{_b64e(nonce)}.{_b64e(ciphertext)}"


def open_setup_ticket(token: str) -> Optional[dict]:
    """解 ticket，成功返回 {uid, pat}；失败（篡改/过期/畸形）返回 None。"""
    if not isinstance(token, str):
        return None
    try:
        scheme, nonce_b64, ct_b64 = token.split(".", 2)
    except ValueError:
        return None
    if scheme.encode() != _SETUP_SCHEME:
        return None
    try:
        plaintext = AESGCM(_aes_key()).decrypt(_b64d(nonce_b64), _b64d(ct_b64), _SETUP_SCHEME)
        body = json.loads(plaintext)
    except (InvalidTag, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    exp = body.get("exp")
    if not isinstance(exp, int) or time.time() > exp:
        return None
    if not all(k in body for k in ("uid", "pat")):
        return None
    return {"uid": body["uid"], "pat": body["pat"]}


# ==================== 任意密钥的静态加密（落盘用）====================
# 与会话 Cookie 同款 AES-256-GCM，但不带过期/签发时间，仅做机密性 +
# 完整性保护。用于把用户级凭证（如 new-api 的 API Key sk-）加密后落到本地
# JSON 存储，避免明文落盘等价口令（sk- 一旦泄露即可冒用该用户配额）。

def encrypt_secret(plain: str) -> str:
    """加密任意字符串（如 sk- Key），返回可安全落盘的 token 串。"""
    if not isinstance(plain, str) or not plain:
        raise ValueError("encrypt_secret: empty plain")
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(_aes_key()).encrypt(nonce, plain.encode("utf-8"), _SCHEME)
    return f"{_SCHEME.decode()}.{_b64e(nonce)}.{_b64e(ct)}"


def decrypt_secret(token: str) -> Optional[str]:
    """解密 encrypt_secret 的产物；篡改/畸形/密钥错/非字符串返回 None。"""
    if not isinstance(token, str):
        return None
    try:
        scheme, nonce_b64, ct_b64 = token.split(".", 2)
    except ValueError:
        return None
    if scheme.encode() != _SCHEME:
        return None
    try:
        return AESGCM(_aes_key()).decrypt(_b64d(nonce_b64), _b64d(ct_b64), _SCHEME).decode("utf-8")
    except (InvalidTag, ValueError, TypeError):
        return None
