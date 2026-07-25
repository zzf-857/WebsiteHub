from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock


class RateLimitExceededError(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limit exceeded")
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class RateLimitLease:
    key: str
    token: int


@dataclass(slots=True)
class _Bucket:
    failures: deque[float] = field(default_factory=deque)
    pending: set[int] = field(default_factory=set)


class SlidingWindowRateLimiter:
    def __init__(
        self,
        *,
        attempts: int,
        window_seconds: int,
        max_keys: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min(attempts, window_seconds, max_keys) < 1:
            raise ValueError("rate limiter limits must be positive")
        self.attempts = attempts
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._failure_order: OrderedDict[str, float] = OrderedDict()
        self._next_token = 0
        self._lock = Lock()

    @property
    def tracked_key_count(self) -> int:
        with self._lock:
            return len(self._buckets)

    def reserve(self, key: str) -> RateLimitLease:
        with self._lock:
            now = self._clock()
            self._prune_expired(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self.max_keys:
                    raise RateLimitExceededError(self._capacity_retry_after(now))
                bucket = self._buckets[key] = _Bucket()
            self._discard_expired(bucket.failures, now)
            if len(bucket.failures) + len(bucket.pending) >= self.attempts:
                raise RateLimitExceededError(self._retry_after(bucket, now))

            self._next_token += 1
            lease = RateLimitLease(key=key, token=self._next_token)
            bucket.pending.add(lease.token)
            return lease

    def record_failure(self, lease: RateLimitLease) -> None:
        with self._lock:
            now = self._clock()
            bucket = self._consume(lease)
            self._discard_expired(bucket.failures, now)
            bucket.failures.append(now)
            self._failure_order[lease.key] = now
            self._failure_order.move_to_end(lease.key)

    def release(self, lease: RateLimitLease) -> None:
        with self._lock:
            bucket = self._consume(lease)
            if not bucket.pending and not bucket.failures:
                self._buckets.pop(lease.key, None)
                self._failure_order.pop(lease.key, None)

    def reset(self, lease: RateLimitLease) -> None:
        with self._lock:
            bucket = self._consume(lease)
            bucket.failures.clear()
            self._failure_order.pop(lease.key, None)
            if not bucket.pending:
                self._buckets.pop(lease.key, None)

    def retry_after(self, key: str) -> int | None:
        with self._lock:
            now = self._clock()
            self._prune_expired(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                return None
            self._discard_expired(bucket.failures, now)
            if len(bucket.failures) + len(bucket.pending) < self.attempts:
                return None
            return self._retry_after(bucket, now)

    def _consume(self, lease: RateLimitLease) -> _Bucket:
        bucket = self._buckets.get(lease.key)
        if bucket is None or lease.token not in bucket.pending:
            raise RuntimeError("rate limit lease is no longer active")
        bucket.pending.remove(lease.token)
        return bucket

    def _prune_expired(self, now: float) -> None:
        threshold = now - self.window_seconds
        while self._failure_order:
            key, last_failure = next(iter(self._failure_order.items()))
            if last_failure > threshold:
                break
            self._failure_order.popitem(last=False)
            bucket = self._buckets.get(key)
            if bucket is None:
                continue
            self._discard_expired(bucket.failures, now)
            if not bucket.pending and not bucket.failures:
                self._buckets.pop(key, None)

    def _capacity_retry_after(self, now: float) -> int:
        if not self._failure_order:
            return 1
        _, last_failure = next(iter(self._failure_order.items()))
        return max(1, math.ceil(self.window_seconds - (now - last_failure)))

    def _retry_after(self, bucket: _Bucket, now: float) -> int:
        if len(bucket.failures) < self.attempts:
            return 1
        return max(1, math.ceil(self.window_seconds - (now - bucket.failures[0])))

    def _discard_expired(self, failures: deque[float], now: float) -> None:
        threshold = now - self.window_seconds
        while failures and failures[0] <= threshold:
            failures.popleft()


@dataclass(frozen=True, slots=True)
class LoginAttempt:
    client: RateLimitLease
    account: RateLimitLease


class LoginRateLimiter:
    def __init__(
        self,
        *,
        account_attempts: int = 5,
        client_attempts: int = 20,
        window_seconds: int = 300,
        max_account_keys: int = 10_000,
        max_client_keys: int = 2_048,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._account = SlidingWindowRateLimiter(
            attempts=account_attempts,
            window_seconds=window_seconds,
            max_keys=max_account_keys,
            clock=clock,
        )
        self._client = SlidingWindowRateLimiter(
            attempts=client_attempts,
            window_seconds=window_seconds,
            max_keys=max_client_keys,
            clock=clock,
        )

    def reserve(self, *, client_key: str, account_key: str) -> LoginAttempt:
        client_lease = self._client.reserve(client_key)
        try:
            account_lease = self._account.reserve(account_key)
        except BaseException:
            self._client.release(client_lease)
            raise
        return LoginAttempt(client=client_lease, account=account_lease)

    def record_failure(self, attempt: LoginAttempt) -> None:
        try:
            self._account.record_failure(attempt.account)
        finally:
            self._client.record_failure(attempt.client)

    def record_success(self, attempt: LoginAttempt) -> None:
        try:
            self._account.reset(attempt.account)
        finally:
            self._client.release(attempt.client)

    def cancel(self, attempt: LoginAttempt) -> None:
        try:
            self._account.release(attempt.account)
        finally:
            self._client.release(attempt.client)
