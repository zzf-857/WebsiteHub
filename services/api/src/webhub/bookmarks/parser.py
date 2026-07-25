from __future__ import annotations

import codecs
import hashlib
import re
from collections import deque
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path

from webhub.bookmarks.models import (
    BookmarkEvent,
    BookmarkFormatError,
    ParsedBookmark,
    ParsedFolder,
    ParserLimits,
    ParserStats,
)

_CHARSET_PATTERN = re.compile(rb"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", re.IGNORECASE)
_NETSCAPE_MARKER = "NETSCAPE-BOOKMARK-FILE-1"
PARSER_VERSION = "netscape-html.v2"


def _clean_text(value: str, limit: int) -> tuple[str, bool]:
    cleaned = " ".join(value.split())
    if len(cleaned) <= limit:
        return cleaned, False
    return cleaned[:limit].rstrip(), True


def _detect_encoding(prefix: bytes) -> str:
    if prefix.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"

    match = _CHARSET_PATTERN.search(prefix[:16_384])
    declared = match.group(1).decode("ascii", errors="ignore") if match else "utf-8"
    try:
        return codecs.lookup(declared).name
    except LookupError as exc:
        raise BookmarkFormatError(f"Unsupported bookmark encoding: {declared}") from exc


def _timestamp(value: str | None, stats: ParserStats) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError:
        stats.invalid_timestamp_count += 1
        return None
    if parsed < 0 or parsed > 32_503_680_000:
        stats.invalid_timestamp_count += 1
        return None
    return parsed


def _warn(stats: ParserStats, message: str) -> None:
    stats.warning_count += 1
    if len(stats.warnings) < 100:
        stats.warnings.append(message)


class _BookmarkHTMLParser(HTMLParser):
    def __init__(self, limits: ParserLimits, stats: ParserStats) -> None:
        super().__init__(convert_charrefs=True)
        self._limits = limits
        self._stats = stats
        self._records: deque[BookmarkEvent] = deque()
        self._folder_stack: list[ParsedFolder] = []
        self._dl_frames: list[bool] = []
        self._pending_folder: ParsedFolder | None = None
        self._capture_tag: str | None = None
        self._capture_parts: list[str] = []
        self._capture_size = 0
        self._capture_truncated = False
        self._capture_attrs: dict[str, str] = {}
        self._source_sequence = 0

    def feed(self, data: str) -> None:
        super().feed(data)
        if len(self.rawdata) > self._limits.max_buffered_tag_chars:
            raise BookmarkFormatError("A bookmark HTML tag exceeds the safe buffered-tag limit")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "dl":
            pushed = self._pending_folder is not None
            if pushed:
                assert self._pending_folder is not None
                self._folder_stack.append(self._pending_folder)
                self._pending_folder = None
                depth = len(self._folder_stack)
                if depth > self._limits.max_folder_depth:
                    raise BookmarkFormatError(
                        f"Bookmark folder depth exceeds {self._limits.max_folder_depth}"
                    )
                self._stats.max_folder_depth = max(self._stats.max_folder_depth, depth)
            self._dl_frames.append(pushed)
            return

        if tag not in {"a", "h3"}:
            return
        if self._capture_tag is not None:
            _warn(self._stats, f"Ignored nested <{tag}> inside <{self._capture_tag}>")
            return

        if tag == "a" and self._pending_folder is not None:
            _warn(self._stats, "Folder without a following <DL> was not added to the path")
            self._pending_folder = None

        selected: dict[str, str] = {}
        for name, value in attrs:
            normalized_name = name.casefold()
            safe_value = value or ""
            if normalized_name in {"href", "add_date", "last_modified"}:
                selected[normalized_name] = safe_value
            elif (
                tag == "a"
                and normalized_name in {"icon", "icon_uri"}
                and safe_value.startswith("data:")
            ):
                self._stats.inline_icon_count += 1
                self._stats.inline_icon_characters_ignored += len(safe_value)

        self._capture_tag = tag
        self._capture_parts = []
        self._capture_size = 0
        self._capture_truncated = False
        self._capture_attrs = selected

    def handle_data(self, data: str) -> None:
        if self._capture_tag is None or self._capture_truncated:
            return
        limit = (
            self._limits.max_title_chars
            if self._capture_tag == "a"
            else self._limits.max_folder_name_chars
        )
        remaining = limit + 1 - self._capture_size
        if remaining <= 0:
            self._capture_truncated = True
            return
        fragment = data[:remaining]
        self._capture_parts.append(fragment)
        self._capture_size += len(fragment)
        if len(data) > remaining or self._capture_size > limit:
            self._capture_truncated = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "dl":
            if not self._dl_frames:
                _warn(self._stats, "Ignored unmatched </DL>")
                return
            if self._dl_frames.pop() and self._folder_stack:
                self._folder_stack.pop()
            return

        if tag != self._capture_tag:
            return

        limit = self._limits.max_title_chars if tag == "a" else self._limits.max_folder_name_chars
        text, truncated = _clean_text("".join(self._capture_parts), limit)
        if truncated or self._capture_truncated:
            _warn(self._stats, f"Truncated overlong <{tag}> text")

        if tag == "h3":
            self._stats.folder_count += 1
            if self._stats.folder_count > self._limits.max_folders:
                raise BookmarkFormatError(
                    f"Bookmark folder count exceeds {self._limits.max_folders}"
                )
            self._source_sequence += 1
            title = text or "(untitled)"
            folder = ParsedFolder(
                source_folder_id=self._stats.folder_count,
                parent_source_folder_id=(
                    self._folder_stack[-1].source_folder_id if self._folder_stack else None
                ),
                source_order=self._stats.folder_count,
                source_sequence=self._source_sequence,
                title=title,
                folder_path=tuple(item.title for item in self._folder_stack) + (title,),
                depth=len(self._folder_stack) + 1,
            )
            self._pending_folder = folder
            self._records.append(folder)
        else:
            self._stats.bookmark_count += 1
            if self._stats.bookmark_count > self._limits.max_bookmarks:
                raise BookmarkFormatError(f"Bookmark count exceeds {self._limits.max_bookmarks}")
            self._source_sequence += 1
            raw_url = self._capture_attrs.get("href", "")
            issues: tuple[str, ...] = ()
            if len(raw_url) > self._limits.max_url_chars:
                raw_url = raw_url[: self._limits.max_url_chars]
                issues = ("url_too_long",)
                _warn(self._stats, "Rejected overlong bookmark URL")
            self._records.append(
                ParsedBookmark(
                    position=self._stats.bookmark_count,
                    source_sequence=self._source_sequence,
                    raw_url=raw_url,
                    title=text,
                    folder_path=tuple(item.title for item in self._folder_stack),
                    source_folder_id=(
                        self._folder_stack[-1].source_folder_id if self._folder_stack else None
                    ),
                    add_date=_timestamp(self._capture_attrs.get("add_date"), self._stats),
                    last_modified=_timestamp(self._capture_attrs.get("last_modified"), self._stats),
                    issues=issues,
                )
            )

        self._capture_tag = None
        self._capture_parts = []
        self._capture_size = 0
        self._capture_truncated = False
        self._capture_attrs = {}

    def drain(self) -> Iterator[BookmarkEvent]:
        while self._records:
            yield self._records.popleft()

    def finish(self) -> None:
        if self._capture_tag is not None:
            _warn(self._stats, f"Input ended inside <{self._capture_tag}>")
        if self._dl_frames:
            _warn(self._stats, "Input ended with unclosed <DL> elements")


