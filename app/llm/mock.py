"""测试与离线开发用的假适配器。

它存在的意义是让整条录入链路（校验 → 后处理 → 草稿 → 确认）能在
**完全不访问网络**的情况下被测到，而且能把各种畸形输出（同时给
deadline 与 priority、越界优先级、未知分类、空标题、无 tool call）
精确注入进去。
"""

from __future__ import annotations

import json
import time

from app.llm.base import (
    PATH_TOOL_CALL,
    LLMError,
    LLMResult,
    LLMToolCallMissing,
    VisionLLM,
)


class MockLLM(VisionLLM):
    """按预设返回结果。"""

    name = "mock"

    def __init__(
        self,
        items: list[dict] | None = None,
        *,
        error: Exception | None = None,
        echo_user_text: bool = False,
        path: str = PATH_TOOL_CALL,
        fallback_note: str | None = None,
    ) -> None:
        self._items = items if items is not None else []
        self._error = error
        self._echo_user_text = echo_user_text
        self._path = path
        self._fallback_note = fallback_note
        self.calls: list[dict] = []

    async def extract(
        self, *, system: str, user_text: str, images: list[bytes] | None = None
    ) -> LLMResult:
        self.calls.append(
            {"system": system, "user_text": user_text, "images": len(images or [])}
        )
        if self._error is not None:
            raise self._error

        items = list(self._items)
        if self._echo_user_text and not items:
            items = [{"title": user_text[:20] or "空", "category": "other", "due_at": None, "priority": 3}]

        started = time.perf_counter()
        raw = json.dumps({"items": items}, ensure_ascii=False)
        return LLMResult(
            items=items,
            raw=raw,
            provider=self.name,
            model="mock-model",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
            path=self._path,
            fallback_note=self._fallback_note,
        )


__all__ = ["MockLLM", "LLMError", "LLMToolCallMissing"]
