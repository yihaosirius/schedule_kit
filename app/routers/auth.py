"""登录 / 登出。

* ``POST /api/login`` 同时接受 JSON 与表单编码：页面用表单（便于无 JS 也能登录），
  快捷指令与脚本用 JSON。
* 登录成功下发两枚 Cookie：``sk_session``（HttpOnly 签名令牌）与
  ``sk_csrf``（非 HttpOnly，供前端读取后放进请求头）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from app.logging import get_logger, kv
from app.ratelimit import login_limiter
from app.security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    csrf_token,
    sign_session,
    verify_password,
)

router = APIRouter(prefix="/api", tags=["auth"])
log = get_logger("auth")


async def _read_password(request: Request) -> str:
    """从 JSON 或表单体中取密码。"""
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - 非法 JSON 视为空密码
            return ""
        if isinstance(payload, dict):
            return str(payload.get("password") or "")
        return ""
    form = await request.form()
    return str(form.get("password") or "")


def _client_ip(request: Request) -> str:
    # Caddy 反代会带 X-Forwarded-For；取最左（原始客户端）
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _set_auth_cookies(response: Response, request: Request) -> None:
    cfg = request.app.state.config
    token = sign_session(cfg.auth.secret_key, cfg.auth.session_ttl_days, cfg.auth.session_epoch)
    max_age = cfg.auth.session_ttl_days * 86400
    # Secure 需要 HTTPS。本地开发走 http，此时不能带 Secure，否则浏览器丢弃 Cookie。
    secure = request.url.scheme == "https"
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token(cfg.auth.secret_key),
        max_age=max_age,
        httponly=False,  # 前端 JS 必须能读出来放进请求头
        secure=secure,
        samesite="lax",
        path="/",
    )


def _wants_html(request: Request) -> bool:
    return "application/json" not in (request.headers.get("content-type") or "").lower()


@router.post("/login")
async def login(request: Request) -> Response:
    cfg = request.app.state.config
    ip = _client_ip(request)

    allowed, retry_after = login_limiter.check(ip)
    if not allowed:
        log.warning("auth.login_rate_limited %s", kv(ip=ip, retry_after=retry_after))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"尝试过于频繁，请 {retry_after:.0f} 秒后再试",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    password = await _read_password(request)
    if not verify_password(password, cfg.auth.password_hash):
        log.warning("auth.login_failed %s", kv(ip=ip, password_len=len(password)))
        if _wants_html(request):
            return RedirectResponse("/login?error=1", status_code=status.HTTP_303_SEE_OTHER)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="密码错误")

    log.info("auth.login_ok %s", kv(ip=ip))
    response: Response
    if _wants_html(request):
        response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    else:
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _set_auth_cookies(response, request)
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    log.info("auth.logout")
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response
