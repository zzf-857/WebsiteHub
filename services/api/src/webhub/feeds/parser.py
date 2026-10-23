"""Turn untrusted feed bytes into bounded, plain-text items.

Parsing is delegated to ``feedparser`` (RSS 0.9x/1.0/2.0, Atom, iTunes tags).
It is only ever handed *bytes*: given a string it would treat it as a URL and
fetch it itself, bypassing the SSRF-checked outbound client.

Everything that reaches the database is bounded and inert:

* titles, summaries and authors are stripped of markup and length-capped;
* links (item, site, audio) must be absolute ``http(s)`` URLs, so a feed cannot
  smuggle ``javascript:`` hrefs into the page;
* at most ``MAX_ITEMS_PER_FETCH`` entries per fetch are kept.
"""

from __future__ import annotations

import calendar
import hashlib
import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import urlsplit

MAX_ITEMS_PER_FETCH = 50
MAX_TITLE_LENGTH = 300
MAX_SUMMARY_LENGTH = 600
MAX_AUTHOR_LENGTH = 200
MAX_GUID_LENGTH = 512
MAX_URL_LENGTH = 4_096
MAX_FEED_TITLE_LENGTH = 200

_TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")
_SCRIPT_BLOCK = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)

FeedKind = Literal["rss", "podcast"]


class FeedParseError(Exception):
    """The document is not a usable feed."""


@dataclass(frozen=True, slots=True)
class ParsedItem:
    guid: str
    url: str | None
    title: str
    summary: str
    author: str | None
    published_at: datetime | None
    audio_url: str | None
    duration_seconds: int | None
    image_url: str | None


@dataclass(frozen=True, slots=True)
class ParsedFeed:
    title: str
    site_url: str | None
    kind: FeedKind
    items: list[ParsedItem]
    image_url: str | None


def plain_text(value: object, *, limit: int) -> str:
    """Markup-free, whitespace-collapsed text of at most ``limit`` characters."""

    if not isinstance(value, str) or not value:
        return ""
    text = _SCRIPT_BLOCK.sub(" ", value)
    text = _TAG.sub(" ", text)
    text = _SPACE.sub(" ", html.unescape(text)).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def safe_link(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_URL_LENGTH:
        return None
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if any(ord(character) < 0x21 for character in candidate):
        return None
    return candidate


def parse_duration(value: object) -> int | None:
    """iTunes durations: ``3723``, ``62:03`` or ``1:02:03``."""

    if isinstance(value, int | float):
        seconds = int(value)
        return seconds if 0 < seconds < 100 * 3600 else None
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3 or not all(part.strip().isdigit() for part in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds if 0 < seconds < 100 * 3600 else None


def _timestamp(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)
            except (OverflowError, TypeError, ValueError):
                continue
    return None


def _audio(entry: Any) -> str | None:
    for enclosure in entry.get("enclosures") or []:
        kind = str(enclosure.get("type") or "").lower()
        href = safe_link(enclosure.get("href"))
        if href and (kind.startswith("audio/") or kind.startswith("video/") or not kind):
            return href
    return None


class _FirstImage(HTMLParser):
    """只读取首个 img 的 src，不执行 HTML 或加载任何资源。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.src: str | None = None
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "img" and not self.found:
            self.found = True
            self.src = safe_link(dict(attrs).get("src"))


def _channel_image(entry: Any) -> str | None:
    for key in ("image", "itunes_image"):
        value = entry.get(key)
        candidate = value.get("href") if isinstance(value, dict) else value
        if url := safe_link(candidate):
            return url
    return None


def _image(entry: Any) -> str | None:
    thumbnails = entry.get("media_thumbnail") or []
    if thumbnails and (url := safe_link(thumbnails[0].get("url"))):
        return url
    for content in entry.get("media_content") or []:
        is_image = content.get("medium") == "image" or str(
            content.get("type") or ""
        ).lower().startswith("image/")
        if is_image and (url := safe_link(content.get("url"))):
            return url
    if url := _channel_image(entry):
        return url
    for enclosure in entry.get("enclosures") or []:
        if str(enclosure.get("type") or "").lower().startswith("image/") and (
            url := safe_link(enclosure.get("href"))
        ):
            return url
    contents = entry.get("content") or []
    sources = [contents[0].get("value") if contents else None, entry.get("summary")]
    for source in sources:
        if isinstance(source, str):
            parser = _FirstImage()
            parser.feed(source)
            if parser.src:
                return parser.src
    return None


def _guid(entry: Any, url: str | None, title: str) -> str | None:
    raw = entry.get("id") or entry.get("guid")
    if isinstance(raw, str) and raw.strip():
        guid = raw.strip()
    elif url:
        guid = url
    elif title:
        guid = "title:" + title
    else:
        return None
    if len(guid) > MAX_GUID_LENGTH:
        # A hash keeps an over-long id stable and still unique per feed.
        guid = "sha256:" + hashlib.sha256(guid.encode("utf-8")).hexdigest()
    return guid


def parse_feed(document: bytes) -> ParsedFeed:
    import feedparser

    if not isinstance(document, bytes):  # pragma: no cover - defensive
        raise TypeError("parse_feed only accepts bytes")
    parsed = feedparser.parse(document)
    header = parsed.get("feed") or {}
    entries = parsed.get("entries") or []
    # ``version`` is empty when feedparser recognised no feed format (an HTML
    # page, a JSON error body); a feed with no entries yet is still a feed.
    if not parsed.get("version") and not entries:
        raise FeedParseError("not_a_feed")

    items: list[ParsedItem] = []
    seen: set[str] = set()
    has_audio = False
    for entry in entries:
        if len(items) >= MAX_ITEMS_PER_FETCH:
            break
        url = safe_link(entry.get("link"))
        title = plain_text(entry.get("title"), limit=MAX_TITLE_LENGTH)
        guid = _guid(entry, url, title)
        if guid is None or guid in seen:
            continue
        seen.add(guid)
        summary_source = entry.get("summary")
        if not summary_source and entry.get("content"):
            summary_source = (entry["content"][0] or {}).get("value")
        audio_url = _audio(entry)
        has_audio = has_audio or audio_url is not None
        author = plain_text(entry.get("author"), limit=MAX_AUTHOR_LENGTH) or None
        items.append(
            ParsedItem(
                guid=guid,
                url=url,
                title=title or (url or "未命名条目")[:MAX_TITLE_LENGTH],
                summary=plain_text(summary_source, limit=MAX_SUMMARY_LENGTH),
                author=author,
                published_at=_timestamp(entry),
                audio_url=audio_url,
                duration_seconds=parse_duration(entry.get("itunes_duration")),
                image_url=_image(entry),
            )
        )
    is_podcast = has_audio or bool(header.get("itunes_author") or header.get("itunes_explicit"))
    return ParsedFeed(
        title=plain_text(header.get("title"), limit=MAX_FEED_TITLE_LENGTH),
        site_url=safe_link(header.get("link")),
        kind="podcast" if is_podcast else "rss",
        items=items,
        image_url=_channel_image(header),
    )
