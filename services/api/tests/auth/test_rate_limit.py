from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from webhub.auth.rate_limit import (
    LoginRateLimiter,
    RateLimitExceededError,
    SlidingWindowRateLimiter,
)


def test_concurrent_reservations_cannot_pass_the_account_limit() -> None:
    limiter = LoginRateLimiter(account_attempts=5, client_attempts=100)
    barrier = Barrier(20)

    def reserve(index: int) -> bool:
        barrier.wait()
        try:
            limiter.reserve(client_key=f"client-{index}", account_key="shared-account")
        except RateLimitExceededError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=20) as executor:
        allowed = list(executor.map(reserve, range(20)))

    assert sum(allowed) == 5


def test_client_limit_caps_failures_across_distinct_accounts() -> None:
    limiter = LoginRateLimiter(account_attempts=5, client_attempts=3)

    for index in range(3):
        attempt = limiter.reserve(client_key="one-client", account_key=f"account-{index}")
        limiter.record_failure(attempt)

    with pytest.raises(RateLimitExceededError):
        limiter.reserve(client_key="one-client", account_key="another-account")


def test_expired_buckets_are_pruned_before_capacity_is_reused() -> None:
    now = 0.0

    def clock() -> float:
        return now

    limiter = SlidingWindowRateLimiter(
        attempts=1,
        window_seconds=10,
        max_keys=2,
        clock=clock,
    )
    for key in ("first", "second"):
        lease = limiter.reserve(key)
        limiter.record_failure(lease)

    with pytest.raises(RateLimitExceededError):
        limiter.reserve("third")

    now = 11.0
    lease = limiter.reserve("third")
    assert lease.key == "third"
    assert limiter.tracked_key_count == 1


def test_failed_second_bucket_reservation_rolls_back_the_client_lease() -> None:
    limiter = LoginRateLimiter(account_attempts=1, client_attempts=2)
    first = limiter.reserve(client_key="one-client", account_key="shared-account")

    with pytest.raises(RateLimitExceededError):
        limiter.reserve(client_key="one-client", account_key="shared-account")

    limiter.cancel(first)
    next_attempt = limiter.reserve(client_key="one-client", account_key="next-account")
    another_attempt = limiter.reserve(client_key="one-client", account_key="another-account")
    limiter.cancel(next_attempt)
    limiter.cancel(another_attempt)


def test_cancel_releases_both_pending_leases() -> None:
    limiter = LoginRateLimiter(account_attempts=1, client_attempts=1)
    cancelled = limiter.reserve(client_key="one-client", account_key="first-account")
    limiter.cancel(cancelled)

    replacement = limiter.reserve(client_key="one-client", account_key="second-account")
    limiter.cancel(replacement)
