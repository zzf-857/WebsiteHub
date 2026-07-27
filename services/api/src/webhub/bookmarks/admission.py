from __future__ import annotations

import asyncio
import hashlib
import math
import re
import shutil
import time
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Protocol

_PATH_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


class _DiskUsage(Protocol):
    free: int


class BookmarkUploadAdmissionError(Exception):
    code = "bookmark_upload_admission_failed"


class BookmarkUploadRateLimitError(BookmarkUploadAdmissionError):
    code = "bookmark_upload_rate_limited"

    def __init__(self, *, retry_after: int, scope: str = "account_rate") -> None:
        super().__init__(self.code)
        self.retry_after = max(1, retry_after)
        self.scope = scope


class BookmarkUploadConcurrencyError(BookmarkUploadRateLimitError):
    code = "bookmark_upload_busy"

    def __init__(self, *, scope: str) -> None:
        super().__init__(retry_after=1, scope=scope)


class BookmarkUploadQuotaExceededError(BookmarkUploadAdmissionError):
    code = "bookmark_upload_account_quota_exceeded"

    def __init__(self, *, used_bytes: int, requested_bytes: int, quota_bytes: int) -> None:
        super().__init__(self.code)
        self.used_bytes = used_bytes
        self.requested_bytes = requested_bytes
        self.quota_bytes = quota_bytes


class BookmarkUploadStorageUnavailableError(BookmarkUploadAdmissionError):
    code = "bookmark_upload_insufficient_storage"

    def __init__(
        self,
        *,
        available_free_bytes: int | None,
        requested_bytes: int,
        minimum_free_bytes: int,
    ) -> None:
        super().__init__(self.code)
        self.available_free_bytes = available_free_bytes
        self.requested_bytes = requested_bytes
        self.minimum_free_bytes = minimum_free_bytes


@dataclass(slots=True)
class _AccountState:
    attempts: deque[float] = field(default_factory=deque)
    active: bool = False


@dataclass(frozen=True, slots=True)
class BookmarkUploadAdmission:
    account_id: str
    existing_source_bytes: int
    declared_size_bytes: int | None
    _manager: BookmarkUploadAdmissionManager = field(repr=False)

    def guard_chunks(self, chunks: AsyncIterable[bytes]) -> AsyncIterator[bytes]:
        return self._manager.guard_chunks(self, chunks)


