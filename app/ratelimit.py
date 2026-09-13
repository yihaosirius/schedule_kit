"""进程内滑动窗口限流。

单用户场景不需要 Redis：一个带锁的 dict 足够，且省掉一个常驻进程。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """按 key（通常是 IP 或 API Key）限制窗口内的请求次数。"""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, *, now: float | None = None) -> tuple[bool, float]:
        """记录一次访问并判断是否超限。

        返回 ``(allowed, retry_after_seconds)``；被拒时第二个值为建议等待秒数。
        """
        moment = now if now is not None else time.monotonic()
        cutoff = moment - self.window
        with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                retry_after = max(0.0, bucket[0] + self.window - moment)
                return False, round(retry_after, 1)
            bucket.append(moment)
            # 顺手回收空桶，避免长期运行后 key 无限增长
            if len(self._hits) > 1024:
                for stale in [k for k, v in self._hits.items() if not v]:
                    self._hits.pop(stale, None)
            return True, 0.0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


#: 登录尝试：5 次/分钟/IP（PLAN.md §7）
login_limiter = SlidingWindowLimiter(limit=5, window_seconds=60)

#: 智能录入：10 次/分钟/凭据
ingest_limiter = SlidingWindowLimiter(limit=10, window_seconds=60)
