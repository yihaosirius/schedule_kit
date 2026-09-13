"""M5 补充：运行状态采集。

``process_rss_bytes`` 曾经因为 ctypes 没声明 ``argtypes`` 而**静默返回 None**：
64 位 HANDLE 被当 32 位传，调用失败但 except 吞掉了，控制台上内存一栏永远显示"—"。
所以这里专门测"确实拿到了一个合理的正数"。
"""

from __future__ import annotations

import sys

import pytest

from app.status import database_size, process_rss_bytes, runtime_status


def test_process_rss_returns_a_plausible_positive_number() -> None:
    """断言"不是 None"才是关键 —— 采集失败会静默降级，光看返回类型发现不了。"""
    if not (sys.platform.startswith("linux") or sys.platform == "win32"):
        pytest.skip("该平台没有实现内存采集")

    rss = process_rss_bytes()
    assert rss is not None, "内存采集失败（返回 None）。检查 ctypes 是否声明了 argtypes/restype"
    assert rss > 0
    # 一个 Python 进程不可能小于 1MB，也不可能大于 16GB
    assert 1 * 1024 * 1024 < rss < 16 * 1024**3, f"内存数值不合理：{rss} 字节"


def test_database_size_counts_wal_and_shm(tmp_path) -> None:
    assert database_size(tmp_path) == 0

    (tmp_path / "schedulekit.db").write_bytes(b"x" * 100)
    (tmp_path / "schedulekit.db-wal").write_bytes(b"y" * 50)
    (tmp_path / "schedulekit.db-shm").write_bytes(b"z" * 25)
    (tmp_path / "无关文件.txt").write_bytes(b"w" * 999)

    assert database_size(tmp_path) == 175, "应统计主库 + WAL + SHM，且不含其它文件"


def test_runtime_status_shape(app) -> None:
    status = runtime_status(app)
    for key in (
        "version", "config_path", "data_dir", "timezone", "public_url",
        "server_time", "rss_bytes", "db_bytes", "uploads_bytes",
        "tasks", "drafts", "keys", "courses", "llm",
    ):
        assert key in status, f"运行状态缺少 {key}"

    assert isinstance(status["tasks"], dict)
    assert isinstance(status["llm"], dict)
    assert status["llm"]["api_key_set"] is True


async def test_status_endpoint_reports_memory(session_client) -> None:
    """控制台上的内存数字必须有真值 —— 这是计划里的成功标准之一。"""
    body = (await session_client.get("/api/status")).json()
    if sys.platform.startswith("linux") or sys.platform == "win32":
        assert body["rss_bytes"] is not None, "内存采集失败"
        assert body["rss_bytes"] > 0


async def test_settings_page_shows_a_real_memory_number(session_client) -> None:
    """页面上不该出现"—"（filesize 过滤器对 None 的输出）。"""
    html = (await session_client.get("/settings")).text
    marker = html.split("常驻内存")[1][:80]
    assert "—" not in marker, f"内存显示为破折号，说明采集失败：{marker!r}"
    assert "MB" in marker or "GB" in marker or "KB" in marker
