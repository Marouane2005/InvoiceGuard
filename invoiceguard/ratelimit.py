"""Small in-memory sliding-window rate limiter (per process)."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Callable


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window_seconds
        self.clock = clock
        self._hits: dict = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def count(self, key: str) -> int:
        """Hits in the current window, without adding one."""
        now = self.clock()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            return len(hits)

    def record(self, key: str) -> None:
        with self._lock:
            self._hits[key].append(self.clock())
