import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from webhub.bookmarks.admission import (
    BookmarkUploadAdmissionManager,
    BookmarkUploadConcurrencyError,
    BookmarkUploadQuotaExceededError,
    BookmarkUploadRateLimitError,
    BookmarkUploadStorageUnavailableError,
)


@dataclass
class _Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass(frozen=True)
class _Usage:
    free: int


def _manager(
    data_directory: Path,
    *,
    global_concurrency: int = 2,
    rate_limit_attempts: int = 20,
    rate_limit_window_seconds: int = 10,
    max_tracked_accounts: int = 10,
    account_quota_bytes: int = 10_000,
    minimum_free_bytes: int = 100,
    disk_check_interval_bytes: int = 10,
    clock: Callable[[], float] | None = None,
    disk_usage: Callable[[Path], _Usage] | None = None,
) -> BookmarkUploadAdmissionManager:
    selected_disk_usage = disk_usage or (lambda _: _Usage(free=100_000))
    return BookmarkUploadAdmissionManager(
        data_directory=data_directory,
        global_concurrency=global_concurrency,
        rate_limit_attempts=rate_limit_attempts,
        rate_limit_window_seconds=rate_limit_window_seconds,
        max_tracked_accounts=max_tracked_accounts,
        account_quota_bytes=account_quota_bytes,
        minimum_free_bytes=minimum_free_bytes,
        disk_check_interval_bytes=disk_check_interval_bytes,
        clock=clock or _Clock(),
        disk_usage=selected_disk_usage,
    )


async def _chunks(values: list[bytes]) -> AsyncIterator[bytes]:
    for value in values:
        yield value


