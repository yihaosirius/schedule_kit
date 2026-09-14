"""按配置解析出适配器实例。

不做"自动探测能力后回退"：``provider`` 写的是什么就用什么，
不支持就明确报错。这样出问题时原因只有一个，不需要猜。

============================ ==========================================
``responses``（默认）         ``{base_url}/responses``，DeepSeek / OpenAI
``openai_compat``            ``{base_url}/chat/completions``，只提供 chat
                             的供应商
``mock``                     离线测试
============================ ==========================================
"""

from __future__ import annotations

from app.llm.base import LLMNotConfigured, VisionLLM

SUPPORTED_PROVIDERS = ("responses", "openai_compat", "mock")

#: 老配置里 ``provider = "openai_compat"`` 仍然有效，但默认值换成了 responses。
DEFAULT_PROVIDER = "responses"


def build_llm(config) -> VisionLLM:
    """按 ``[llm].provider`` 构建适配器。"""
    section = config.llm
    provider = (section.provider or "").strip() or DEFAULT_PROVIDER

    if provider == "mock":
        from app.llm.mock import MockLLM

        return MockLLM()

    if provider == "responses":
        from app.llm.responses import ResponsesLLM

        return ResponsesLLM(section)

    if provider == "openai_compat":
        from app.llm.chat import ChatCompatLLM

        return ChatCompatLLM(section)

    raise LLMNotConfigured(
        f"不支持的 LLM provider：{provider!r}。可选：{', '.join(SUPPORTED_PROVIDERS)}"
    )
