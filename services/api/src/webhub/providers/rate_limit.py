from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from threading import Lock


class ProviderTestRateLimitError(RuntimeError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("provider connection test rate limit exceeded")
        self.retry_after = retry_after


class ProviderTestRateLimiter:
    def __init__(
        self,
        *,
        attempts: int,
        window_seconds: int,
        max_accounts: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min(attempts, window_seconds, max_accounts) < 1:
            raise ValueError("provider test rate limiter limits must be positive")
        self.attempts = attempts
        self.window_seconds = window_seconds
        self.max_accounts = max_accounts
        self._clock = clock
        self._attempts_by_account: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = Lock()

    def record(self, user_id: str) -> None:
        with self._lock:
            now = self._clock()
            threshold = now - self.window_seconds
            self._prune_empty(threshold)
            attempts = self._attempts_by_account.get(user_id)
            if attempts is None:
                if len(self._attempts_by_account) >= self.max_accounts:
                    oldest = next(iter(self._attempts_by_account.values()))
                    retry_after = max(
                        1,
                        math.ceil(self.window_seconds - (now - oldest[0])),
                    )
                    raise ProviderTestRateLimitError(retry_after)
                attempts = deque()
                self._attempts_by_account[user_id] = attempts
            while attempts and attempts[0] <= threshold:
                attempts.popleft()
            if len(attempts) >= self.attempts:
                retry_after = max(
                    1,
                    math.ceil(self.window_seconds - (now - attempts[0])),
                )
                raise ProviderTestRateLimitError(retry_after)
            attempts.append(now)
            self._attempts_by_account.move_to_end(user_id)

    def _prune_empty(self, threshold: float) -> None:
        for user_id, attempts in tuple(self._attempts_by_account.items()):
            while attempts and attempts[0] <= threshold:
                attempts.popleft()
            if attempts:
                continue
            self._attempts_by_account.pop(user_id, None)
