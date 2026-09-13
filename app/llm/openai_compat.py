"""OpenAI 兼容协议的适配器（用 httpx 直连，不引入厂商 SDK）。

一套实现覆盖 OpenAI、DeepSeek、通义千问、硅基流动、vLLM 等——
只改 ``base_url`` / ``model`` / ``api_key`` 即可切换，这也是当初选它做
默认的原因。厂商 SDK 会各自带来依赖与版本噪音，而我们只用到一个端点。
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx

from app.llm.base import (
    LLMRequestFailed,
    LLMResult,
    LLMToolCallMissing,
    VisionLLM,
)
from app.llm.tools import SUBMIT_TASKS_TOOL, TOOL_NAME, forced_tool_choice
from app.logging import get_logger, kv

log = get_logger("llm")

MIME_BY_SIGNATURE = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
}


def guess_mime(data: bytes) -> str:
    for signature, mime in MIME_BY_SIGNATURE.items():
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class OpenAICompatLLM(VisionLLM):
    """``{base_url}/chat/completions`` + 强制 function calling。"""

    name = "openai_compat"

    def __init__(self, section) -> None:
        self.base_url = (section.base_url or "").rstrip("/")
        self.model = section.model
        self.api_key = section.api_key
        self.temperature = section.temperature
        self.timeout = section.timeout_seconds
        self.max_tokens = section.max_tokens
        if not self.base_url or not self.model:
            from app.llm.base import LLMNotConfigured

            raise LLMNotConfigured("LLM 未配置：base_url 与 model 都是必填（在 /settings 里填）")
        if not self.api_key:
            # 不拦的话 httpx 会抛 "Illegal header value b'Bearer '"，完全看不出
            # 是"没填 API Key"——这是从一次真实运行日志里发现的。
            from app.llm.base import LLMNotConfigured

            raise LLMNotConfigured(
                "LLM API Key 未设置。请在 /settings 里填入，保存后立即生效，无需重启。"
            )

    def _endpoint(self) -> str:
        # 允许用户填到 /v1 或直接填完整的 /chat/completions
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _build_payload(self, system: str, user_text: str, images: list[bytes] | None) -> dict:
        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        for image in images or []:
            encoded = base64.b64encode(image).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{guess_mime(image)};base64,{encoded}"},
                }
            )

        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "tools": [SUBMIT_TASKS_TOOL],
            "tool_choice": forced_tool_choice(),
            "parallel_tool_calls": False,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

    async def extract(
        self, *, system: str, user_text: str, images: list[bytes] | None = None
    ) -> LLMResult:
        payload = self._build_payload(system, user_text, images)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        log.info(
            "llm.request %s",
            kv(
                provider=self.name,
                model=self.model,
                endpoint=self._endpoint(),
                images=len(images or []),
                system_chars=len(system),
                user_chars=len(user_text),
                max_tokens=self.max_tokens,
            ),
        )

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self._endpoint(), json=payload, headers=headers)
        except httpx.HTTPError as exc:
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            log.warning("llm.transport_error %s", kv(elapsed_ms=elapsed, error=type(exc).__name__, detail=str(exc)))
            raise LLMRequestFailed(f"请求 LLM 失败：{type(exc).__name__}: {exc}") from exc

        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

        if response.status_code >= 400:
            body = response.text[:500]
            log.warning("llm.http_error %s", kv(status=response.status_code, elapsed_ms=elapsed_ms, body=body))
            hint = ""
            if response.status_code in (400, 422) and "tool_choice" in body:
                hint = "（该供应商可能不支持强制 tool_choice，请换模型或供应商）"
            raise LLMRequestFailed(f"LLM 返回 HTTP {response.status_code}{hint}：{body}")

        try:
            body = response.json()
        except ValueError as exc:
            raise LLMRequestFailed(f"LLM 返回的不是 JSON：{response.text[:200]}") from exc

        message = (body.get("choices") or [{}])[0].get("message") or {}
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            # 关键决策：没有 tool call 就是失败，不退回解析自由文本
            log.warning(
                "llm.no_tool_call %s",
                kv(elapsed_ms=elapsed_ms, finish_reason=(body.get("choices") or [{}])[0].get("finish_reason")),
            )
            raise LLMToolCallMissing("模型没有调用 submit_tasks（可能不支持强制 tool_choice）")

        call = tool_calls[0]
        function = call.get("function") or {}
        if function.get("name") != TOOL_NAME:
            raise LLMToolCallMissing(f"模型调用了意料之外的工具：{function.get('name')}")

        arguments = function.get("arguments") or "{}"
        try:
            parsed = json.loads(arguments)
        except ValueError as exc:
            log.warning("llm.bad_arguments %s", kv(elapsed_ms=elapsed_ms, arguments=arguments[:200]))
            raise LLMToolCallMissing(f"工具参数不是合法 JSON：{exc}") from exc

        items = parsed.get("items")
        if not isinstance(items, list):
            raise LLMToolCallMissing("工具参数缺少 items 数组")

        log.info(
            "llm.response %s",
            kv(elapsed_ms=elapsed_ms, tool_call=TOOL_NAME, items=len(items), raw_chars=len(arguments)),
        )
        return LLMResult(
            items=items,
            raw=arguments,
            provider=self.name,
            model=self.model,
            elapsed_ms=elapsed_ms,
            usage=body.get("usage") or {},
        )
