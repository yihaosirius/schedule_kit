"""LLM 适配器：报文形状、重试策略、JSON 降级通道。

这一层原先**几乎没有测试**——所有录入用例都走 ``MockLLM``，于是
``/chat/completions`` 的 payload 从来没有被断言过。结果是命名
``tool_choice`` 撞上 DeepSeek 默认开启的 thinking 模式、每次请求必然
400 这件事，一路活到了生产才被发现。

所以这里逐字段断言**实际发出的 JSON**，而不是只测"能跑通"。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.llm import structured
from app.llm.base import (
    PATH_JSON_FALLBACK,
    PATH_TOOL_CALL,
    LLMChannelRejected,
    LLMNotConfigured,
    LLMOutputTruncated,
    LLMRequestFailed,
    LLMToolCallMissing,
)
from app.llm.chat import ChatCompatLLM
from app.llm.responses import ResponsesLLM
from app.llm.structured import _backoff, _parse_retry_after, loads_lenient
from app.llm.tools import (
    JSON_FALLBACK_SUFFIX,
    TOOL_NAME,
    chat_tool,
    responses_tool,
)

SYSTEM = "你是抽取助手，只负责把内容变成事项。"
USER = "下周一交第三章习题"

SAMPLE_ITEMS = [
    {
        "title": "第三章习题",
        "category": "homework",
        "due_at": "2026-09-15T23:59:00+08:00",
        "priority": None,
    }
]


# --------------------------------------------------------------------------- #
# 替身
# --------------------------------------------------------------------------- #
class Section:
    """``LLMSection`` 的最小替身。"""

    def __init__(self, **overrides: Any) -> None:
        self.provider = "responses"
        self.base_url = "https://api.deepseek.com"
        self.model = "deepseek-flash"
        self.api_key = "sk-test"
        self.temperature = 0.0
        self.timeout_seconds = 30
        self.max_tokens = 1024
        self.max_image_bytes = 8 * 1024 * 1024
        self.system_prompt = SYSTEM
        self.retry_count = 3
        self.retry_backoff_seconds = 0.0
        for key, value in overrides.items():
            setattr(self, key, value)


class FakeClient:
    """按脚本返回响应的 ``httpx.AsyncClient`` 替身。

    脚本项要么是 ``httpx.Response``，要么是要抛出的异常。脚本用完还收到
    请求就直接断言失败——多发的请求几乎总是重试/降级逻辑写错了。
    """

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, **_kwargs: Any) -> FakeClient:
        return self

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def post(
        self, url: str, json: Any = None, headers: Any = None
    ) -> httpx.Response:
        self.requests.append({"url": url, "json": json, "headers": headers})
        if not self.script:
            raise AssertionError("脚本已用完，代码却还在发请求")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """退避置零，免得测试真的等 0.8+1.6+3.2 秒。

    只替换 ``_backoff`` 而不去 patch ``asyncio.sleep``：后者会被全局替换，
    连测试框架自己的调度都可能受影响。真正的退避算法由纯函数用例单独覆盖。
    """
    monkeypatch.setattr(structured, "_backoff", lambda *_a, **_k: 0.0)


def install(monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> FakeClient:
    fake = FakeClient(script)
    monkeypatch.setattr(structured.httpx, "AsyncClient", fake)
    return fake


def ok(payload: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def err(status: int, text: str = "boom", **headers: str) -> httpx.Response:
    return httpx.Response(status, text=text, headers=headers)


def responses_tool_body(
    items: list[dict] | None = None, *, status: str = "completed"
) -> dict[str, Any]:
    """一条正常的 Responses API 工具调用响应，含 thinking 产生的 reasoning 项。"""
    return {
        "id": "resp_1",
        "object": "response",
        "status": status,
        "model": "deepseek-flash",
        "output": [
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "想想"}]},
            {
                "type": "function_call",
                "call_id": "fc_1",
                "name": TOOL_NAME,
                "arguments": json.dumps({"items": items or SAMPLE_ITEMS}, ensure_ascii=False),
            },
        ],
        "usage": {"input_tokens": 120, "output_tokens": 40, "total_tokens": 160},
    }


def responses_json_body(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "resp_2",
        "object": "response",
        "status": "completed",
        "model": "deepseek-flash",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": json.dumps(payload, ensure_ascii=False)}
                ],
            }
        ],
        "usage": {"input_tokens": 130, "output_tokens": 50},
    }


def chat_tool_body(items: list[dict] | None = None) -> dict[str, Any]:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": TOOL_NAME,
                                "arguments": json.dumps(
                                    {"items": items or SAMPLE_ITEMS}, ensure_ascii=False
                                ),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 30},
    }


# --------------------------------------------------------------------------- #
# 报文形状：Responses
# --------------------------------------------------------------------------- #
def test_responses_tool_payload_shape() -> None:
    llm = ResponsesLLM(Section())
    payload = llm.build_payload(
        PATH_TOOL_CALL, system=SYSTEM, user_text=USER, images=None
    )

    assert payload["model"] == "deepseek-flash"
    assert payload["instructions"] == SYSTEM
    assert payload["temperature"] == 0.0
    # 协议改名：max_tokens -> max_output_tokens
    assert payload["max_output_tokens"] == 1024
    assert "max_tokens" not in payload

    # 工具定义是扁平的，没有嵌套的 function 对象
    tool = payload["tools"][0]
    assert tool["type"] == "function"
    assert tool["name"] == TOOL_NAME
    assert "function" not in tool
    assert tool["parameters"]["required"] == ["items"]

    # 命名 tool_choice 同样是扁平的——照抄 chat 的形状会被服务端拒绝
    assert payload["tool_choice"] == {"type": "function", "name": TOOL_NAME}

    # 关掉 thinking：命名 tool_choice 在 thinking 下会被 400 拒绝
    assert payload["reasoning"] == {"effort": "none"}

    # 用户消息是 input 项列表
    assert payload["input"][0]["role"] == "user"
    assert payload["input"][0]["content"][0] == {"type": "input_text", "text": USER}


def test_responses_json_fallback_payload() -> None:
    llm = ResponsesLLM(Section())
    payload = llm.build_payload(
        PATH_JSON_FALLBACK, system=SYSTEM, user_text=USER, images=None
    )

    assert "tools" not in payload
    assert "tool_choice" not in payload
    assert payload["text"] == {"format": {"type": "json_object"}}
    assert payload["reasoning"] == {"effort": "none"}

    instructions = payload["instructions"]
    assert instructions.startswith(SYSTEM)
    assert JSON_FALLBACK_SUFFIX in instructions
    # DeepSeek 的 JSON 模式硬性要求：prompt 里必须出现 "json" 这个词
    assert "json" in instructions
    # 必须显式作废前半段"必须调用工具"的指令，否则两段 prompt 互相打架
    assert "不要调用任何工具" in instructions
    # 二选一规则必须在降级通道里重申
    assert "due_at 与 priority" in instructions


def test_json_fallback_suffix_carries_a_format_example() -> None:
    """DeepSeek 文档要求 JSON 模式同时给出目标格式示例，否则可能空转到截断。"""
    assert '"items"' in JSON_FALLBACK_SUFFIX
    assert '"due_at"' in JSON_FALLBACK_SUFFIX
    assert "```" not in JSON_FALLBACK_SUFFIX.split("示例 json：")[0]


def test_responses_image_parts() -> None:
    llm = ResponsesLLM(Section())
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    payload = llm.build_payload(
        PATH_TOOL_CALL, system=SYSTEM, user_text=USER, images=[png]
    )
    parts = payload["input"][0]["content"]
    assert parts[1]["type"] == "input_image"
    assert parts[1]["image_url"].startswith("data:image/png;base64,")


def test_responses_endpoint_joining() -> None:
    assert (
        ResponsesLLM(Section()).endpoint() == "https://api.deepseek.com/responses"
    )
    # 已经写到 /responses 的不要再拼一次
    assert (
        ResponsesLLM(Section(base_url="https://x.invalid/v1/responses")).endpoint()
        == "https://x.invalid/v1/responses"
    )


def test_tool_schemas_do_not_share_a_mutable_dict() -> None:
    """两个形状必须各自深拷贝参数，避免任何一方就地改写真相。"""
    first = responses_tool()
    first["parameters"]["required"].append("污染")
    assert responses_tool()["parameters"]["required"] == ["items"]
    assert chat_tool()["function"]["parameters"]["required"] == ["items"]


# --------------------------------------------------------------------------- #
# 报文形状：Chat Completions
# --------------------------------------------------------------------------- #
def test_chat_disables_thinking_mode() -> None:
    """**核心回归用例。**

    DeepSeek 的 chat 文档：``required`` 与命名工具选择**在 thinking 模式
    下不被支持，会返回 400**；而 ``thinking.type`` 默认是 ``enabled``。
    旧代码发的正是命名 tool_choice 且从不带 thinking 字段，所以在 DeepSeek
    上每个请求都 400，报错却指向"供应商不支持强制 tool_choice"。
    """
    llm = ChatCompatLLM(Section(provider="openai_compat"))
    for tier in (PATH_TOOL_CALL, PATH_JSON_FALLBACK):
        payload = llm.build_payload(
            tier, system=SYSTEM, user_text=USER, images=None
        )
        assert payload["thinking"] == {"type": "disabled"}, f"{tier} 没有关掉 thinking"


def test_chat_tool_payload_shape() -> None:
    llm = ChatCompatLLM(Section(provider="openai_compat"))
    payload = llm.build_payload(
        PATH_TOOL_CALL, system=SYSTEM, user_text=USER, images=None
    )

    # chat 这一侧是**嵌套**形状，与 Responses 正好相反
    assert payload["tool_choice"] == {
        "type": "function",
        "function": {"name": TOOL_NAME},
    }
    assert payload["tools"][0]["function"]["name"] == TOOL_NAME
    assert payload["messages"][0] == {"role": "system", "content": SYSTEM}
    # chat 保留 max_tokens 这个名字
    assert payload["max_tokens"] == 1024
    assert "max_output_tokens" not in payload


def test_chat_json_fallback_payload() -> None:
    llm = ChatCompatLLM(Section(provider="openai_compat"))
    payload = llm.build_payload(
        PATH_JSON_FALLBACK, system=SYSTEM, user_text=USER, images=None
    )
    assert payload["response_format"] == {"type": "json_object"}
    assert "tools" not in payload
    assert payload["messages"][0]["content"].startswith(SYSTEM)
    assert "json" in payload["messages"][0]["content"]


def test_chat_image_parts_and_endpoint() -> None:
    llm = ChatCompatLLM(Section(provider="openai_compat", base_url="https://api.deepseek.com/v1"))
    payload = llm.build_payload(
        PATH_TOOL_CALL, system=SYSTEM, user_text=USER, images=[b"\xff\xd8\xff\xe0rest"]
    )
    parts = payload["messages"][1]["content"]
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert llm.endpoint() == "https://api.deepseek.com/v1/chat/completions"


# --------------------------------------------------------------------------- #
# 必填校验
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "overrides",
    [
        {"base_url": ""},
        {"model": ""},
        {"api_key": ""},
    ],
)
def test_missing_config_raises_not_configured(overrides: dict[str, Any]) -> None:
    with pytest.raises(LLMNotConfigured):
        ResponsesLLM(Section(**overrides))
    with pytest.raises(LLMNotConfigured):
        ChatCompatLLM(Section(provider="openai_compat", **overrides))


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
async def test_extract_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, [ok(responses_tool_body())])
    result = await ResponsesLLM(Section()).extract(system=SYSTEM, user_text=USER)

    assert result.items == SAMPLE_ITEMS
    assert result.path == PATH_TOOL_CALL
    assert result.fallback_note is None
    assert result.attempts == 1
    assert result.usage["input_tokens"] == 120
    assert len(fake.requests) == 1


async def test_parallel_tool_calls_are_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Responses API 的 ``parallel_tool_calls`` 恒为开启且无法关闭。

    只取第一个 function_call 会静静丢掉一半条目。
    """
    body = responses_tool_body()
    body["output"].append(
        {
            "type": "function_call",
            "call_id": "fc_2",
            "name": TOOL_NAME,
            "arguments": json.dumps(
                {
                    "items": [
                        {
                            "title": "背单词",
                            "category": "practice",
                            "due_at": None,
                            "priority": 3,
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        }
    )
    install(monkeypatch, [ok(body)])
    result = await ResponsesLLM(Section()).extract(system=SYSTEM, user_text=USER)

    assert [item["title"] for item in result.items] == ["第三章习题", "背单词"]
    assert "\n" in result.raw  # 两段原始参数都留档了


async def test_chat_merges_parallel_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    body = chat_tool_body()
    body["choices"][0]["message"]["tool_calls"].append(
        {
            "id": "call_2",
            "type": "function",
            "function": {
                "name": TOOL_NAME,
                "arguments": json.dumps(
                    {"items": [{"title": "背单词", "category": "practice", "due_at": None, "priority": 3}]},
                    ensure_ascii=False,
                ),
            },
        }
    )
    install(monkeypatch, [ok(body)])
    result = await ChatCompatLLM(Section(provider="openai_compat")).extract(
        system=SYSTEM, user_text=USER
    )
    assert [item["title"] for item in result.items] == ["第三章习题", "背单词"]


async def test_unexpected_tool_name_is_ignored_by_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = responses_tool_body()
    body["output"][1]["name"] = "delete_everything"
    install(monkeypatch, [ok(body), ok(responses_json_body({"items": SAMPLE_ITEMS}))])
    result = await ResponsesLLM(Section()).extract(system=SYSTEM, user_text=USER)
    assert result.path == PATH_JSON_FALLBACK


def test_truncation_reports_actionable_error() -> None:
    """截断跟"模型乱吐格式"是完全不同的病，必须分开报。"""
    llm = ResponsesLLM(Section())
    with pytest.raises(LLMOutputTruncated) as excinfo:
        llm.parse_response(
            PATH_TOOL_CALL,
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
            },
        )
    assert "max_output_tokens" in str(excinfo.value)


def test_chat_length_finish_reason_reports_truncation() -> None:
    llm = ChatCompatLLM(Section(provider="openai_compat"))
    with pytest.raises(LLMOutputTruncated):
        llm.parse_response(
            PATH_TOOL_CALL,
            {"choices": [{"finish_reason": "length", "message": {"content": "{"}}]},
        )


async def test_failed_status_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 200 但 ``status: failed`` —— 服务端说这次生成失败了，值得重试。"""
    failed = {
        "status": "failed",
        "error": {"code": "server_error", "message": "内部错误"},
        "output": [],
    }
    fake = install(monkeypatch, [ok(failed), ok(responses_tool_body())])
    result = await ResponsesLLM(Section(retry_count=1)).extract(
        system=SYSTEM, user_text=USER
    )
    assert result.attempts == 2
    assert len(fake.requests) == 2


# --------------------------------------------------------------------------- #
# 降级通道
# --------------------------------------------------------------------------- #
async def test_missing_function_call_falls_back_to_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 但没有 function call → 不重试，直接换通道。"""
    no_call = {"status": "completed", "output": [{"type": "message", "content": []}]}
    fake = install(
        monkeypatch, [ok(no_call), ok(responses_json_body({"items": SAMPLE_ITEMS}))]
    )
    result = await ResponsesLLM(Section()).extract(system=SYSTEM, user_text=USER)

    assert result.path == PATH_JSON_FALLBACK
    assert result.items == SAMPLE_ITEMS
    assert result.fallback_note and "function_call" in result.fallback_note
    assert len(fake.requests) == 2
    # 降级请求确实换了形状
    assert fake.requests[1]["json"]["text"] == {"format": {"type": "json_object"}}
    assert "tools" not in fake.requests[1]["json"]


async def test_rejected_tool_choice_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(
        monkeypatch,
        [
            err(400, '{"error":{"message":"tool_choice is not supported"}}'),
            ok(responses_json_body({"items": SAMPLE_ITEMS})),
        ],
    )
    result = await ResponsesLLM(Section()).extract(system=SYSTEM, user_text=USER)
    assert result.path == PATH_JSON_FALLBACK
    assert len(fake.requests) == 2


async def test_400_without_tool_choice_hint_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """别把所有 400 都当降级信号——那会把真正的报文错误藏起来。"""
    fake = install(monkeypatch, [err(400, "model not found")])
    with pytest.raises(LLMRequestFailed):
        await ResponsesLLM(Section()).extract(system=SYSTEM, user_text=USER)
    assert len(fake.requests) == 1


async def test_json_fallback_tolerates_code_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_call = {"status": "completed", "output": []}
    fenced = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "```json\n"
                        + json.dumps({"items": SAMPLE_ITEMS}, ensure_ascii=False)
                        + "\n```",
                    }
                ],
            }
        ],
    }
    install(monkeypatch, [ok(no_call), ok(fenced)])
    result = await ResponsesLLM(Section()).extract(system=SYSTEM, user_text=USER)
    assert result.items == SAMPLE_ITEMS
    assert result.path == PATH_JSON_FALLBACK


