from __future__ import annotations

import asyncio
import codecs
import hashlib
import os
import re
import unicodedata
from collections.abc import AsyncIterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from webhub.bookmarks.models import BookmarkFormatError, ParserLimits

_CHARSET_PATTERN = re.compile(rb"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", re.IGNORECASE)
_NETSCAPE_MARKER = "NETSCAPE-BOOKMARK-FILE-1"
_PROBE_BYTES = 16 * 1024
_WRITE_BLOCK_BYTES = 64 * 1024
_SOURCE_FORMAT = "netscape_html"
_DEFAULT_DISPLAY_FILENAME = "bookmarks.html"


class BookmarkUploadTooLargeError(BookmarkFormatError):
    """Raised when the streamed upload exceeds its configured byte limit."""


@dataclass(frozen=True, slots=True)
class StagedBookmarkUpload:
    temporary_path: Path
    source_sha256: str
    source_size_bytes: int
    display_filename: str
    source_format: str
    encoding: str


def _safe_display_filename(value: str | None) -> str:
    if value is None:
        return _DEFAULT_DISPLAY_FILENAME
    if not isinstance(value, str):
        raise TypeError("original_filename must be a string or None")

    normalized = unicodedata.normalize("NFKC", value)
    leaf = normalized.replace("\\", "/").rsplit("/", 1)[-1]
    visible = "".join(
        character for character in leaf if not unicodedata.category(character).startswith("C")
    )
    display = " ".join(visible.split())[:255].strip()
    if display in {"", ".", ".."}:
        return _DEFAULT_DISPLAY_FILENAME
    return display


def _detect_netscape_format(probe: bytes, *, complete_file: bool) -> str:
    if probe.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    elif probe.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    else:
        match = _CHARSET_PATTERN.search(probe)
        declared = match.group(1).decode("ascii", errors="ignore") if match else "utf-8"
        try:
            encoding = codecs.lookup(declared).name
        except LookupError as exc:
            raise BookmarkFormatError(f"Unsupported bookmark encoding: {declared}") from exc

    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    try:
        decoded_probe = decoder.decode(probe, final=complete_file)
    except UnicodeDecodeError as exc:
        raise BookmarkFormatError(f"Bookmark file is not valid {encoding}") from exc
    if _NETSCAPE_MARKER not in decoded_probe.upper():
        raise BookmarkFormatError("Expected a Netscape Bookmark HTML export")
    return encoding


def _validate_decoded_bytes(
    decoder: codecs.IncrementalDecoder,
    data: bytes,
    *,
    encoding: str,
    final: bool,
) -> None:
    try:
        decoder.decode(data, final=final)
    except UnicodeDecodeError as exc:
        raise BookmarkFormatError(f"Bookmark file is not valid {encoding}") from exc


def _ensure_directory(path: Path) -> bool:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if not path.is_dir():
            raise NotADirectoryError(f"Upload directory path is not a directory: {path}") from None
        return False
    return True


