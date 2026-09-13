"""按配置解析出适配器实例。

不做"自动探测能力后回退"：``provider`` 写的是什么就用什么，
不支持就明确报错。这样出问题时原因只有一个，不需要猜。
"""

from __future__ import annotations

from app.llm.base import LLMNotConfigured, VisionLLM

SUPPORTED_PROVIDERS = ("openai_compat", "mock")


def build_llm(config) -> VisionLLM:
    """按 ``[llm].provider`` 构建适配器。"""
    section = config.llm
    provider = (section.provider or "").strip()

    if provider == "mock":
        from app.llm.mock import MockLLM

        return MockLLM()

    if provider == "openai_compat":
        from app.llm.openai_compat import OpenAICompatLLM

        return OpenAICompatLLM(section)

    raise LLMNotConfigured(
        f"不支持的 LLM provider：{provider!r}。可选：{', '.join(SUPPORTED_PROVIDERS)}"
    )