def _write_bytes(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_account_and_global_concurrency_limits_are_distinct(tmp_path: Path) -> None:
    manager = _manager(tmp_path, global_concurrency=1)

    async def scenario() -> None:
        async with manager.admit("alice"):
            with pytest.raises(BookmarkUploadConcurrencyError) as account_error:
                async with manager.admit("alice"):
                    pass
            assert account_error.value.scope == "account_concurrency"

            with pytest.raises(BookmarkUploadConcurrencyError) as global_error:
                async with manager.admit("bob"):
                    pass
            assert global_error.value.scope == "global_concurrency"
            assert manager.active_upload_count == 1

        assert manager.active_upload_count == 0

    asyncio.run(scenario())


def test_slots_are_released_after_normal_and_exceptional_exit(tmp_path: Path) -> None:
    manager = _manager(tmp_path, global_concurrency=1)

    async def scenario() -> None:
        async with manager.admit("alice"):
            pass
        assert manager.active_upload_count == 0

        with pytest.raises(RuntimeError, match="upload failed"):
            async with manager.admit("bob"):
                raise RuntimeError("upload failed")
        assert manager.active_upload_count == 0

        async with manager.admit("carol"):
            assert manager.active_upload_count == 1

    asyncio.run(scenario())


def test_slot_is_released_after_task_cancellation(tmp_path: Path) -> None:
    manager = _manager(tmp_path, global_concurrency=1)

    async def scenario() -> None:
        entered = asyncio.Event()
        never = asyncio.Event()

        async def stalled_upload() -> None:
            async with manager.admit("alice"):
                entered.set()
                await never.wait()

        task = asyncio.create_task(stalled_upload())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert manager.active_upload_count == 0
        async with manager.admit("alice"):
            assert manager.active_upload_count == 1

    asyncio.run(scenario())


def test_short_window_rate_limit_reports_retry_after(tmp_path: Path) -> None:
    clock = _Clock()
    manager = _manager(
        tmp_path,
        rate_limit_attempts=2,
        rate_limit_window_seconds=10,
        clock=clock,
    )

    async def scenario() -> None:
        async with manager.admit("alice"):
            pass
        clock.now = 2.0
        async with manager.admit("alice"):
            pass

        clock.now = 3.0
        with pytest.raises(BookmarkUploadRateLimitError) as rate_error:
            async with manager.admit("alice"):
                pass
        assert rate_error.value.scope == "account_rate"
        assert rate_error.value.retry_after == 7

        clock.now = 10.0
        async with manager.admit("alice"):
            pass

    asyncio.run(scenario())


def test_tracked_account_state_is_bounded_and_expired_entries_are_pruned(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    manager = _manager(
        tmp_path,
        max_tracked_accounts=2,
        rate_limit_window_seconds=10,
        clock=clock,
    )

    async def scenario() -> None:
        async with manager.admit("alice"):
            pass
        clock.now = 1.0
        async with manager.admit("bob"):
            pass
        assert manager.tracked_account_count == 2

        with pytest.raises(BookmarkUploadRateLimitError) as capacity_error:
            async with manager.admit("carol"):
                pass
        assert capacity_error.value.scope == "state_capacity"
        assert capacity_error.value.retry_after == 9
        assert manager.tracked_account_count == 2

        clock.now = 10.0
        async with manager.admit("carol"):
            pass
        assert manager.tracked_account_count == 2

    asyncio.run(scenario())


def test_published_and_stale_incoming_sources_count_toward_account_quota(
    tmp_path: Path,
) -> None:
    _write_bytes(tmp_path / "bookmark-imports" / "alice" / "snapshot-1" / "source.html", 5)
    _write_bytes(tmp_path / "bookmark-imports" / "alice" / "snapshot-2" / "source.html", 7)
    account_hash = hashlib.sha256(b"alice").hexdigest()
    incoming = tmp_path / "bookmark-imports" / "incoming" / f"account-{account_hash}"
    _write_bytes(incoming / "upload-stale" / "source.part", 3)
    _write_bytes(incoming / "upload-staged" / "source.html", 4)
    manager = _manager(tmp_path, account_quota_bytes=20)

    async def scenario() -> None:
        async with manager.admit("alice", declared_size_bytes=1) as admission:
            assert admission.existing_source_bytes == 19

        with pytest.raises(BookmarkUploadQuotaExceededError) as quota_error:
            async with manager.admit("alice", declared_size_bytes=2):
                pass
        assert quota_error.value.used_bytes == 19
        assert quota_error.value.requested_bytes == 2
        assert quota_error.value.quota_bytes == 20
        assert manager.active_upload_count == 0

    asyncio.run(scenario())


def test_stream_can_end_with_an_empty_chunk_at_the_exact_quota_boundary(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, account_quota_bytes=5)

    async def scenario() -> None:
        async with manager.admit("alice", declared_size_bytes=5) as admission:
            forwarded = [chunk async for chunk in admission.guard_chunks(_chunks([b"12345", b""]))]
        assert forwarded == [b"12345", b""]

    asyncio.run(scenario())


def test_declared_size_rejects_insufficient_disk_before_streaming(tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        minimum_free_bytes=100,
        disk_usage=lambda _: _Usage(free=150),
    )

    async def scenario() -> None:
        with pytest.raises(BookmarkUploadStorageUnavailableError) as storage_error:
            async with manager.admit("alice", declared_size_bytes=51):
                pass
        assert storage_error.value.available_free_bytes == 150
        assert storage_error.value.requested_bytes == 51
        assert storage_error.value.minimum_free_bytes == 100
        assert manager.active_upload_count == 0

    asyncio.run(scenario())


def test_stream_rechecks_disk_and_stops_before_an_unsafe_chunk(tmp_path: Path) -> None:
    free_values = iter((1_000, 106))
    calls: list[Path] = []

    def disk_usage(path: Path) -> _Usage:
        calls.append(path)
        return _Usage(free=next(free_values))

    manager = _manager(tmp_path, minimum_free_bytes=100, disk_usage=disk_usage)

    async def scenario() -> None:
        async with manager.admit("alice") as admission:
            with pytest.raises(BookmarkUploadStorageUnavailableError) as storage_error:
                async for _ in admission.guard_chunks(_chunks([b"1234567"])):
                    pass
            assert storage_error.value.available_free_bytes == 106
            assert storage_error.value.requested_bytes == 7

    asyncio.run(scenario())
    assert len(calls) == 2


def test_guard_chunks_is_lazy_and_forwards_each_bytes_object_unchanged(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    values = [bytes(bytearray(b"alpha")), bytes(bytearray(b"beta"))]
    produced: list[bytes] = []

    async def source() -> AsyncIterator[bytes]:
        for value in values:
            produced.append(value)
            yield value

    async def scenario() -> None:
        async with manager.admit("alice") as admission:
            guarded = admission.guard_chunks(source())
            first = await anext(guarded)
            assert first is values[0]
            assert produced == [values[0]]

            second = await anext(guarded)
            assert second is values[1]
            assert produced == values
            with pytest.raises(StopAsyncIteration):
                await anext(guarded)

    asyncio.run(scenario())


def test_disk_usage_checks_follow_the_configured_byte_interval(tmp_path: Path) -> None:
    calls: list[Path] = []

    def disk_usage(path: Path) -> _Usage:
        calls.append(path)
        return _Usage(free=100_000)

    manager = _manager(tmp_path, disk_check_interval_bytes=10, disk_usage=disk_usage)
    values = [bytes([index]) * 3 for index in range(7)]

    async def scenario() -> None:
        async with manager.admit("alice") as admission:
            forwarded = [chunk async for chunk in admission.guard_chunks(_chunks(values))]
        assert forwarded == values

    asyncio.run(scenario())
    assert len(calls) == 4


def test_minimum_disk_reserve_covers_every_concurrent_check_interval(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="must be at least"):
        _manager(
            tmp_path,
            global_concurrency=3,
            minimum_free_bytes=29,
            disk_check_interval_bytes=10,
        )

    manager = _manager(
        tmp_path,
        global_concurrency=3,
        minimum_free_bytes=30,
        disk_check_interval_bytes=10,
    )
    assert manager.minimum_free_bytes == 30