def _require_within(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Upload path escapes its storage root: {path}") from exc


def _write_all(destination: BinaryIO, data: bytes | bytearray | memoryview) -> None:
    remaining = memoryview(data)
    while remaining:
        written = destination.write(remaining)
        if written is None or written <= 0:
            raise OSError("Could not make progress while writing bookmark upload")
        remaining = remaining[written:]


async def _write_chunk(
    destination: BinaryIO,
    chunk: bytes,
    pending: bytearray,
) -> None:
    offset = 0
    if pending:
        take = min(_WRITE_BLOCK_BYTES - len(pending), len(chunk))
        pending.extend(chunk[:take])
        offset = take
        if len(pending) == _WRITE_BLOCK_BYTES:
            _write_all(destination, pending)
            pending.clear()
            await asyncio.sleep(0)

    while len(chunk) - offset >= _WRITE_BLOCK_BYTES:
        end = offset + _WRITE_BLOCK_BYTES
        _write_all(destination, memoryview(chunk)[offset:end])
        offset = end
        await asyncio.sleep(0)

    if offset < len(chunk):
        pending.extend(chunk[offset:])


def _cleanup_upload(
    partial_path: Path | None,
    staged_path: Path | None,
    created_directories: list[Path],
) -> None:
    for path in (partial_path, staged_path):
        if path is None:
            continue
        with suppress(OSError):
            path.unlink(missing_ok=True)
    for directory in reversed(created_directories):
        with suppress(OSError):
            directory.rmdir()


async def stage_bookmark_upload(
    chunks: AsyncIterable[bytes],
    *,
    data_directory: Path,
    account_id: str,
    original_filename: str | None = None,
    max_file_bytes: int | None = None,
) -> StagedBookmarkUpload:
    """Stream one browser export into an isolated, atomically published staging file."""
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("account_id must be a non-empty string")
    display_filename = _safe_display_filename(original_filename)

    default_maximum = ParserLimits().max_file_bytes
    selected_maximum = default_maximum if max_file_bytes is None else max_file_bytes
    if (
        isinstance(selected_maximum, bool)
        or not isinstance(selected_maximum, int)
        or not 1 <= selected_maximum <= default_maximum
    ):
        raise ValueError(f"max_file_bytes must be between 1 and {default_maximum}")

    data_root = Path(data_directory).expanduser().resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    data_root = data_root.resolve(strict=True)

    created_directories: list[Path] = []
    partial_path: Path | None = None
    staged_path: Path | None = None
    try:
        imports_directory = data_root / "bookmark-imports"
        if _ensure_directory(imports_directory):
            created_directories.append(imports_directory)
        imports_directory = imports_directory.resolve(strict=True)
        _require_within(imports_directory, data_root)

        incoming_directory = imports_directory / "incoming"
        if _ensure_directory(incoming_directory):
            created_directories.append(incoming_directory)
        incoming_directory = incoming_directory.resolve(strict=True)
        _require_within(incoming_directory, imports_directory)

        account_hash = hashlib.sha256(account_id.strip().encode("utf-8")).hexdigest()
        account_directory = incoming_directory / f"account-{account_hash}"
        if _ensure_directory(account_directory):
            created_directories.append(account_directory)
        account_directory = account_directory.resolve(strict=True)
        _require_within(account_directory, incoming_directory)

        while True:
            job_directory = account_directory / f"upload-{uuid4().hex}"
            try:
                job_directory.mkdir(mode=0o700)
            except FileExistsError:
                continue
            created_directories.append(job_directory)
            break
        job_directory = job_directory.resolve(strict=True)
        _require_within(job_directory, incoming_directory)

        partial_path = (job_directory / "source.part").resolve(strict=False)
        staged_path = (job_directory / "source.html").resolve(strict=False)
        _require_within(partial_path, incoming_directory)
        _require_within(staged_path, incoming_directory)

        digest = hashlib.sha256()
        probe = bytearray()
        pending = bytearray()
        source_size = 0
        encoding: str | None = None
        decoder: codecs.IncrementalDecoder | None = None
        open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(partial_path, open_flags, 0o600)
        with os.fdopen(descriptor, "wb", buffering=0) as destination:
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise TypeError("Bookmark upload chunks must be bytes")
                if len(chunk) > selected_maximum - source_size:
                    raise BookmarkUploadTooLargeError(
                        f"Bookmark upload exceeds the {selected_maximum}-byte limit"
                    )

                source_size += len(chunk)
                digest.update(chunk)
                probe_bytes = 0
                if len(probe) < _PROBE_BYTES:
                    probe_bytes = min(_PROBE_BYTES - len(probe), len(chunk))
                    probe.extend(chunk[:probe_bytes])
                    if len(probe) == _PROBE_BYTES:
                        encoding = _detect_netscape_format(
                            bytes(probe),
                            complete_file=False,
                        )
                        decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
                        _validate_decoded_bytes(
                            decoder,
                            bytes(probe),
                            encoding=encoding,
                            final=False,
                        )
                if decoder is not None and probe_bytes < len(chunk):
                    _validate_decoded_bytes(
                        decoder,
                        chunk[probe_bytes:],
                        encoding=encoding,
                        final=False,
                    )
                await _write_chunk(destination, chunk, pending)

            if source_size == 0:
                raise BookmarkFormatError("Bookmark file is empty")
            if pending:
                _write_all(destination, pending)
                pending.clear()
            await asyncio.sleep(0)

            if decoder is None:
                encoding = _detect_netscape_format(bytes(probe), complete_file=True)
            else:
                _validate_decoded_bytes(
                    decoder,
                    b"",
                    encoding=encoding,
                    final=True,
                )
            destination.flush()
            os.fsync(destination.fileno())

        os.replace(partial_path, staged_path)
        partial_path = None
        staged_path = staged_path.resolve(strict=True)
        _require_within(staged_path, incoming_directory)
        return StagedBookmarkUpload(
            temporary_path=staged_path,
            source_sha256=digest.hexdigest(),
            source_size_bytes=source_size,
            display_filename=display_filename,
            source_format=_SOURCE_FORMAT,
            encoding=encoding,
        )
    except BaseException:
        _cleanup_upload(partial_path, staged_path, created_directories)
        raise
