"""运行状态采集（控制台用）。

不用 psutil：只为读一个常驻内存数字而引入编译型依赖不划算。
POSIX 读 ``/proc/self/statm``，Windows 走 Win32 API，都拿不到时返回
``None`` —— 控制台显示"—"即可，不该因为采集失败而报错。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from app import __version__
from app.logging import get_logger
from app.services import apikeys as apikey_service
from app.services import courses as course_service
from app.services import drafts as draft_service
from app.timeutil import now_utc

log = get_logger("status")


def process_rss_bytes() -> int | None:
    """当前进程常驻内存。"""
    if sys.platform.startswith("linux"):
        return _rss_linux()
    if os.name == "nt":
        return _rss_windows()
    return None


def _rss_linux() -> int | None:
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            resident_pages = int(handle.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def _rss_windows() -> int | None:  # pragma: no cover - 平台相关
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        # 必须显式声明 argtypes/restype：否则 ctypes 会把 64 位 HANDLE
        # 当 32 位传，调用静默失败并返回 None —— 这个坑实测踩过一次。
        get_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory_info.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        get_memory_info.restype = wintypes.BOOL

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if get_memory_info(handle, ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize)
        return None
    except Exception:  # noqa: BLE001 - 采集失败不该影响控制台
        return None


def _directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def database_size(data_dir: Path) -> int:
    """SQLite 主文件 + WAL + SHM 的总大小。"""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = data_dir / f"schedulekit.db{suffix}"
        if candidate.exists():
            try:
                total += candidate.stat().st_size
            except OSError:
                pass
    return total


def runtime_status(app) -> dict[str, Any]:
    """给控制台的状态快照。任何采集失败都降级为 None，不抛异常。"""
    cfg = app.state.config
    db = app.state.db
    data_dir = cfg.server.data_dir

    try:
        task_counts = _task_counts(db)
        draft_counts = _draft_counts(db)
    except Exception as exc:  # noqa: BLE001 - 库异常时控制台仍应能打开
        log.warning("status.query_failed %s", exc)
        task_counts, draft_counts = {}, {}

    return {
        "version": __version__,
        "config_path": str(cfg.path),
        "data_dir": str(data_dir),
        "timezone": cfg.server.timezone,
        "public_url": cfg.server.public_url,
        "server_time": now_utc().isoformat(timespec="seconds"),
        "rss_bytes": process_rss_bytes(),
        "db_bytes": database_size(data_dir),
        "uploads_bytes": _directory_size(data_dir / "uploads"),
        "tasks": task_counts,
        "drafts": draft_counts,
        "keys": apikey_service.counts(db),
        "courses": course_service.counts(db),
        "llm": {
            "provider": cfg.llm.provider,
            "base_url": cfg.llm.base_url,
            "model": cfg.llm.model,
            "api_key_set": cfg.llm.api_key_set,
            "ready": app.state.llm is not None,
            "error": app.state.llm_error,
        },
    }


def _task_counts(db) -> dict[str, int]:
    with db.connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status")
        counts = {row["status"]: row["n"] for row in rows}
        counts["ordered"] = conn.execute(
            "SELECT COUNT(*) FROM items WHERE due_at IS NOT NULL AND status = 'open'"
        ).fetchone()[0]
        counts["unordered"] = conn.execute(
            "SELECT COUNT(*) FROM items WHERE priority IS NOT NULL AND status = 'open'"
        ).fetchone()[0]
        return counts


def _draft_counts(db) -> dict[str, int]:
    with db.connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM ingest_drafts GROUP BY status")
        return {row["status"]: row["n"] for row in rows}


__all__ = ["runtime_status", "process_rss_bytes", "database_size", "draft_service"]
