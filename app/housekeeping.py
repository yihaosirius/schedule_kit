"""周期性清理。

用进程内的 asyncio 任务而不是 cron：少一个部署部件，也不会因为
systemd 重启而遗留孤儿任务。任务随应用生命周期启停。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta

from app.logging import get_logger, kv
from app.services import drafts as draft_service
from app.timeutil import now_utc

log = get_logger("housekeeping")

DEFAULT_INTERVAL_SECONDS = 3600


async def run_forever(app, interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> None:
    """每小时清理一次过期草稿与超期图片。"""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(sweep, app)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 清理失败绝不能拖垮服务
            log.exception("housekeeping.failed")


def sweep(app) -> dict[str, int]:
    """执行一次清理，返回统计。同步实现，便于单测直接调用。"""
    cfg = app.state.config
    db = app.state.db

    removed_drafts = draft_service.cleanup_expired(db)
    removed_files = _purge_old_uploads(
        cfg.server.data_dir / "uploads", days=cfg.backup.upload_retention_days
    )

    stats = {"drafts": removed_drafts, "uploads": removed_files}
    if removed_drafts or removed_files:
        log.info("housekeeping.swept %s", kv(**stats))
    return stats


def _purge_old_uploads(uploads_dir, *, days: int) -> int:
    """删除超过保留期的图片。目录按日期命名，直接按名字判断。"""
    if days <= 0 or not uploads_dir.is_dir():
        return 0

    cutoff = (now_utc() - timedelta(days=days)).strftime("%Y-%m-%d")
    removed = 0
    for bucket in uploads_dir.iterdir():
        if not bucket.is_dir():
            continue
        if bucket.name >= cutoff:  # 目录名是 YYYY-MM-DD，字典序即时间序
            continue
        for image in bucket.iterdir():
            if image.is_file():
                with suppress(OSError):
                    image.unlink()
                    removed += 1
    return removed
