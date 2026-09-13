"""统一的 trace 日志。

写日志的约定（同时见 ``docs/dev-notes.md``）：

* **单行结构化**：``时间 级别 logger [t=<trace_id>] 事件名 键=值 键=值``
* **事件名可检索**：用 ``点分.小写`` 命名关键决策点，例如
  ``request.start`` / ``llm.response`` / ``normalize.item`` / ``draft.confirm``。
  出问题时按事件名 grep 就能还原整条链路，不用读源码。
* **关键决策点必须打点**：尤其是"代码推翻了模型输出"的地方
  （如"有 deadline 所以丢弃模型给的 priority"），否则事后无法判断
  到底是模型错了还是后处理改的。
* **绝不打点密钥**：``kv()`` 会自动屏蔽形如 ``api_key`` / ``token`` /
  ``password`` / ``secret`` / ``cookie`` 的字段。任何可疑值用 :func:`mask` 包裹。
* **同一个请求共享 trace_id**：由 ``main.py`` 的中间件注入，
  也会回写到响应的 ``X-Trace-Id`` 头，便于把客户端报错与服务端日志对上。
"""

from __future__ import annotations

import contextvars
import logging
import secrets
import sys
from pathlib import Path
from typing import Any

TRACE_ID: contextvars.ContextVar[str] = contextvars.ContextVar("sk_trace_id", default="-")

LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-5s %(name)-16s [t=%(trace_id)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"

#: 字段名整体等于其中之一 → 敏感。
SENSITIVE_NAMES = frozenset(
    {
        "api_key", "apikey", "token", "access_token", "refresh_token",
        "password", "passwd", "secret", "authorization", "cookie",
        "credential", "credentials",
    }
)

#: 字段名的**最后一段**等于其中之一 → 敏感（覆盖 xxx_key / xxx_token 这类）。
SENSITIVE_SUFFIXES = frozenset({"key", "token", "password", "passwd", "secret", "hash", "cookie", "credential"})


# --------------------------------------------------------------------------- #
# trace id
# --------------------------------------------------------------------------- #
def new_trace_id() -> str:
    """生成一个短的、可读性尚可的 trace id。"""
    return secrets.token_hex(4)


def set_trace_id(value: str | None = None) -> str:
    """设置当前上下文的 trace id，并返回最终值。"""
    trace_id = (value or "").strip() or new_trace_id()
    TRACE_ID.set(trace_id)
    return trace_id


def get_trace_id() -> str:
    return TRACE_ID.get()


# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #
def mask(value: Any, *, keep: int = 4) -> str:
    """把敏感值变成 ``abcd…(len=43)`` 形式，保留前缀便于比对。"""
    text = "" if value is None else str(value)
    if not text:
        return "(empty)"
    if len(text) <= keep:
        return f"…(len={len(text)})"
    return f"{text[:keep]}…(len={len(text)})"


def _is_sensitive(name: str) -> bool:
    """判断字段名是否敏感。

    刻意**不**用"包含子串"这种糙规则：那样 ``max_tokens`` 会因为含
    ``token`` 被误脱敏成 ``…(len=4)``，日志里就再也看不到真实取值了。
    改为看整体名与最后一段。

    （这是从一次真实运行输出里发现的问题：``llm_api_key_set=True`` 被
    脱敏成 ``Fals…(len=5)``，布尔值的信息完全丢失。）
    """
    lowered = name.lower()
    if lowered in SENSITIVE_NAMES:
        return True
    return lowered.rsplit("_", 1)[-1] in SENSITIVE_SUFFIXES


def redact_value(name: str, value: Any) -> str:
    """按字段名决定是否脱敏，返回可直接写入日志的字符串。"""
    if value is None:
        return "-"
    # 布尔与数值本身不可能承载密钥，先放行，避免丢信息
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{value:g}" if isinstance(value, float) else str(value)
    if _is_sensitive(name):
        return mask(value)
    if isinstance(value, (list, tuple, set)):
        return f"[{len(value)}]"
    if isinstance(value, dict):
        return f"{{{len(value)}}}"
    text = str(value)
    if "\n" in text:
        text = text.replace("\n", "\\n")
    if len(text) > 300:
        text = text[:300] + f"…(+{len(text) - 300})"
    return text


def _quote(text: str) -> str:
    if text == "-" or (" " not in text and '"' not in text and "=" not in text):
        return text
    return '"' + text.replace('"', '\\"') + '"'


def kv(**fields: Any) -> str:
    """把关键字字段渲染成 ``键=值`` 串，自动脱敏并对含空格的值加引号。"""
    parts: list[str] = []
    for name, value in fields.items():
        if value is None:
            continue
        parts.append(f"{name}={_quote(redact_value(name, value))}")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #
class _TraceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = TRACE_ID.get()
        return True


def setup_logging(
    level: str | int = "INFO",
    *,
    log_file: str | Path | None = None,
    stream: Any | None = None,
) -> None:
    """配置根日志器。重复调用是幂等的（会先清掉已有 handler）。"""
    handler_stream = stream if stream is not None else sys.stdout
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
    trace_filter = _TraceFilter()

    handlers: list[logging.Handler] = [logging.StreamHandler(handler_stream)]
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(trace_filter)
        root.addHandler(handler)

    root.setLevel(level)
    # uvicorn 自己的 logger 保持安静，访问日志由我们的中间件负责。
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """取业务 logger，统一挂在 ``sk.`` 命名空间下。"""
    return logging.getLogger(name if name.startswith("sk.") else f"sk.{name}")
