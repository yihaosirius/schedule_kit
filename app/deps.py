"""FastAPI 依赖：凭据解析与权限判定。

两种凭据（PLAN.md §7）：

* **会话 Cookie**（人，来自浏览器）：无状态 HMAC 签名；写操作需带 CSRF 头。
* **API Key**（机器：快捷指令 / 悬浮窗 / 未来的 agent）：``Authorization: Bearer``
  或 ``X-API-Key``；只存 SHA-256；只有一个 ``read_only`` 布尔，没有多级作用域。

资源归属只有一个主人，所以"是否是管理员"等价于"是否是会话"——
``/api/settings`` 与 ``/api/keys`` 只接受会话，API Key 一律拒绝。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.logging import get_logger, kv
from app.security import (
    API_KEY_PREFIX,
    CSRF_HEADER,
    SESSION_COOKIE,
    hash_api_key,
    verify_csrf,
    verify_session,
)

log = get_logger("auth")


@dataclass(frozen=True)
class AuthContext:
    """一次请求的认证结果。"""

    kind: str  # "session" | "apikey"
    read_only: bool
    key_id: int | None = None
    key_name: str | None = None

    @property
    def is_session(self) -> bool:
        return self.kind == "session"

    def describe(self) -> str:
        if self.is_session:
            return "session"
        return f"apikey:{self.key_name}{'(read-only)' if self.read_only else ''}"


def _extract_api_key(request: Request) -> str | None:
    header = request.headers.get("Authorization")
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
        # 有些客户端会直接把 key 放进 Authorization，不带 Bearer 前缀
        if not value and header.strip().startswith(API_KEY_PREFIX):
            return header.strip()
    direct = request.headers.get("X-API-Key")
    if direct and direct.strip():
        return direct.strip()
    return None


def _lookup_key(db, raw_key: str) -> sqlite3.Row | None:
    digest = hash_api_key(raw_key)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, name, read_only, revoked_at FROM api_keys WHERE key_hash = ?",
            (digest,),
        ).fetchone()
    if row is None or row["revoked_at"]:
        return None
    return row


def authenticate(request: Request) -> AuthContext | None:
    """解析凭据；无法认证时返回 ``None``。"""
    cfg = request.app.state.config
    db = request.app.state.db

    raw_key = _extract_api_key(request)
    if raw_key:
        row = _lookup_key(db, raw_key)
        if row is not None:
            _touch_key(db, row["id"])
            return AuthContext(
                kind="apikey",
                read_only=bool(row["read_only"]),
                key_id=int(row["id"]),
                key_name=str(row["name"]),
            )
        log.warning("auth.apikey_rejected %s", kv(reason="unknown-or-revoked"))
        # 带上 key 的凭据却无效时，不再回退到 Cookie，避免掩盖客户端配置错误
        return None

    token = request.cookies.get(SESSION_COOKIE)
    if token and verify_session(cfg.auth.secret_key, token, cfg.auth.session_epoch):
        return AuthContext(kind="session", read_only=False)
    return None


def _touch_key(db, key_id: int) -> None:
    """记录最后使用时间。失败不影响请求。"""
    try:
        from app.timeutil import utc_iso

        with db.transaction() as conn:
            conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (utc_iso(), key_id))
    except sqlite3.Error as exc:  # pragma: no cover - 仅在库损坏时触发
        log.warning("auth.touch_failed %s", kv(key_id=key_id, error=str(exc)))


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_auth(request: Request) -> AuthContext:
    """要求已认证（读操作）。"""
    context = authenticate(request)
    if context is None:
        raise _unauthorized("缺少或无效的凭据")
    request.state.auth_kind = context.describe()
    return context


def require_write(request: Request) -> AuthContext:
    """要求可写：会话需带 CSRF 头，API Key 不能是只读。"""
    context = current_auth(request)
    if context.read_only:
        log.warning("auth.write_denied %s", kv(reason="read-only-key", key=context.key_name))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="该密钥为只读，不能执行写操作")
    _verify_csrf_for_session(request, context)
    return context


def require_session(request: Request) -> AuthContext:
    """要求浏览器会话（控制台用）。API Key 一律拒绝。"""
    context = current_auth(request)
    if not context.is_session:
        log.warning("auth.session_required %s", kv(kind=context.kind, key=context.key_name))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="该操作仅限网页会话")
    return context


def require_session_write(request: Request) -> AuthContext:
    """要求浏览器会话 + CSRF。"""
    context = require_session(request)
    _verify_csrf_for_session(request, context)
    return context


def _verify_csrf_for_session(request: Request, context: AuthContext) -> None:
    """API Key 认证天然不受 CSRF 影响；只有 Cookie 认证需要双提交校验。"""
    if not context.is_session:
        return
    cfg = request.app.state.config
    provided = request.headers.get(CSRF_HEADER)
    if not verify_csrf(cfg.auth.secret_key, provided):
        log.warning("auth.csrf_rejected %s", kv(path=request.url.path, header_present=bool(provided)))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"缺少或错误的 {CSRF_HEADER}")


AuthDep = Annotated[AuthContext, Depends(current_auth)]
WriteAuthDep = Annotated[AuthContext, Depends(require_write)]
SessionDep = Annotated[AuthContext, Depends(require_session)]
SessionWriteDep = Annotated[AuthContext, Depends(require_session_write)]
