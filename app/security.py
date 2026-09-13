"""鉴权原语：scrypt 密码、无状态签名会话、CSRF、API Key。

设计要点（PLAN.md §7）：

* 会话是**无状态**的（HMAC 签名，不落库）。撤销手段是 ``session_epoch``：
  改密码时自增，所有既有 Cookie 立即失效。
* API Key 只存 SHA-256，明文仅在创建时返回一次。
* 所有比较走 :func:`hmac.compare_digest`，避免时序侧信道。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32

SESSION_COOKIE = "sk_session"
CSRF_COOKIE = "sk_csrf"
CSRF_HEADER = "X-CSRF-Token"
API_KEY_PREFIX = "sk_"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


# --------------------------------------------------------------------------- #
# 密码
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    """生成 ``scrypt$n$r$p$salt$hash`` 形式的密码哈希。"""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_BYTES
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码。任何格式异常都返回 False 而不是抛错。"""
    if not stored:
        return False
    try:
        scheme, n_raw, r_raw, p_raw, salt_raw, hash_raw = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = _b64d(hash_raw)
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_b64d(salt_raw),
            n=int(n_raw),
            r=int(r_raw),
            p=int(p_raw),
            dklen=len(expected),
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(candidate, expected)


# --------------------------------------------------------------------------- #
# 会话（无状态签名 Cookie）
# --------------------------------------------------------------------------- #
def _session_payload(expiry: int, epoch: int) -> bytes:
    return f"{expiry}.{epoch}".encode("ascii")


def sign_session(secret_key: str, ttl_days: int, epoch: int, *, now: float | None = None) -> str:
    """生成 ``<expiry>.<epoch>.<sig>`` 形式的会话令牌。"""
    expiry = int((now if now is not None else time.time()) + ttl_days * 86400)
    payload = _session_payload(expiry, epoch)
    signature = hmac.new(secret_key.encode("utf-8"), payload, hashlib.sha256).digest()
    return f"{expiry}.{epoch}.{_b64e(signature)}"


def verify_session(secret_key: str, token: str, current_epoch: int, *, now: float | None = None) -> bool:
    """校验会话令牌的签名、有效期与 epoch。"""
    if not secret_key or not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    expiry_raw, epoch_raw, signature_raw = parts
    try:
        expiry = int(expiry_raw)
        epoch = int(epoch_raw)
    except ValueError:
        return False
    if epoch != current_epoch:
        return False
    if expiry < int(now if now is not None else time.time()):
        return False
    expected = hmac.new(
        secret_key.encode("utf-8"), _session_payload(expiry, epoch), hashlib.sha256
    ).digest()
    try:
        provided = _b64d(signature_raw)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, provided)


# --------------------------------------------------------------------------- #
# CSRF（双提交：Cookie + 请求头，值由密钥派生，无需存储）
# --------------------------------------------------------------------------- #
def csrf_token(secret_key: str) -> str:
    return hmac.new(secret_key.encode("utf-8"), b"csrf", hashlib.sha256).hexdigest()


def verify_csrf(secret_key: str, provided: str | None) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(csrf_token(secret_key), provided)


# --------------------------------------------------------------------------- #
# API Key
# --------------------------------------------------------------------------- #
def generate_api_key() -> str:
    """生成明文 API Key（仅创建时返回一次）。"""
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def api_key_hint(key: str) -> str:
    """用于列表展示的不可逆提示串。"""
    return key[:11] + "…" if len(key) > 11 else key
