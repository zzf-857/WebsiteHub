from __future__ import annotations

import asyncio
import hashlib
import os
import re
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from webhub.bookmarks import persistence
from webhub.bookmarks.uploads import StagedBookmarkUpload

_PATH_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_READ_BLOCK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class PublishedBookmarkFile:
    path: Path
    replayed: bool


def _path_component(value: str, *, field: str) -> str:
    if not isinstance(value, str) or _PATH_COMPONENT.fullmatch(value) is None:
        raise persistence.BookmarkPersistenceValidationError(f"{field}不是安全的路径标识")
    return value


def _require_within(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise persistence.BookmarkPersistenceValidationError("书签文件路径超出存储目录") from exc


def _data_root(data_directory: Path) -> Path:
    root = Path(data_directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve(strict=True)


def _incoming_account_directory(data_root: Path, account_id: str) -> Path:
    incoming = (data_root / "bookmark-imports" / "incoming").resolve(strict=False)
    _require_within(incoming, data_root)
    account_hash = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
    account_directory = (incoming / f"account-{account_hash}").resolve(strict=False)
    _require_within(account_directory, incoming)
    return account_directory


def _staged_source_path(
    staged_upload: StagedBookmarkUpload,
    *,
    data_root: Path,
    account_id: str,
) -> Path:
    candidate = Path(staged_upload.temporary_path)
    if not candidate.is_absolute():
        raise persistence.BookmarkPersistenceValidationError("暂存书签文件必须使用绝对路径")
    if candidate.is_symlink():
        raise persistence.BookmarkPersistenceValidationError("暂存书签文件不能是符号链接")

    source = candidate.resolve(strict=False)
    account_directory = _incoming_account_directory(data_root, account_id)
    _require_within(source, account_directory)
    if (
        source.name != "source.html"
        or source.parent.parent != account_directory
        or not source.parent.name.startswith("upload-")
    ):
        raise persistence.BookmarkPersistenceValidationError("暂存书签文件路径结构不合法")
    return source


def _ensure_real_directory(path: Path, *, root: Path) -> Path:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if _is_link_like_directory(path) or not path.is_dir():
            raise persistence.BookmarkPersistenceConflictError("书签目标目录不安全") from None
    resolved = path.resolve(strict=True)
    _require_within(resolved, root)
    return resolved


def _is_link_like_directory(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _require_real_directory(path: Path, *, root: Path) -> Path:
    if _is_link_like_directory(path) or not path.is_dir():
        raise persistence.BookmarkPersistenceConflictError("书签目标目录不安全")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise persistence.BookmarkPersistenceConflictError("书签目标目录不安全") from exc
    return resolved


def _destination_path(
    *,
    data_root: Path,
    account_id: str,
    snapshot_id: str,
    storage_key: str,
    create_directories: bool,
) -> Path:
    account_component = _path_component(account_id, field="账号标识")
    snapshot_component = _path_component(snapshot_id, field="快照标识")
    expected_storage_key = f"bookmark-imports/{account_component}/{snapshot_component}/source.html"
    if storage_key != expected_storage_key:
        raise persistence.BookmarkPersistenceConflictError("书签快照存储位置不合法")

    imports_candidate = data_root / "bookmark-imports"
    if create_directories:
        imports_directory = _ensure_real_directory(imports_candidate, root=data_root)
        account_directory = _ensure_real_directory(
            imports_directory / account_component,
            root=imports_directory,
        )
        snapshot_directory = _ensure_real_directory(
            account_directory / snapshot_component,
            root=account_directory,
        )
    else:
        imports_directory = _require_real_directory(imports_candidate, root=data_root)
        account_directory = _require_real_directory(
            imports_directory / account_component,
            root=imports_directory,
        )
        snapshot_directory = _require_real_directory(
            account_directory / snapshot_component,
            root=account_directory,
        )

    destination = (snapshot_directory / "source.html").resolve(strict=False)
    _require_within(destination, snapshot_directory)
    if destination.parent != snapshot_directory:
        raise persistence.BookmarkPersistenceConflictError("书签快照存储位置不合法")
    return destination


def _validated_metadata(staged_upload: StagedBookmarkUpload) -> tuple[str, int]:
    source_hash = staged_upload.source_sha256.strip().casefold()
    if _SHA256.fullmatch(source_hash) is None:
        raise persistence.BookmarkPersistenceValidationError("暂存书签文件摘要不合法")
    source_size = staged_upload.source_size_bytes
    if isinstance(source_size, bool) or not isinstance(source_size, int) or source_size <= 0:
        raise persistence.BookmarkPersistenceValidationError("暂存书签文件大小不合法")
    if staged_upload.source_format != "netscape_html":
        raise persistence.BookmarkPersistenceValidationError("暂存书签文件格式不受支持")
    return source_hash, source_size


async def _file_facts(path: Path) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise persistence.BookmarkPersistenceConflictError("书签文件不存在或类型不安全")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(_READ_BLOCK_BYTES):
            digest.update(chunk)
            size += len(chunk)
            await asyncio.sleep(0)
    return digest.hexdigest(), size


async def _assert_file_matches(path: Path, *, source_hash: str, source_size: int) -> None:
    actual_hash, actual_size = await _file_facts(path)
    if actual_hash != source_hash or actual_size != source_size:
        raise persistence.BookmarkPersistenceConflictError("书签文件内容与快照摘要不一致")


def _prune_staged_job(source: Path, account_directory: Path) -> None:
    job_directory = source.parent
    if job_directory.parent != account_directory:
        return
    with suppress(OSError):
        job_directory.rmdir()


def discard_staged_upload(
    staged_upload: StagedBookmarkUpload,
    *,
    data_directory: Path,
    account_id: str,
) -> None:
    data_root = _data_root(data_directory)
    source = _staged_source_path(
        staged_upload,
        data_root=data_root,
        account_id=account_id,
    )
    account_directory = _incoming_account_directory(data_root, account_id)
    with suppress(FileNotFoundError):
        source.unlink()
    _prune_staged_job(source, account_directory)


async def publish_staged_upload(
    staged_upload: StagedBookmarkUpload,
    *,
    data_directory: Path,
    account_id: str,
    snapshot_id: str,
    storage_key: str,
    allow_create: bool,
) -> PublishedBookmarkFile:
    source_hash, source_size = _validated_metadata(staged_upload)
    data_root = _data_root(data_directory)
    source = _staged_source_path(
        staged_upload,
        data_root=data_root,
        account_id=account_id,
    )
    destination = _destination_path(
        data_root=data_root,
        account_id=account_id,
        snapshot_id=snapshot_id,
        storage_key=storage_key,
        create_directories=allow_create,
    )

    if destination.is_symlink():
        raise persistence.BookmarkPersistenceConflictError("书签目标文件不能是符号链接")
    if destination.exists():
        await _assert_file_matches(
            destination,
            source_hash=source_hash,
            source_size=source_size,
        )
        discard_staged_upload(
            staged_upload,
            data_directory=data_root,
            account_id=account_id,
        )
        return PublishedBookmarkFile(path=destination.resolve(strict=True), replayed=True)

    if not allow_create:
        raise persistence.BookmarkPersistenceConflictError("已排队的书签任务缺少源文件")
    if not source.exists():
        raise persistence.BookmarkPersistenceConflictError("暂存书签文件不存在")
    await _assert_file_matches(source, source_hash=source_hash, source_size=source_size)

    try:
        os.link(source, destination, follow_symlinks=False)
    except FileExistsError:
        await _assert_file_matches(
            destination,
            source_hash=source_hash,
            source_size=source_size,
        )
        replayed = True
    else:
        replayed = False

    source.unlink()
    account_directory = _incoming_account_directory(data_root, account_id)
    _prune_staged_job(source, account_directory)
    resolved_destination = destination.resolve(strict=True)
    _require_within(resolved_destination, destination.parent)
    return PublishedBookmarkFile(path=resolved_destination, replayed=replayed)


async def intake_bookmark_upload(
    session: AsyncSession,
    *,
    data_directory: Path,
    account_id: str,
    staged_upload: StagedBookmarkUpload,
    idempotency_key: str,
) -> persistence.ImportJobResult:
    source_hash, source_size = _validated_metadata(staged_upload)
    _path_component(account_id, field="账号标识")
    data_root = _data_root(data_directory)
    _staged_source_path(
        staged_upload,
        data_root=data_root,
        account_id=account_id,
    )
    try:
        import_job = await persistence.create_import(
            session,
            account_id,
            source_sha256=source_hash,
            source_size_bytes=source_size,
            original_filename=staged_upload.display_filename,
            idempotency_key=idempotency_key,
            detected_encoding=staged_upload.encoding,
            ready_for_parse=False,
        )
        # Replay lookups autobegin a read transaction; release it before hashing large files.
        if session.in_transaction():
            await session.rollback()
        published = await publish_staged_upload(
            staged_upload,
            data_directory=data_root,
            account_id=account_id,
            snapshot_id=import_job.snapshot_id,
            storage_key=import_job.storage_key,
            allow_create=import_job.state == "receiving",
        )
        if import_job.state != "receiving":
            return replace(import_job, replayed=True)

        queued = await persistence.queue_import_for_parse(
            session,
            account_id,
            import_job.job_id,
            expected_job_version=import_job.job_version,
        )
        return replace(
            queued,
            replayed=import_job.replayed or published.replayed or queued.replayed,
        )
    except BaseException:
        with suppress(Exception):
            discard_staged_upload(
                staged_upload,
                data_directory=data_root,
                account_id=account_id,
            )
        raise
