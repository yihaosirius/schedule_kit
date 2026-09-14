"""LLM 适配器的统一接口。

全系统只有一种结构化输出机制：**强制 function calling**。不做能力探测、
不做多档回退——供应商不支持强制 ``tool_choice`` 就是配置错误，配置页给出
明确报错，而不是悄悄退化成"解析模型自由文本"。（PLAN.md §10）

2026-09 修订：**唯一例外**是那条降级通道。DeepSeek 的命名 ``tool_choice``
在 thinking 模式下会被服务端 400 拒绝（见 :mod:`app.llm.chat`），而
"模型没产出 function call"也是一个真实的、无法事前探测的失败模式。
因此允许一次降级到 JSON 输出——但它必须是**响亮的**：写警告日志、
在草稿里记下 ``llm_path``、在确认页上标出来。
"不静默降级"这条原则没有变，变的只是"降级"不再等于"静默"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: ``LLMResult.path`` 的两种取值。
PATH_TOOL_CALL = "tool_call"
PATH_JSON_FALLBACK = "json_fallback"


class LLMError(RuntimeError):
    """LLM 调用失败的基类。"""


class LLMNotConfigured(LLMError):
    """配置缺失或供应商不受支持。"""


class LLMRequestFailed(LLMError):
    """请求层面失败：网络、超时、HTTP 错误码。"""


class LLMOutputTruncated(LLMError):
    """输出被 ``max_tokens`` 截断，JSON 必然不完整。

    单独一个类型是因为它和"模型乱吐格式"是完全不同的病：前者调大
    ``max_tokens`` 就好，后者要改 prompt。原先两者都报成"工具参数不是
    合法 JSON"，把排查方向指错了。
    """


class LLMTransient(LLMError):
    """临时性失败：连接、超时、429、5xx。

    可以安全重试——我们的请求是单发无副作用的（``store: false``，
    服务端不保存任何东西），重试不会造成重复写入。
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LLMChannelRejected(LLMError):
    """当前通道拿不到结构化输出 —— 换下一条通道，重试没有意义。

    两种触发情形：供应商明确拒绝强制 ``tool_choice``（HTTP 400/422），
    或者 200 响应里根本没有 function call。
    """


class LLMToolCallMissing(LLMError):
    """两条通道都没拿到可用的结构化输出。"""


@dataclass(frozen=True)
class LLMResult:
    """一次抽取的结果。"""

    items: list[dict[str, Any]]
    raw: str
    provider: str
    model: str
    elapsed_ms: float
    usage: dict[str, Any] = field(default_factory=dict)
    #: 走的是哪条通道：``tool_call`` 或 ``json_fallback``。
    path: str = PATH_TOOL_CALL
    #: 降级原因（仅在 ``path == json_fallback`` 时有值），用于日志与确认页。
    fallback_note: str | None = None
    #: 实际发出的尝试次数（含重试），便于判断供应商稳不稳定。
    attempts: int = 1


@runtime_checkable
class VisionLLM(Protocol):
    """所有适配器实现这一个方法。"""

    name: str

    async def extract(
        self, *, system: str, user_text: str, images: list[bytes] | None = None
    ) -> LLMResult:
        """从文本与（可选的）图片中抽取结构化事项。"""
        ...
