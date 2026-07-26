"""Extract URLs from free text and stage them for a single confirmed write.

Two things this module exists to stop.

**"The model will loop over them."**  Before this, ``/存入`` with ten URLs relied
on the model choosing to call ``propose_site`` ten times.  Nothing guaranteed
it.  Extraction is mechanical work with a right answer, so it belongs in code —
the same reasoning that keeps duplicate detection out of the model.

**One bad URL poisoning the batch.**  Every item carries its own status, so a
timeout on item 7 cannot stop items 8-10 from being saved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.bookmarks.models import NormalizationStatus
from webhub.bookmarks.normalization import normalize_bookmark_url
from webhub.db.models import Site
from webhub.library.schemas import MAX_BATCH_URLS
from webhub.library.service import LibraryError

ItemStatus = Literal["ready", "duplicate", "invalid", "created", "failed"]

# Deliberately conservative: an http(s) scheme is required rather than guessed.
# "example.com" in prose is far more often a sentence than an address, and a
# wrong guess here silently saves something the user never asked for.
# ASCII parens/brackets are *allowed* in the match — Wikipedia's
# `/wiki/Foo_(bar)` is a real URL — and unbalanced trailing ones are stripped
# afterwards.  CJK punctuation never appears in a URL, so it terminates one.
# The excluded ranges are written as escapes so the intent is readable:
# U+3000-U+303F is CJK punctuation, U+FF00-U+FFEF is fullwidth forms.
_URL_PATTERN = re.compile(r"https?://[^\s<>\"'\u3000-\u303f\uff00-\uffef]+", re.IGNORECASE)
# Trailing punctuation that is almost always sentence punctuation, not the URL.
_TRAILING = ".,;:!?'\"”’》】"


@dataclass(frozen=True, slots=True)
class BatchItem:
    url: str
    status: ItemStatus
    reason: str | None = None
    site_id: str | None = None
    identity_url: str | None = None


def extract_urls(text: str, *, limit: int = MAX_BATCH_URLS) -> list[str]:
    """Pull every http(s) URL out of free text, in order, without duplicates.

    De-duplication here is only textual; two spellings of the same address are
    collapsed later by ``identity_url``.  Doing it in both places is deliberate:
    this one keeps the preview honest, that one keeps the database correct.
    """

    seen: set[str] = set()
    found: list[str] = []
    for match in _URL_PATTERN.finditer(text or ""):
        candidate = match.group(0).rstrip(_TRAILING)
        # Drop only the closing brackets the URL never opened: keeps
        # `/wiki/Foo_(bar)` intact while shedding a sentence's own `)`.
        while candidate and candidate[-1] in ")]":
            opener = "(" if candidate[-1] == ")" else "["
            if candidate.count(opener) >= candidate.count(candidate[-1]):
                break
            candidate = candidate[:-1].rstrip(_TRAILING)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        found.append(candidate)
        if len(found) >= limit:
            break
    return found


async def preview_batch(
    session: AsyncSession,
    user_id: str,
    urls: list[str],
) -> list[BatchItem]:
    """Classify each URL without writing anything.

    Read-only by construction, which is what makes "确认前主数据无变化" a
    property of the code rather than a promise in a comment.
    """

    items: list[BatchItem] = []
    normalized: dict[str, str] = {}
    for url in urls[:MAX_BATCH_URLS]:
        result = normalize_bookmark_url(url)
        if result.status is not NormalizationStatus.ACCEPTED or not result.normalized_url:
            items.append(
                BatchItem(url=url, status="invalid", reason="网址无效或不受支持")
            )
            continue
        normalized[url] = result.normalized_url

    if normalized:
        existing = set(
            (
                await session.scalars(
                    select(Site.identity_url).where(
                        Site.user_id == user_id,
                        Site.identity_url.in_(list(normalized.values())),
                    )
                )
            ).all()
        )
    else:
        existing = set()

    # Track identities claimed earlier in this same batch so two spellings of
    # one address report as a duplicate instead of both looking importable.
    claimed: set[str] = set()
    ordered: list[BatchItem] = []
    for url in urls[:MAX_BATCH_URLS]:
        identity = normalized.get(url)
        if identity is None:
            ordered.append(next(item for item in items if item.url == url))
            continue
        if identity in existing or identity in claimed:
            ordered.append(
                BatchItem(
                    url=url,
                    status="duplicate",
                    reason="资料库里已经有这个网址",
                    identity_url=identity,
                )
            )
            continue
        claimed.add(identity)
        ordered.append(BatchItem(url=url, status="ready", identity_url=identity))
    return ordered


__all__ = [
    "MAX_BATCH_URLS",
    "BatchItem",
    "ItemStatus",
    "create_batch",
    "extract_urls",
    "preview_batch",
]


async def create_batch(
    session: AsyncSession,
    user_id: str,
    urls: list[str],
    *,
    default_name_from_host: bool = True,
) -> list[BatchItem]:
    """Create a Site for every importable URL, item by item.

    Each item commits on its own.  That is the point: one URL that trips a
    constraint must not roll back the nine that succeeded, and the caller gets
    a per-item answer rather than "the batch failed".

    Replay safety comes from the same place as the bookmark import — the
    ``UNIQUE (user_id, identity_url)`` index — so confirming twice reports
    every item as ``duplicate`` instead of writing a second row.
    """

    from webhub.library import service as library_service
    from webhub.library.schemas import SiteCreateRequest

    previewed = await preview_batch(session, user_id, urls)
    results: list[BatchItem] = []
    for item in previewed:
        if item.status != "ready":
            results.append(item)
            continue
        host = ""
        try:
            host = urlsplit(item.url).hostname or ""
        except ValueError:
            host = ""
        name = host if (default_name_from_host and host) else item.url
        try:
            created = await library_service.create_site(
                session,
                user_id,
                SiteCreateRequest(name=name[:160], url=item.url),
            )
        except LibraryError as error:
            # A concurrent write may have taken the URL between preview and
            # create; report it on this item and keep going.
            results.append(
                BatchItem(
                    url=item.url,
                    status="duplicate" if "已存在" in error.message else "failed",
                    reason=error.message,
                    identity_url=item.identity_url,
                )
            )
            continue
        results.append(
            BatchItem(
                url=item.url,
                status="created",
                site_id=created.id,
                identity_url=item.identity_url,
            )
        )
    return results
