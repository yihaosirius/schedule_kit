"""结构化输出的共享编排：通道选择、重试、降级。

两个传输层（:mod:`app.llm.responses` / :mod:`app.llm.chat`）只负责把
统一的语义翻译成各自的报文形状，**所有控制流都在这里**：

1. **通道一：强制 function calling。** 默认路径。
2. **通道二：JSON 输出。** 仅当通道一被明确拒绝、或 200 响应里没有
   function call 时才走。触发时一定留下警告日志与 ``fallback_note``，
   绝不静默。

重试与降级是两套正交的策略，不要混在一起：

* **重试** —— 同样的请求再发一次。只对临时性失败（连接、超时、429、5xx）
  生效。这类失败下换通道毫无意义：供应商挂了，JSON 通道一样挂。
* **降级** —— 换一个请求（不用工具，改用 JSON 模式）。只对**确定性拒绝**
  生效。重试也没有意义：报文本身不合供应商的胃口，再发一百次还是 400。

所以临时性失败重试耗尽后**直接失败**，不会再去试降级通道——否则最坏
情况是 4 次 × 2 条通道 = 8 次请求，一个手机端请求要等好几分钟。
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from app.llm.base import (
    LLMChannelRejected,
    LLMError,
    LLMNotConfigured,
    LLMRequestFailed,
    LLMResult,
    LLMToolCallMissing,
    LLMTransient,
    PATH_JSON_FALLBACK,
    PATH_TOOL_CALL,
)
from app.logging import get_logger, kv

log = get_logger("llm")

#: 通道顺序。
TIERS = (PATH_TOOL_CALL, PATH_JSON_FALLBACK)

#: 除 5xx 之外还值得重试的状态码。
_TRANSIENT_STATUS = frozenset({408, 425, 429})

#: 识别"供应商拒绝强制 tool_choice"的关键词。
_TOOL_CHOICE_HINTS = ("tool_choice", "tool choice", "toolchoice")

#: ``Retry-After`` 最多等这么久。供应商说 60 秒也照样只等 8 秒：
#: 手机端的快捷指令等不了，而且真被限流了多等也没用。
RETRY_AFTER_CAP_SECONDS = 8.0

#: 单次退避的上限。
MAX_BACKOFF_SECONDS = 10.0

#: 整个请求（含重试）的总时间预算。
#:
#: 这是防止 ``retry_count=3`` + ``timeout=60`` 组合出 4 分钟请求的保险丝。
#: 超预算就不再重试，并在日志里说明是预算砍掉的，而不是供应商恢复了。
RETRY_TOTAL_BUDGET_SECONDS = 180.0


# --------------------------------------------------------------------------- #
# 图片辅助
# --------------------------------------------------------------------------- #
MIME_BY_SIGNATURE = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
}


def guess_mime(data: bytes) -> str:
    """按魔数判断图片类型——客户端声明的 MIME 不可信。"""
    for signature, mime in MIME_BY_SIGNATURE.items():
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def data_url(image: bytes) -> str:
    return f"data:{guess_mime(image)};base64,{base64.b64encode(image).decode('ascii')}"


def require_llm_config(section) -> None:
    """两个传输层共用的必填校验。

    ``api_key`` 单独查是因为不查的话 httpx 会抛
    ``Illegal header value b'Bearer '``，完全看不出是"没填 API Key"
    ——这是从一次真实运行日志里发现的。
    """
    if not (section.base_url or "").strip() or not (section.model or "").strip():
        raise LLMNotConfigured(
            "LLM 未配置：base_url 与 model 都是必填（在 /settings 里填）"
        )
    if not (section.api_key or "").strip():
        raise LLMNotConfigured(
            "LLM API Key 未设置。请在 /settings 里填入，保存后立即生效，无需重启。"
        )


# --------------------------------------------------------------------------- #
# 宽松 JSON 解析（降级通道专用）
# --------------------------------------------------------------------------- #
def outermost_object(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    return text[start : end + 1]


def loads_lenient(text: str) -> dict[str, Any] | None:
    """容忍模型偶尔不听话加上代码围栏或前后废话。

    只用在降级通道上。工具调用那条路拿到的是结构化字段，不需要猜。
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:]  # 去掉 ```json 这一行
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    for candidate in (cleaned, outermost_object(cleaned)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


# --------------------------------------------------------------------------- #
# 传输层协议
# --------------------------------------------------------------------------- #
@runtime_checkable
class StructuredTransport(Protocol):
    """一个供应商的报文形状翻译层。控制流一概不在这里。"""

    name: str
    model: str
    timeout: float
    retry_count: int
    retry_backoff_seconds: float

    def endpoint(self) -> str:
        """完整请求 URL。"""

    def headers(self) -> dict[str, str]:
        """请求头。"""

    def build_payload(
        self, tier: str, *, system: str, user_text: str, images: list[bytes] | None
    ) -> dict[str, Any]:
        """按通道构造请求体。"""

    def parse_response(self, tier: str, body: dict[str, Any]) -> tuple[list[dict], str]:
        """从响应体里取出条目与原始参数串。

        :raises LLMChannelRejected: 本通道没有可用结果，应换通道
        :raises LLMTransient: 服务端临时故障，可重试
        :raises LLMError: 不可恢复的失败
        """


# --------------------------------------------------------------------------- #
# 退避
# --------------------------------------------------------------------------- #
def _parse_retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        # HTTP-date 形式。极少见，不值得为它引入邮件头解析——退避照常。
        return None


def _backoff(base: float, attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(retry_after, RETRY_AFTER_CAP_SECONDS)
    delay = max(0.0, base) * (2 ** (attempt - 1))
    delay *= random.uniform(0.75, 1.25)  # 抖动：避免多客户端同时重试
    return min(delay, MAX_BACKOFF_SECONDS)


# --------------------------------------------------------------------------- #
# 单次尝试
# --------------------------------------------------------------------------- #
def _classify_http(response: httpx.Response, tier: str) -> LLMError:
    status = response.status_code
    text = response.text

    if status >= 500 or status in _TRANSIENT_STATUS:
        return LLMTransient(
            f"HTTP {status}：{text[:300]}", retry_after=_parse_retry_after(response)
        )

    if tier == PATH_TOOL_CALL and status in (400, 422):
        lowered = text.lower()
        if any(hint in lowered for hint in _TOOL_CHOICE_HINTS):
            return LLMChannelRejected(f"供应商拒绝强制 tool_choice（HTTP {status}）：{text[:300]}")

    return LLMRequestFailed(f"LLM 返回 HTTP {status}：{text[:500]}")


@dataclass
class _Run:
    """一次 ``extract_structured`` 的共享状态。"""

    deadline: float
    started: float
    attempts: int = 0


async def _attempt_once(
    transport: StructuredTransport,
    tier: str,
    payload: dict[str, Any],
    *,
    image_count: int,
) -> tuple[list[dict], str, dict]:
    log.info(
        "llm.request %s",
        kv(
            provider=transport.name,
            model=transport.model,
            endpoint=transport.endpoint(),
            tier=tier,
            images=image_count,
        ),
    )

    try:
        async with httpx.AsyncClient(timeout=transport.timeout) as client:
            response = await client.post(
                transport.endpoint(), json=payload, headers=transport.headers()
            )
    except httpx.HTTPError as exc:
        raise LLMTransient(f"{type(exc).__name__}: {exc}") from exc

    if response.status_code >= 400:
        raise _classify_http(response, tier)

    try:
        body = response.json()
    except ValueError as exc:
        # 200 却不是 JSON：多半是打到了登录页或反向代理的错误页。重试无意义。
        raise LLMRequestFailed(
            f"LLM 返回的不是 JSON（HTTP {response.status_code}）：{response.text[:200]}"
        ) from exc

    items, raw = transport.parse_response(tier, body)
    usage = body.get("usage")
    return items, raw, usage if isinstance(usage, dict) else {}


# --------------------------------------------------------------------------- #
# 单条通道（含重试）
# --------------------------------------------------------------------------- #
async def _run_tier(
    transport: StructuredTransport,
    tier: str,
    payload: dict[str, Any],
    run: _Run,
    *,
    image_count: int,
) -> tuple[list[dict], str, dict]:
    retries = max(0, transport.retry_count)

    while True:
        run.attempts += 1
        attempt = run.attempts
        try:
            items, raw, usage = await _attempt_once(
                transport, tier, payload, image_count=image_count
            )
            return items, raw, usage
        except LLMTransient as exc:
            if attempt > retries:
                log.warning(
                    "llm.retries_exhausted %s",
                    kv(tier=tier, attempts=attempt, error=str(exc)[:200]),
                )
                raise LLMRequestFailed(
                    f"LLM 连续 {attempt} 次临时失败（已重试 {retries} 次）：{exc}"
                ) from exc

            now = time.perf_counter()
            if now >= run.deadline:
                log.warning(
                    "llm.retry_budget_exhausted %s",
                    kv(
                        tier=tier,
                        attempts=attempt,
                        elapsed_ms=round((now - run.started) * 1000, 1),
                        budget_seconds=RETRY_TOTAL_BUDGET_SECONDS,
                    ),
                )
                raise LLMRequestFailed(
                    f"重试超出总时间预算（{RETRY_TOTAL_BUDGET_SECONDS:.0f} 秒）：{exc}"
                ) from exc

            delay = _backoff(transport.retry_backoff_seconds, attempt, exc.retry_after)
            log.warning(
                "llm.retry %s",
                kv(
                    tier=tier,
                    attempt=attempt,
                    max_attempts=retries + 1,
                    delay_ms=round(delay * 1000, 1),
                    retry_after=exc.retry_after,
                    error=str(exc)[:200],
                ),
            )
            await asyncio.sleep(delay)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
async def extract_structured(
    transport: StructuredTransport,
    *,
    system: str,
    user_text: str,
    images: list[bytes] | None = None,
) -> LLMResult:
    """走完通道阶梯，返回第一个可用的结构化结果。"""
    started = time.perf_counter()
    run = _Run(deadline=started + RETRY_TOTAL_BUDGET_SECONDS, started=started)
    image_count = len(images or [])
    fallback_note: str | None = None
    reasons: list[str] = []

    for tier in TIERS:
        payload = transport.build_payload(
            tier, system=system, user_text=user_text, images=images
        )
        try:
            items, raw, usage = await _run_tier(
                transport, tier, payload, run, image_count=image_count
            )
        except LLMChannelRejected as exc:
            # 降级是响亮的：日志 + 回传的 fallback_note 都会带上原因。
            fallback_note = str(exc)
            reasons.append(f"{tier}：{exc}")
            log.warning(
                "llm.channel_rejected %s",
                kv(
                    provider=transport.name,
                    model=transport.model,
                    tier=tier,
                    attempts=run.attempts,
                    reason=str(exc)[:300],
                ),
            )
            continue

        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        if tier == PATH_JSON_FALLBACK:
            log.warning(
                "llm.fallback_used %s",
                kv(
                    provider=transport.name,
                    model=transport.model,
                    items=len(items),
                    attempts=run.attempts,
                    reason=(fallback_note or "")[:300],
                ),
            )
        log.info(
            "llm.response %s",
            kv(
                provider=transport.name,
                model=transport.model,
                tier=tier,
                items=len(items),
                raw_chars=len(raw),
                attempts=run.attempts,
                elapsed_ms=elapsed_ms,
                input_tokens=usage.get("input_tokens") or usage.get("prompt_tokens"),
                output_tokens=usage.get("output_tokens") or usage.get("completion_tokens"),
            ),
        )
        return LLMResult(
            items=items,
            raw=raw,
            provider=transport.name,
            model=transport.model,
            elapsed_ms=elapsed_ms,
            usage=usage,
            path=tier,
            fallback_note=fallback_note,
            attempts=run.attempts,
        )

    # 两条通道各自为什么失败都要报出来：第一条通常才是真正有用的那条
    # （"模型不支持强制 tool_choice" 比"降级通道也没有 items"重要得多）。
    raise LLMToolCallMissing(
        "强制工具调用与 JSON 两条通道都没能拿到结构化输出 —— " + "；".join(reasons)
    )