def iter_netscape_events(
    source_path: Path,
    *,
    limits: ParserLimits | None = None,
    stats: ParserStats | None = None,
    chunk_size: int = 64 * 1024,
) -> Iterator[BookmarkEvent]:
    """Yield folder and bookmark events without retaining the complete file in memory."""
    selected_limits = limits or ParserLimits()
    selected_stats = stats or ParserStats()
    source_size = source_path.stat().st_size
    if source_size > selected_limits.max_file_bytes:
        raise BookmarkFormatError(
            f"Bookmark file is {source_size} bytes; limit is {selected_limits.max_file_bytes}"
        )
    selected_stats.source_size_bytes = source_size

    digest = hashlib.sha256()
    with source_path.open("rb") as source:
        probe = source.read(min(source_size, 16_384))
        if not probe:
            raise BookmarkFormatError("Bookmark file is empty")
        encoding = _detect_encoding(probe)
        selected_stats.encoding = encoding
        try:
            decoded_probe = probe.decode(encoding, errors="strict")
        except UnicodeDecodeError as exc:
            raise BookmarkFormatError(f"Bookmark file is not valid {encoding}") from exc
        if _NETSCAPE_MARKER not in decoded_probe[:16_384].upper():
            raise BookmarkFormatError("Expected a Netscape Bookmark HTML export")

        source.seek(0)
        decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        parser = _BookmarkHTMLParser(selected_limits, selected_stats)
        while chunk := source.read(chunk_size):
            digest.update(chunk)
            try:
                parser.feed(decoder.decode(chunk))
            except UnicodeDecodeError as exc:
                raise BookmarkFormatError(f"Bookmark file is not valid {encoding}") from exc
            yield from parser.drain()

        try:
            parser.feed(decoder.decode(b"", final=True))
        except UnicodeDecodeError as exc:
            raise BookmarkFormatError(f"Bookmark file is not valid {encoding}") from exc
        parser.close()
        parser.finish()
        yield from parser.drain()

    selected_stats.source_sha256 = digest.hexdigest()


def iter_netscape_bookmarks(
    source_path: Path,
    *,
    limits: ParserLimits | None = None,
    stats: ParserStats | None = None,
    chunk_size: int = 64 * 1024,
) -> Iterator[ParsedBookmark]:
    for event in iter_netscape_events(
        source_path,
        limits=limits,
        stats=stats,
        chunk_size=chunk_size,
    ):
        if isinstance(event, ParsedBookmark):
            yield event
