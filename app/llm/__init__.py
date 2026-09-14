"""LLM 适配器层。"""

from app.llm.base import (
    LLMChannelRejected,
    LLMError,
    LLMNotConfigured,
    LLMOutputTruncated,
    LLMRequestFailed,
    LLMResult,
    LLMToolCallMissing,
    LLMTransient,
    PATH_JSON_FALLBACK,
    PATH_TOOL_CALL,
    VisionLLM,
)
from app.llm.registry import build_llm

__all__ = [
    "LLMChannelRejected",
    "LLMError",
    "LLMNotConfigured",
    "LLMOutputTruncated",
    "LLMRequestFailed",
    "LLMResult",
    "LLMToolCallMissing",
    "LLMTransient",
    "PATH_JSON_FALLBACK",
    "PATH_TOOL_CALL",
    "VisionLLM",
    "build_llm",
]
