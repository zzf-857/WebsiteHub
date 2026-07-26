import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from webhub.bookmarks.models import BookmarkFormatError
from webhub.bookmarks.uploads import stage_bookmark_upload


def _export(body: bytes = b'<DT><A HREF="https://example.com">Example</A>') -> bytes:
    return (
        b"<!DOCTYPE NETSCAPE-Bookmark-file-1>\n"
        b'<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">\n'
        b"<DL><p>\n" + body + b"\n</DL><p>\n"
    )


async def _chunked(payload: bytes, chunk_size: int) -> AsyncIterator[bytes]:
    for offset in range(0, len(payload), chunk_size):
        yield payload[offset : offset + chunk_size]


def _assert_no_upload_artifacts(data_directory: Path) -> None:
    assert not (data_directory / "bookmark-imports").exists()


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 31, 65_536])
def test_stage_upload_is_independent_of_chunk_boundaries(
    tmp_path: Path,
    chunk_size: int,
) -> None:
    payload = _export()

    staged = asyncio.run(
        stage_bookmark_upload(
            _chunked(payload, chunk_size),
            data_directory=tmp_path,
            account_id="../../account/alice",
            original_filename="..\\private／folder\\bookmarks.html",
        )
    )

    incoming = (tmp_path / "bookmark-imports" / "incoming").resolve()
    assert staged.temporary_path.is_absolute()
    assert staged.temporary_path.resolve().is_relative_to(incoming)
    assert staged.temporary_path.name == "source.html"
    assert staged.temporary_path.read_bytes() == payload
    assert staged.source_sha256 == hashlib.sha256(payload).hexdigest()
    assert staged.source_size_bytes == len(payload)
    assert staged.display_filename == "bookmarks.html"
    assert staged.source_format == "netscape_html"
    assert staged.encoding == "utf-8"
    assert not list(incoming.rglob("*.part"))


def test_stage_upload_detects_utf16_bom_and_marker_split_across_chunks(tmp_path: Path) -> None:
    text = (
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>\n"
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-16">\n'
        '<DL><p><DT><A HREF="https://example.com">Example</A></DL><p>\n'
    )
    payload = text.encode("utf-16")

    staged = asyncio.run(
        stage_bookmark_upload(
            _chunked(payload, 1),
            data_directory=tmp_path,
            account_id="alice",
        )
    )

    assert staged.encoding == "utf-16"
    assert staged.temporary_path.read_bytes() == payload


def test_empty_upload_is_rejected_and_cleans_directories(tmp_path: Path) -> None:
    async def empty() -> AsyncIterator[bytes]:
        if False:
            yield b""

    with pytest.raises(BookmarkFormatError, match="empty"):
        asyncio.run(
            stage_bookmark_upload(
                empty(),
                data_directory=tmp_path,
                account_id="alice",
            )
        )

    _assert_no_upload_artifacts(tmp_path)


def test_oversized_upload_is_rejected_before_writing_excess_and_cleans(tmp_path: Path) -> None:
    payload = _export()

    with pytest.raises(BookmarkFormatError, match="17-byte limit"):
        asyncio.run(
            stage_bookmark_upload(
                _chunked(payload, 3),
                data_directory=tmp_path,
                account_id="alice",
                max_file_bytes=17,
            )
        )

    _assert_no_upload_artifacts(tmp_path)


def test_non_netscape_html_is_rejected_by_content_and_cleans(tmp_path: Path) -> None:
    payload = b'<html><a href="https://example.com">Example</a></html>'

    with pytest.raises(BookmarkFormatError, match="Netscape"):
        asyncio.run(
            stage_bookmark_upload(
                _chunked(payload, 2),
                data_directory=tmp_path,
                account_id="alice",
                original_filename="bookmarks.html",
            )
        )

    _assert_no_upload_artifacts(tmp_path)


def test_invalid_encoding_after_the_format_probe_is_rejected_and_cleans(
    tmp_path: Path,
) -> None:
    payload = _export() + (b" " * (20 * 1024)) + b"\xff"

    with pytest.raises(BookmarkFormatError, match="not valid"):
        asyncio.run(
            stage_bookmark_upload(
                _chunked(payload, 997),
                data_directory=tmp_path,
                account_id="alice",
            )
        )

    _assert_no_upload_artifacts(tmp_path)


def test_non_bytes_chunk_is_rejected_and_cleans(tmp_path: Path) -> None:
    async def malformed() -> AsyncIterator[bytes]:
        yield _export()[:20]
        yield "not bytes"  # type: ignore[misc]

    with pytest.raises(TypeError, match="chunks must be bytes"):
        asyncio.run(
            stage_bookmark_upload(
                malformed(),
                data_directory=tmp_path,
                account_id="alice",
            )
        )

    _assert_no_upload_artifacts(tmp_path)


def test_task_cancellation_removes_partial_file_and_empty_directories(tmp_path: Path) -> None:
    started = asyncio.Event()
    never = asyncio.Event()
    payload = _export(b" " * 70_000)

    async def stalled() -> AsyncIterator[bytes]:
        yield payload
        started.set()
        await never.wait()

    async def scenario() -> None:
        task = asyncio.create_task(
            stage_bookmark_upload(
                stalled(),
                data_directory=tmp_path,
                account_id="alice",
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    _assert_no_upload_artifacts(tmp_path)


def test_same_account_concurrent_uploads_use_unique_paths(tmp_path: Path) -> None:
    payloads = [_export(f"<DT><H3>Folder {index}</H3>".encode()) for index in range(12)]

    async def delayed_chunks(payload: bytes) -> AsyncIterator[bytes]:
        midpoint = len(payload) // 2
        yield payload[:midpoint]
        await asyncio.sleep(0)
        yield payload[midpoint:]

    async def scenario():
        return await asyncio.gather(
            *(
                stage_bookmark_upload(
                    delayed_chunks(payload),
                    data_directory=tmp_path,
                    account_id="same-account",
                    original_filename="bookmarks.html",
                )
                for payload in payloads
            )
        )

    staged_uploads = asyncio.run(scenario())

    paths = [staged.temporary_path for staged in staged_uploads]
    assert len(set(paths)) == len(payloads)
    assert [path.read_bytes() for path in paths] == payloads
