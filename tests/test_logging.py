"""M0：trace 日志的脱敏与格式约定。

日志是事后排查的唯一线索，所以脱敏规则本身也要有测试兜住——
一旦有人往 ``kv()`` 里塞了密钥字段，这里应当先红。
"""

from __future__ import annotations

import logging

import pytest

from app.logging import get_logger, get_trace_id, kv, mask, set_trace_id, setup_logging


@pytest.mark.parametrize(
    "field",
    ["api_key", "apikey", "token", "duckdns_token", "password", "password_hash", "secret_key", "Cookie", "Authorization"],
)
def test_kv_masks_sensitive_fields(field: str, ) -> None:
    rendered = kv(**{field: "super-secret-value-1234567890"})
    assert "super-secret-value-1234567890" not in rendered
    assert "…" in rendered, f"{field} 应被脱敏成掩码形式，实际：{rendered}"


def test_kv_does_not_mask_innocent_fields_containing_sensitive_substrings() -> None:
    """从真实运行日志里发现的问题：max_tokens 因为含 "token" 被脱敏成 …(len=4)。"""
    rendered = kv(max_tokens=1024, token_count=7, monkey="x", keychain_id="abc")
    assert "max_tokens=1024" in rendered
    assert "token_count=7" in rendered
    # 末尾是 key 的仍然要脱敏
    assert "keychain_id=abc" in rendered


def test_kv_preserves_booleans_and_numbers() -> None:
    """布尔值被脱敏成 "Fals…(len=5)" 会让人完全读不懂日志。"""
    rendered = kv(api_key_set=True, ready=False, elapsed_ms=12.5, count=3)
    assert "api_key_set=true" in rendered
    assert "ready=false" in rendered
    assert "elapsed_ms=12.5" in rendered
    assert "count=3" in rendered


def test_kv_still_masks_real_secrets_after_relaxing_the_rule() -> None:
    rendered = kv(api_key="sk-abcdefghijklmnop", duckdns_token="uuid-1234", password_hash="scrypt$abc")
    assert "sk-abcdefghijklmnop" not in rendered
    assert "uuid-1234" not in rendered
    assert "scrypt$abc" not in rendered


def test_kv_keeps_nonsensitive_fields_readable() -> None:
    rendered = kv(method="POST", path="/api/tasks", status=201, elapsed_ms=12.5, ok=True)
    assert "method=POST" in rendered
    assert "path=/api/tasks" in rendered
    assert "status=201" in rendered
    assert "elapsed_ms=12.5" in rendered
    assert "ok=true" in rendered


def test_kv_quotes_values_with_spaces_and_escapes_newlines() -> None:
    rendered = kv(reason="two words", multi="line1\nline2")
    assert 'reason="two words"' in rendered
    assert "\\n" in rendered
    assert "\n" not in rendered


def test_kv_truncates_long_values() -> None:
    rendered = kv(note="x" * 500)
    assert "…(+200)" in rendered
    assert len(rendered) < 400


def test_kv_skips_none_and_summarizes_collections() -> None:
    rendered = kv(a=None, items=[1, 2, 3], mapping={"k": "v"})
    assert "a=" not in rendered
    assert "items=[3]" in rendered
    assert "mapping={1}" in rendered


def test_mask_keeps_prefix_for_correlation() -> None:
    assert mask("sk_abcdefghij").startswith("sk_a")
    assert "len=13" in mask("sk_abcdefghij")
    assert mask("") == "(empty)"
    assert mask("abc") == "…(len=3)"


def test_trace_id_is_per_context_and_restorable() -> None:
    original = get_trace_id()
    assigned = set_trace_id()
    assert assigned != original
    assert get_trace_id() == assigned
    assert set_trace_id("fixed-id") == "fixed-id"
    assert get_trace_id() == "fixed-id"


def test_setup_logging_emits_trace_id_and_event_name(capsys: pytest.CaptureFixture[str]) -> None:
    import io

    stream = io.StringIO()
    setup_logging("INFO", stream=stream)
    try:
        set_trace_id("abcd1234")
        get_logger("demo").info("llm.response %s", kv(model="m", elapsed_ms=12.0, api_key="secret"))
    finally:
        logging.getLogger().handlers.clear()

    output = stream.getvalue()
    assert "[t=abcd1234]" in output
    assert "sk.demo" in output
    assert "llm.response" in output
    assert "elapsed_ms=12" in output
    assert "secret" not in output
