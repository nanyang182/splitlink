# -*- coding: utf-8 -*-
"""令牌桶限流：进程内实现，够单机用；多实例部署时换成 Redis 即可。"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, rate: int, per_seconds: float = 60.0, burst: int | None = None):
        """
        rate        每个周期补充的令牌数
        per_seconds 周期长度（秒）
        burst       桶容量，默认等于 rate
        """
        self.rate = rate
        self.per = per_seconds
        self.burst = burst or rate
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.burst), now))
            tokens = min(self.burst, tokens + (now - last) * self.rate / self.per)
            if tokens < 1:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1, now)
            return True

    def retry_after(self, key: str) -> int:
        """距离下一个令牌的秒数，用于 Retry-After 头。"""
        with self._lock:
            tokens, _ = self._buckets.get(key, (float(self.burst), time.monotonic()))
        return max(1, int((1 - tokens) * self.per / self.rate) + 1)

    def sweep(self, older_than: float = 600.0) -> None:
        """清掉长期不活跃的 key，避免内存无限增长。"""
        now = time.monotonic()
        with self._lock:
            stale = [k for k, (_, last) in self._buckets.items() if now - last > older_than]
            for k in stale:
                del self._buckets[k]
