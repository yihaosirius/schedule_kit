"""OpenAI 兼容的 ``/chat/completions`` 适配器 —— 第二传输层。

保留它是因为 **Responses API 的普及度还不如 chat completions**：
通义千问、硅基流动、Kimi、vLLM 等多数供应商目前只有 chat 一侧。
只有一家供应商时用 :mod:`app.llm.responses`，换了别家再切这个。

.. warning::

   **DeepSeek 上这个传输层必须显式关掉 thinking，否则每次请求都是 400。**

   chat 的 ``tool_choice`` 文档原文：``required`` 与命名工具选择
   **在 thinking 模式下不被支持，会返回 400 错误，请先关闭 thinking 模式**。
   而 ``thinking.type`` 的默认值是 ``enabled``。之前的版本发的正是命名
   ``tool_choice`` 且从不带 ``thinking`` 字段，所以**在 DeepSeek 上必然
   全部失败**——报错提示还写成"该供应商可能不支持强制 tool_choice"，
   把病因指错了方向。

   顺带两个后果：``temperature`` 在 thinking 模式下完全无效（我们配的
   ``temperature = 0`` 一直被静默忽略），而 thinking token 照价计费。
   抽取任务不需要思考，关掉它没有损失。
"""

from __future__ import annotations

import json
from typing import Any

from app.llm.base import (
    LLMChannelRejected,
    LLMOutputTruncated,
    LLMRequestFailed,
    LLMResult,
    LLMTransient,
    PATH_JSON_FALLBACK,
    PATH_TOOL_CALL,
    VisionLLM,
)
from app.llm.structured import (
    data_url,
    extract_structured,
    loads_lenient,
    require_llm_config,
)
from app.llm.tools import (
    JSON_FALLBACK_SUFFIX,
    TOOL_NAME,
    chat_forced_tool_choice,
    chat_tool,
    json_object_format,
)

#: 关掉 thinking 模式。理由见模块文档。
NO_THINKING = {"type": "disabled"}


class ChatCompatLLM(VisionLLM):
    """``{base_url}/chat/completions`` + 强制 function calling。"""

    name = "openai_compat"

    def __init__(self, section) -> None:
        require_llm_config(section)
        self.base_url = (section.base_url or "").rstrip("/")
        self.model = section.model
        self.api_key = section.api_key
        self.temperature = section.temperature
        self.timeout = section.timeout_seconds
        self.max_tokens = section.max_tokens
        self.retry_count = section.retry_count
        self.retry_backoff_seconds = section.retry_backoff_seconds

    # -- 报文 --------------------------------------------------------------- #
    def endpoint(self) -> str:
        # 允许用户填到 /v1 或直接填完整的 /chat/completions
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _user_content(self, user_text: str, images: list[bytes] | None) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        for image in images or []:
            content.append({"type": "image_url", "image_url": {"url": data_url(image)}})
        return content

    def build_payload(
        self, tier: str, *, system: str, user_text: str, images: list[bytes] | None
    ) -> dict[str, Any]:
        if tier == PATH_JSON_FALLBACK:
            system = system + JSON_FALLBACK_SUFFIX

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": self._user_content(user_text, images)},
            ],
            "thinking": dict(NO_THINKING),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        if tier == PATH_JSON_FALLBACK:
            payload["response_format"] = json_object_format()
        else:
            payload["tools"] = [chat_tool()]
            payload["tool_choice"] = chat_forced_tool_choice()
            payload["parallel_tool_calls"] = False

        return payload

    # -- 解析 --------------------------------------------------------------- #
    @staticmethod
    def _choice(body: dict[str, Any]) -> dict[str, Any]:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return {}
        first = choices[0]
        return first if isinstance(first, dict) else {}

    def _check_finish(self, choice: dict[str, Any]) -> None:
        reason = choice.get("finish_reason")
        if reason == "length":
            raise LLMOutputTruncated(
                f"输出被 max_tokens={self.max_tokens} 截断，JSON 不完整。请把 max_tokens 调大。"
            )
        if reason == "content_filter":
            raise LLMRequestFailed("输出被内容过滤器拦下，请换个说法或换供应商。")
        if reason == "insufficient_system_resource":
            # DeepSeek 文档里的状态：推理资源不足被中断。是临时故障。
            raise LLMTransient("推理资源不足（insufficient_system_resource）")

    def _parse_tool_call(self, body: dict[str, Any]) -> tuple[list[dict], str]:
        choice = self._choice(body)
        message = choice.get("message") or {}
        calls = message.get("tool_calls") or []
        if not calls:
            raise LLMChannelRejected(
                f"响应里没有 tool call（finish_reason={choice.get('finish_reason')}）"
            )

        items: list[dict] = []
        raws: list[str] = []
        for call in calls:
            function = (call or {}).get("function") or {}
            if function.get("name") != TOOL_NAME:
                raise LLMChannelRejected(f"模型调用了意料之外的工具：{function.get('name')}")
            arguments = function.get("arguments") or "{}"
            raws.append(arguments)
            try:
                parsed = json.loads(arguments)
            except ValueError as exc:
                raise LLMChannelRejected(f"工具参数不是合法 JSON：{exc}") from exc
            if not isinstance(parsed, dict):
                raise LLMChannelRejected("工具参数不是对象")
            chunk = parsed.get("items")
            if not isinstance(chunk, list):
                raise LLMChannelRejected("工具参数缺少 items 数组")
            items.extend(chunk)

        return items, "\n".join(raws)

    def _parse_json(self, body: dict[str, Any]) -> tuple[list[dict], str]:
        message = self._choice(body).get("message") or {}
        text = str(message.get("content") or "").strip()
        if not text:
            raise LLMChannelRejected(
                "JSON 通道返回了空内容"
                "——DeepSeek 文档提到 JSON 模式偶发空响应，重试通常就好"
            )
        payload = loads_lenient(text)
        if payload is None:
            raise LLMChannelRejected(f"JSON 通道的输出无法解析：{text[:200]}")
        items = payload.get("items")
        if not isinstance(items, list):
            raise LLMChannelRejected(f"JSON 通道的输出缺少 items 数组：{text[:200]}")
        return items, text

    def parse_response(self, tier: str, body: dict[str, Any]) -> tuple[list[dict], str]:
        self._check_finish(self._choice(body))
        if tier == PATH_TOOL_CALL:
            return self._parse_tool_call(body)
        return self._parse_json(body)

    # -- 入口 --------------------------------------------------------------- #
    async def extract(
        self, *, system: str, user_text: str, images: list[bytes] | None = None
    ) -> LLMResult:
        return await extract_structured(
            self, system=system, user_text=user_text, images=images
        )
