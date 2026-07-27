"""Read title / description / image / GitHub links out of an HTML document.

Uses the standard library's ``html.parser`` rather than a DOM library on
purpose: the whole job is reading a handful of ``<head>`` tags, a full DOM buys
nothing, and every third-party HTML parser is another parser to keep patched
against malformed-input bugs.  Nothing here executes anything — no scripts, no
network, no ``eval``.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

MAX_TITLE_CHARS = 160
MAX_DESCRIPTION_CHARS = 1_000
# Enough to catch a repository list; beyond this the page is not "about" them.
MAX_RELATED_LINKS = 8
# Parsing deliberately does *not* stop at <body>: related-repository links live
# in the body, so an early stop returned metadata but never any links.  Input is
# already bounded by the fetcher's 2 MB body cap, and link collection stops at
# MAX_RELATED_LINKS, so scanning on costs little.


@dataclass(slots=True)
class ParsedMetadata:
    title: str | None = None
    og_title: str | None = None
    description: str | None = None
    image_url: str | None = None
    icon_href: str | None = None
    github_links: list[str] = field(default_factory=list)

    @property
    def best_title(self) -> str | None:
        """``og:title`` is usually the cleaner human title; fall back to ``<title>``."""

        return self.og_title or self.title


def _collapse(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.result = ParsedMetadata()
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return

        values = {key.lower(): (value or "") for key, value in attrs}
        if tag == "meta":
            # og:* uses `property`, description uses `name`; accept either key.
            key = (values.get("property") or values.get("name") or "").strip().lower()
            content = values.get("content", "").strip()
            if not content:
                return
            if key in {"og:title", "twitter:title"} and not self.result.og_title:
                self.result.og_title = _collapse(content, MAX_TITLE_CHARS)
            elif key in {"description", "og:description", "twitter:description"}:
                if not self.result.description:
                    self.result.description = _collapse(content, MAX_DESCRIPTION_CHARS)
            elif key in {"og:image", "twitter:image"} and not self.result.image_url:
                self.result.image_url = content
        elif tag == "link":
            rel = values.get("rel", "").strip().lower()
            href = values.get("href", "").strip()
            icon_relations = {
                "icon",
                "apple-touch-icon",
                "apple-touch-icon-precomposed",
                "mask-icon",
            }
            if href and not self.result.icon_href and icon_relations.intersection(rel.split()):
                self.result.icon_href = href
        elif tag == "a":
            href = values.get("href", "").strip()
            if href and len(self.result.github_links) < MAX_RELATED_LINKS:
                self.result.github_links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)


def _github_repository(url: str) -> str | None:
    """Keep only ``https://github.com/owner/repo`` style links.

    Deliberately narrow.  The queue asked for "the most obviously defensible
    thing" for related sites: a repository link is a fact stated by the page,
    not an inference.  Anything looser would be the model guessing.
    """

    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return None
    if parts.hostname not in {"github.com", "www.github.com"}:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) != 2:
        return None
    owner, repo = segments
    # These are github's own pages, not repositories.
    if owner.lower() in {"features", "about", "pricing", "topics", "collections", "sponsors"}:
        return None
    return f"https://github.com/{owner}/{repo.removesuffix('.git')}"


def parse_metadata(html: str, *, base_url: str) -> ParsedMetadata:
    """Extract metadata from one HTML document.

    Malformed markup is expected — browser exports and real pages are full of
    it — so a parse error yields whatever was collected so far rather than
    failing the whole analysis.
    """

    parser = _MetadataParser()
    # 部分元数据好过没有：真实页面（尤其浏览器导出里的老页面）畸形标记很常见，
    # 解析中途出错时保留已经收集到的部分，而不是整次分析失败。
    with suppress(Exception):
        parser.feed(html)

    result = parser.result
    if parser._title_parts and not result.title:  # noqa: SLF001 - same module
        result.title = _collapse("".join(parser._title_parts), MAX_TITLE_CHARS)  # noqa: SLF001

    # Relative URLs are common for icons and og:image; resolve against the page
    # that was actually fetched (after redirects), not the requested URL.
    if result.image_url:
        result.image_url = urljoin(base_url, result.image_url)
    if result.icon_href:
        result.icon_href = urljoin(base_url, result.icon_href)

    seen: set[str] = set()
    repositories: list[str] = []
    for href in result.github_links:
        repository = _github_repository(urljoin(base_url, href))
        if repository and repository not in seen:
            seen.add(repository)
            repositories.append(repository)
    result.github_links = repositories[:MAX_RELATED_LINKS]
    return result


__all__ = [
    "MAX_DESCRIPTION_CHARS",
    "MAX_RELATED_LINKS",
    "MAX_TITLE_CHARS",
    "ParsedMetadata",
    "parse_metadata",
]
