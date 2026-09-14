"""Responses API 适配器（``{base_url}/responses``）—— 默认传输层。

以 DeepSeek 的 Responses API 为参照实现，但它就是 OpenAI 的那套格式，
所以任何实现了 ``/responses`` 的供应商都能直接用。

**为什么要从 ``/chat/completions`` 换过来**，三个实打实的理由：

1. DeepSeek 的 ``tool_choice`` 文档里写着 ``required`` 与命名工具选择
   **在 thinking 模式下不被支持，会返回 400**，而 thinking 的默认值是
   ``enabled``。chat 传输层必须靠显式关掉 thinking 才能用强制工具调用；
   Responses 这一侧至少不存在这条文档化的冲突。
2. Responses 的报文里 ``function_call`` 是 ``output`` 数组里的一等公民，
   解析不必再穿过 ``choices[0].message.tool_calls[*].function``。
3. ``text.format`` 原生支持 ``json_object`` / ``json_schema``，
   降级通道不需要再依赖 chat 那套 ``response_format``。

**几个必须记住的协议细节**（照抄 chat 的形状就会踩）：

* 工具定义是**扁平**的：``{"type":"function","name":...}``，
  没有嵌套的 ``function`` 对象。
* 命名 ``tool_choice`` 同样是扁平的：``{"type":"function","name":"..."}``。
* ``max_tokens`` 改叫 ``max_output_tokens``，而且**包含** reasoning token。
* ``parallel_tool_calls`` 被**忽略**（并行永远开启），所以一条响应里可能
  有多个 ``function_call`` —— 必须全部合并，只取第一个会丢数据。
* ``store`` / ``previous_response_id`` / ``conversation`` 都不支持，
  接口是无状态的；我们本来就是单发请求，没有影响。
* 图片走 ``input_image``，且**不允许**出现在 system / assistant 消息里，
  所以图片只能挂在 user 消息上。
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
    json_object_format,
    responses_forced_tool_choice,
    responses_tool,
)

#: 关掉 thinking。
#:
#: 对抽取任务来说这是纯赚：思考 token 照价计费、拉长延迟，而 ``temperature``
#: 在 thinking 模式下**完全无效**——不关掉的话我们配的 ``temperature = 0``
#: 是被静默忽略的。另外命名 tool_choice 在 thinking 下的行为两边文档说法
#: 不一致（chat 文档说 400，Responses 文档没提），关掉它就不用赌。
NO_THINKING = {"effort": "none"}


class ResponsesLLM(VisionLLM):
    """``{base_url}/responses`` + 强制 function calling。"""

    name = "responses"

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
        # 允许用户填到域名（DeepSeek 就是 https://api.deepseek.com），
        # 也允许直接填完整的 .../responses，避免出现 /responses/responses。
        if self.base_url.endswith("/responses"):
            return self.base_url
        return f"{self.base_url}/responses"

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _input(self, user_text: str, images: list[bytes] | None) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "input_text", "text": user_text}]
        for image in images or []:
            content.append({"type": "input_image", "image_url": data_url(image)})
        return [{"role": "user", "content": content}]

    def build_payload(
        self, tier: str, *, system: str, user_text: str, images: list[bytes] | None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": self._input(user_text, images),
            "reasoning": dict(NO_THINKING),
            "temperature": self.temperature,
            "max_output_tokens": self.max_tokens,
        }

        if tier == PATH_JSON_FALLBACK:
            # 降级时 system prompt 必须改写：前半段还在要求"必须调用工具"，
            # 不覆盖的话两段指令互相打架。JSON_FALLBACK_SUFFIX 同时满足
            # DeepSeek 对 JSON 模式的两个硬性要求（prompt 里出现 "json" 这个词、
            # 给出目标格式示例）。
            payload["instructions"] = system + JSON_FALLBACK_SUFFIX
            payload["text"] = {"format": json_object_format()}
        else:
            payload["tools"] = [responses_tool()]
            payload["tool_choice"] = responses_forced_tool_choice()

        return payload

    # -- 解析 --------------------------------------------------------------- #
    def _check_status(self, body: dict[str, Any]) -> None:
        status = body.get("status")
        if status == "failed":
            error = body.get("error") or {}
            # HTTP 是 200，但服务端说这次生成失败了。可能是临时故障，值得重试。
            raise LLMTransient(
                f"响应 failed：{error.get('code')} {error.get('message')}".strip()
            )
        if status == "incomplete":
            details = body.get("incomplete_details") or {}
            reason = details.get("reason")
            if reason == "max_output_tokens":
                raise LLMOutputTruncated(
                    f"输出被 max_output_tokens={self.max_tokens} 截断，JSON 不完整。"
                    "请把 max_tokens 调大（注意它包含思考 token）。"
                )
            raise LLMRequestFailed(f"响应不完整：{reason}")

    @staticmethod
    def _function_calls(body: dict[str, Any]) -> list[dict[str, Any]]:
        output = body.get("output")
        if not isinstance(output, list):
            return []
        return [
            item
            for item in output
            if isinstance(item, dict) and item.get("type") == "function_call"
        ]

    def _parse_tool_call(self, body: dict[str, Any]) -> tuple[list[dict], str]:
        calls = self._function_calls(body)
        if not calls:
            kinds = [
                item.get("type")
                for item in (body.get("output") or [])
                if isinstance(item, dict)
            ]
            raise LLMChannelRejected(
                f"响应里没有 function_call（status={body.get('status')}，output={kinds}）"
            )

        # parallel_tool_calls 在 Responses API 里恒为开启且无法关闭，
        # 所以模型可能把事项拆到多个 function_call 里。全部合并——只取
        # 第一个会静静丢掉一半条目。
        items: list[dict] = []
        raws: list[str] = []
        for call in calls:
            name = call.get("name")
            if name != TOOL_NAME:
                raise LLMChannelRejected(f"模型调用了意料之外的工具：{name}")

            arguments = call.get("arguments") or "{}"
            raws.append(arguments)
            try:
                parsed = json.loads(arguments)
            except ValueError as exc:
                raise LLMChannelRejected(f"function call 参数不是合法 JSON：{exc}") from exc
            if not isinstance(parsed, dict):
                raise LLMChannelRejected("function call 参数不是对象")
            chunk = parsed.get("items")
            if not isinstance(chunk, list):
                raise LLMChannelRejected("function call 参数缺少 items 数组")
            items.extend(chunk)

        return items, "\n".join(raws)

    @staticmethod
    def _output_text(body: dict[str, Any]) -> str:
        chunks: list[str] = []
        for item in body.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    chunks.append(str(part.get("text") or ""))
        return "".join(chunks)

    def _parse_json(self, body: dict[str, Any]) -> tuple[list[dict], str]:
        text = self._output_text(body).strip()
        if not text:
            raise LLMChannelRejected(
                f"JSON 通道返回了空内容（status={body.get('status')}）"
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
        self._check_status(body)
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