class BookmarkUploadAdmissionManager:
    def __init__(
        self,
        *,
        data_directory: Path,
        global_concurrency: int,
        rate_limit_attempts: int,
        rate_limit_window_seconds: int,
        max_tracked_accounts: int,
        account_quota_bytes: int,
        minimum_free_bytes: int,
        disk_check_interval_bytes: int,
        clock: Callable[[], float] = time.monotonic,
        disk_usage: Callable[[Path], _DiskUsage] = shutil.disk_usage,
    ) -> None:
        limits = {
            "global_concurrency": global_concurrency,
            "rate_limit_attempts": rate_limit_attempts,
            "rate_limit_window_seconds": rate_limit_window_seconds,
            "max_tracked_accounts": max_tracked_accounts,
            "account_quota_bytes": account_quota_bytes,
            "minimum_free_bytes": minimum_free_bytes,
            "disk_check_interval_bytes": disk_check_interval_bytes,
        }
        for name, value in limits.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if global_concurrency > max_tracked_accounts:
            raise ValueError("global_concurrency cannot exceed max_tracked_accounts")
        minimum_disk_reserve = global_concurrency * disk_check_interval_bytes
        if minimum_free_bytes < minimum_disk_reserve:
            raise ValueError(
                "minimum_free_bytes must be at least global_concurrency * disk_check_interval_bytes"
            )

        self.data_directory = Path(data_directory).expanduser().resolve()
        self.global_concurrency = global_concurrency
        self.rate_limit_attempts = rate_limit_attempts
        self.rate_limit_window_seconds = rate_limit_window_seconds
        self.max_tracked_accounts = max_tracked_accounts
        self.account_quota_bytes = account_quota_bytes
        self.minimum_free_bytes = minimum_free_bytes
        self.disk_check_interval_bytes = disk_check_interval_bytes
        self._clock = clock
        self._disk_usage = disk_usage
        self._states: dict[str, _AccountState] = {}
        self._active_uploads = 0
        self._state_lock = Lock()

    @property
    def active_upload_count(self) -> int:
        with self._state_lock:
            return self._active_uploads

    @property
    def tracked_account_count(self) -> int:
        with self._state_lock:
            return len(self._states)

    @asynccontextmanager
    async def admit(
        self,
        account_id: str,
        *,
        declared_size_bytes: int | None = None,
    ) -> AsyncIterator[BookmarkUploadAdmission]:
        normalized_account_id = self._validate_account_id(account_id)
        declared_size = self._validate_declared_size(declared_size_bytes)
        self._reserve(normalized_account_id)
        try:
            existing_source_bytes = await asyncio.to_thread(
                self._account_source_bytes,
                normalized_account_id,
            )
            self._ensure_quota(
                used_bytes=existing_source_bytes,
                requested_bytes=declared_size or 0,
            )
            await self._disk_budget(declared_size or 0)
            yield BookmarkUploadAdmission(
                account_id=normalized_account_id,
                existing_source_bytes=existing_source_bytes,
                declared_size_bytes=declared_size,
                _manager=self,
            )
        finally:
            self._release(normalized_account_id)

    async def guard_chunks(
        self,
        admission: BookmarkUploadAdmission,
        chunks: AsyncIterable[bytes],
    ) -> AsyncIterator[bytes]:
        streamed_bytes = 0
        bytes_since_check = 0
        disk_budget: int | None = None
        async for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("Bookmark upload chunks must be bytes")
            chunk_bytes = len(chunk)
            self._ensure_quota(
                used_bytes=admission.existing_source_bytes + streamed_bytes,
                requested_bytes=chunk_bytes,
            )
            if (
                disk_budget is None
                or bytes_since_check + chunk_bytes >= self.disk_check_interval_bytes
                or chunk_bytes > disk_budget
            ):
                disk_budget = await self._disk_budget(chunk_bytes)
                bytes_since_check = 0
            if chunk_bytes > disk_budget:
                raise BookmarkUploadStorageUnavailableError(
                    available_free_bytes=disk_budget + self.minimum_free_bytes,
                    requested_bytes=chunk_bytes,
                    minimum_free_bytes=self.minimum_free_bytes,
                )

            yield chunk
            streamed_bytes += chunk_bytes
            bytes_since_check += chunk_bytes
            disk_budget -= chunk_bytes

    def _reserve(self, account_id: str) -> None:
        with self._state_lock:
            now = self._clock()
            self._prune_locked(now)
            state = self._states.get(account_id)
            if state is not None:
                self._discard_expired(state.attempts, now)
                if state.active:
                    raise BookmarkUploadConcurrencyError(scope="account_concurrency")
                if len(state.attempts) >= self.rate_limit_attempts:
                    raise BookmarkUploadRateLimitError(
                        retry_after=self._retry_after(state, now),
                    )
            if self._active_uploads >= self.global_concurrency:
                raise BookmarkUploadConcurrencyError(scope="global_concurrency")
            if state is None:
                if len(self._states) >= self.max_tracked_accounts:
                    raise BookmarkUploadRateLimitError(
                        retry_after=self._capacity_retry_after(now),
                        scope="state_capacity",
                    )
                state = self._states[account_id] = _AccountState()

            state.attempts.append(now)
            state.active = True
            self._active_uploads += 1

    def _release(self, account_id: str) -> None:
        with self._state_lock:
            state = self._states.get(account_id)
            if state is None or not state.active:
                return
            state.active = False
            self._active_uploads -= 1
            now = self._clock()
            self._discard_expired(state.attempts, now)
            if not state.attempts:
                self._states.pop(account_id, None)

    def _prune_locked(self, now: float) -> None:
        for account_id, state in list(self._states.items()):
            self._discard_expired(state.attempts, now)
            if not state.active and not state.attempts:
                self._states.pop(account_id, None)

    def _discard_expired(self, attempts: deque[float], now: float) -> None:
        threshold = now - self.rate_limit_window_seconds
        while attempts and attempts[0] <= threshold:
            attempts.popleft()

    def _retry_after(self, state: _AccountState, now: float) -> int:
        return max(
            1,
            math.ceil(self.rate_limit_window_seconds - (now - state.attempts[0])),
        )

    def _capacity_retry_after(self, now: float) -> int:
        oldest_attempt = min(
            (state.attempts[0] for state in self._states.values() if state.attempts),
            default=now,
        )
        return max(1, math.ceil(self.rate_limit_window_seconds - (now - oldest_attempt)))

    def _validate_account_id(self, value: str) -> str:
        if not isinstance(value, str) or _PATH_COMPONENT.fullmatch(value) is None:
            raise ValueError("account_id must be a safe path identifier")
        return value

    @staticmethod
    def _validate_declared_size(value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("declared_size_bytes must be a nonnegative integer or None")
        return value

    def _ensure_quota(self, *, used_bytes: int, requested_bytes: int) -> None:
        if used_bytes > self.account_quota_bytes or (
            requested_bytes > self.account_quota_bytes - used_bytes
        ):
            raise BookmarkUploadQuotaExceededError(
                used_bytes=used_bytes,
                requested_bytes=requested_bytes,
                quota_bytes=self.account_quota_bytes,
            )

    async def _disk_budget(self, requested_bytes: int) -> int:
        try:
            usage = await asyncio.to_thread(self._disk_usage, self._disk_usage_path())
            free_bytes = usage.free
            if isinstance(free_bytes, bool) or not isinstance(free_bytes, int) or free_bytes < 0:
                raise OSError("disk usage returned an invalid free-byte count")
        except OSError as error:
            raise BookmarkUploadStorageUnavailableError(
                available_free_bytes=None,
                requested_bytes=requested_bytes,
                minimum_free_bytes=self.minimum_free_bytes,
            ) from error

        budget = max(0, free_bytes - self.minimum_free_bytes)
        if requested_bytes > budget:
            raise BookmarkUploadStorageUnavailableError(
                available_free_bytes=free_bytes,
                requested_bytes=requested_bytes,
                minimum_free_bytes=self.minimum_free_bytes,
            )
        return budget

    def _disk_usage_path(self) -> Path:
        candidate = self.data_directory
        while not candidate.exists() and candidate.parent != candidate:
            candidate = candidate.parent
        return candidate

    def _account_source_bytes(self, account_id: str) -> int:
        imports_root = (self.data_directory / "bookmark-imports").resolve(strict=False)
        self._require_within(imports_root, self.data_directory)
        final_account = (imports_root / account_id).resolve(strict=False)
        self._require_within(final_account, imports_root)
        account_hash = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
        incoming_account = (imports_root / "incoming" / f"account-{account_hash}").resolve(
            strict=False
        )
        self._require_within(incoming_account, imports_root)
        return self._published_source_bytes(final_account) + self._incoming_source_bytes(
            incoming_account
        )

    def _published_source_bytes(self, account_directory: Path) -> int:
        if not account_directory.exists():
            return 0
        self._require_real_directory(account_directory)
        total = 0
        for snapshot_directory in account_directory.iterdir():
            if not snapshot_directory.is_dir() or self._is_link_like(snapshot_directory):
                continue
            total += self._safe_file_size(snapshot_directory / "source.html")
        return total

    def _incoming_source_bytes(self, account_directory: Path) -> int:
        if not account_directory.exists():
            return 0
        self._require_real_directory(account_directory)
        total = 0
        for upload_directory in account_directory.iterdir():
            if not upload_directory.is_dir() or self._is_link_like(upload_directory):
                continue
            total += self._safe_file_size(upload_directory / "source.part")
            total += self._safe_file_size(upload_directory / "source.html")
        return total

    def _safe_file_size(self, path: Path) -> int:
        try:
            if path.is_symlink() or not path.is_file():
                return 0
            return path.stat(follow_symlinks=False).st_size
        except FileNotFoundError:
            return 0
        except OSError as error:
            raise BookmarkUploadStorageUnavailableError(
                available_free_bytes=None,
                requested_bytes=0,
                minimum_free_bytes=self.minimum_free_bytes,
            ) from error

    def _require_real_directory(self, path: Path) -> None:
        if self._is_link_like(path) or not path.is_dir():
            raise BookmarkUploadStorageUnavailableError(
                available_free_bytes=None,
                requested_bytes=0,
                minimum_free_bytes=self.minimum_free_bytes,
            )

    @staticmethod
    def _is_link_like(path: Path) -> bool:
        return path.is_symlink() or path.is_junction()

    def _require_within(self, path: Path, root: Path) -> None:
        try:
            path.relative_to(root)
        except ValueError as error:
            raise BookmarkUploadStorageUnavailableError(
                available_free_bytes=None,
                requested_bytes=0,
                minimum_free_bytes=self.minimum_free_bytes,
            ) from error
