from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock


class SlidingWindowRateLimiter:
    def __init__(self, *, attempts: int = 5, window_seconds: int = 300) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._events: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def retry_after(self, key: str) -> int | None:
        now = time.monotonic()
        with self._lock:
            events = self._events[key]
            self._discard_expired(events, now)
            if len(events) < self.attempts:
                return None
            return max(1, int(self.window_seconds - (now - events[0])))

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            events = self._events[key]
            self._discard_expired(events, now)
            events.append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)

    def _discard_expired(self, events: deque[float], now: float) -> None:
        threshold = now - self.window_seconds
        while events and events[0] <= threshold:
            events.popleft()
