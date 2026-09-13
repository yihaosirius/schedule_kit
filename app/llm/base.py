"""LLM 适配器的统一接口。

全系统只有一种结构化输出机制：**强制 function calling**。
不做能力探测、不做多档回退——供应商不支持强制 ``tool_choice`` 就是
不支持，配置页给出明确报错，而不是悄悄退化成"解析模型自由文本"。
（PLAN.md §10）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class LLMError(RuntimeError):
    """LLM 调用失败的基类。"""


class LLMNotConfigured(LLMError):
    """配置缺失或供应商不受支持。"""


class LLMRequestFailed(LLMError):
    """请求层面失败：网络、超时、HTTP 错误码。"""


class LLMToolCallMissing(LLMError):
    """响应里没有 tool call —— 视为失败，不退回文本解析。"""


@dataclass(frozen=True)
class LLMResult:
    """一次抽取的结果。"""

    items: list[dict[str, Any]]
    raw: str
    provider: str
    model: str
    elapsed_ms: float
    usage: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class VisionLLM(Protocol):
    """所有适配器实现这一个方法。"""

    name: str

    async def extract(
        self, *, system: str, user_text: str, images: list[bytes] | None = None
    ) -> LLMResult:
        """从文本与（可选的）图片中抽取结构化事项。"""
        ...