async def test_both_channels_failing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        [
            ok({"status": "completed", "output": []}),
            ok(responses_json_body({"unexpected": True})),
        ],
    )
    with pytest.raises(LLMToolCallMissing) as excinfo:
        await ResponsesLLM(Section()).extract(system=SYSTEM, user_text=USER)
    # 报错里要能看出两条通道各自为什么失败
    assert "function_call" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 重试
# --------------------------------------------------------------------------- #
async def test_transient_5xx_is_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(
        monkeypatch, [err(503), err(502), ok(responses_tool_body())]
    )
    result = await ResponsesLLM(Section(retry_count=3)).extract(
        system=SYSTEM, user_text=USER
    )
    assert result.attempts == 3
    assert len(fake.requests) == 3
    assert result.items == SAMPLE_ITEMS


async def test_transport_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(
        monkeypatch,
        [
            httpx.ConnectError("连接被拒绝"),
            httpx.ReadTimeout("超时"),
            ok(responses_tool_body()),
        ],
    )
    result = await ResponsesLLM(Section(retry_count=3)).extract(
        system=SYSTEM, user_text=USER
    )
    assert result.attempts == 3
    assert len(fake.requests) == 3


@pytest.mark.parametrize("status", [401, 403, 404, 413])
async def test_deterministic_errors_are_not_retried(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """认证/路径/体积错误再发一百次也是同样的结果，重试纯属浪费。"""
    fake = install(monkeypatch, [err(status, "nope")])
    with pytest.raises(LLMRequestFailed):
        await ResponsesLLM(Section(retry_count=3)).extract(
            system=SYSTEM, user_text=USER
        )
    assert len(fake.requests) == 1


async def test_transient_exhaustion_does_not_try_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """供应商挂了的时候换通道毫无意义。

    而且这条用例是"请求数上限"的守卫：4 次尝试 × 2 条通道 = 8 个请求，
    手机端的快捷指令要等好几分钟。必须停在 4。
    """
    fake = install(monkeypatch, [err(503)] * 4)
    with pytest.raises(LLMRequestFailed) as excinfo:
        await ResponsesLLM(Section(retry_count=3)).extract(
            system=SYSTEM, user_text=USER
        )
    assert len(fake.requests) == 4
    assert "4 次" in str(excinfo.value)
    # 没有一条请求是 JSON 通道
    assert all("text" not in request["json"] for request in fake.requests)


async def test_retry_count_zero_means_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(monkeypatch, [err(500)])
    with pytest.raises(LLMRequestFailed):
        await ResponsesLLM(Section(retry_count=0)).extract(
            system=SYSTEM, user_text=USER
        )
    assert len(fake.requests) == 1


def test_backoff_is_exponential_and_capped() -> None:
    # 基数 0.8，抖动 ±25%
    assert 0.6 <= _backoff(0.8, 1, None) <= 1.0
    assert 1.2 <= _backoff(0.8, 2, None) <= 2.0
    assert 2.4 <= _backoff(0.8, 3, None) <= 4.0
    # 上限封顶
    assert _backoff(10.0, 5, None) == structured.MAX_BACKOFF_SECONDS


def test_retry_after_is_honoured_but_capped() -> None:
    assert _backoff(0.8, 1, 3.0) == 3.0
    # 供应商说 60 秒也只等 8 秒：手机端等不了，真被限流了多等也没用
    assert _backoff(0.8, 1, 60.0) == structured.RETRY_AFTER_CAP_SECONDS


def test_parse_retry_after() -> None:
    assert _parse_retry_after(err(429, "slow", **{"Retry-After": "5"})) == 5.0
    assert _parse_retry_after(err(429)) is None
    # HTTP-date 形式：不解析，退避照常
    assert (
        _parse_retry_after(
            err(429, "slow", **{"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        )
        is None
    )


# --------------------------------------------------------------------------- #
# 宽松 JSON 解析
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        '{"items": []}',
        '  {"items": []}  ',
        '```json\n{"items": []}\n```',
        '好的，这是结果：\n{"items": []}\n希望有帮助。',
    ],
)
def test_loads_lenient_accepts_real_world_noise(text: str) -> None:
    assert loads_lenient(text) == {"items": []}


@pytest.mark.parametrize("text", ["", "没有 JSON", "[1, 2, 3]", "{坏掉"])
def test_loads_lenient_rejects_garbage(text: str) -> None:
    assert loads_lenient(text) is None


def test_parser_raises_channel_rejected_not_generic_error() -> None:
    """通道级失败必须能被编排层识别出来，否则不会触发降级。"""
    llm = ResponsesLLM(Section())
    with pytest.raises(LLMChannelRejected):
        llm.parse_response(PATH_TOOL_CALL, {"status": "completed", "output": []})
