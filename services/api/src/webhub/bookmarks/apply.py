"""Turn a staged bookmark import into real Site rows.

This is the half of the import pipeline that was missing: parsing, staging and
preview all worked, but nothing ever wrote a ``Site``.

Three properties this module leans on rather than re-implements:

* **Idempotency comes from the schema.**  ``sites`` has a
  ``UNIQUE (user_id, identity_url)``, so a URL cannot exist twice in one
  account.  Applying the same job again therefore finds every candidate already
  present and skips it.  There is no separate "already applied" bookkeeping to
  drift out of sync with reality.
* **The staging layer already decided.**  Each candidate carries a
  ``proposed_action`` computed at parse time; apply executes that decision, it
  does not re-derive one.
* **Categories come from the shared rule classifier**, the same
  ``suggest_category`` the preview shows, so what the user approved is what gets
  written.

Deliberately *not* done here: populating ``bookmark_source_occurrences`` /
``site_import_origins``.  Those tables exist in the schema but nothing in the
codebase writes them, so the provenance chain "which bookmark occurrence became
which site" is still unbuilt.  Half-filling it here would create a table that
looks authoritative while covering only imports that happen to run through this
function.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import (
    DEFAULT_CATEGORY_NAME,
    BookmarkStagingCandidate,
    BookmarkStagingCandidateFolder,
    BookmarkStagingFolder,
    Category,
    Site,
    new_id,
    utc_now,
)
from webhub.library.service import LibraryError
from webhub.library.service import _site_url as normalize_site_url

from .classification import suggest_category

# Rows per transaction.  Small enough that a failure loses little work, large
# enough that a 2000-bookmark import is not 2000 fsyncs.
BATCH_SIZE = 200

# Actions the user is asked to review by hand; apply never acts on them.
_SKIPPED_ACTIONS = frozenset({"reject", "needs_review"})


class BookmarkApplyError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    """What one apply run did, in the vocabulary the UI reports."""

    total_candidates: int
    created: int
    skipped_existing: int
    skipped_needs_review: int
    failed: int

    def as_dict(self) -> dict[str, int]:
        return {
            "total_candidates": self.total_candidates,
            "created": self.created,
            "skipped_existing": self.skipped_existing,
            "skipped_needs_review": self.skipped_needs_review,
            "failed": self.failed,
        }


def _folder_path(display_path_json: str | None) -> tuple[str, ...]:
    """``display_path`` is stored as a JSON array of folder names."""

    if not display_path_json:
        return ()
    try:
        parsed = json.loads(display_path_json)
    except ValueError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(entry for entry in parsed if isinstance(entry, str) and entry.strip())


async def _category_ids(session: AsyncSession, user_id: str) -> dict[str, str]:
    """Existing categories for the account, keyed by normalized name."""

    rows = await session.execute(
        select(Category.normalized_name, Category.id).where(Category.user_id == user_id)
    )
    return {name: identifier for name, identifier in rows.all()}


async def _ensure_category(
    session: AsyncSession,
    user_id: str,
    name: str,
    cache: dict[str, str],
) -> str:
    normalized = name.strip().casefold()
    existing = cache.get(normalized)
    if existing is not None:
        return existing
    category = Category(
        id=new_id(),
        user_id=user_id,
        name=name.strip(),
        normalized_name=normalized,
    )
    session.add(category)
    await session.flush()
    cache[normalized] = category.id
    return category.id


async def _existing_identity_urls(
    session: AsyncSession,
    user_id: str,
    identity_urls: list[str],
) -> set[str]:
    if not identity_urls:
        return set()
    rows = await session.scalars(
        select(Site.identity_url).where(
            Site.user_id == user_id,
            Site.identity_url.in_(identity_urls),
        )
    )
    return set(rows.all())


async def _candidate_folder_paths(
    session: AsyncSession,
    user_id: str,
    run_id: str,
    candidate_ids: list[str],
) -> dict[str, tuple[str, ...]]:
    """Map each candidate to the folder path of its earliest occurrence.

    A URL filed in several folders keeps the first one by source order, so the
    category a user sees in the preview is the one they get.
    """

    if not candidate_ids:
        return {}
    rows = await session.execute(
        select(
            BookmarkStagingCandidateFolder.candidate_id,
            BookmarkStagingCandidateFolder.first_source_sequence,
            BookmarkStagingFolder.display_path,
        )
        .join(
            BookmarkStagingFolder,
            (BookmarkStagingFolder.user_id == BookmarkStagingCandidateFolder.user_id)
            & (BookmarkStagingFolder.run_id == BookmarkStagingCandidateFolder.run_id)
            & (BookmarkStagingFolder.id == BookmarkStagingCandidateFolder.folder_id),
            isouter=True,
        )
        .where(
            BookmarkStagingCandidateFolder.user_id == user_id,
            BookmarkStagingCandidateFolder.run_id == run_id,
            BookmarkStagingCandidateFolder.candidate_id.in_(candidate_ids),
        )
        .order_by(
            BookmarkStagingCandidateFolder.candidate_id,
            BookmarkStagingCandidateFolder.first_source_sequence,
        )
    )
    paths: dict[str, tuple[str, ...]] = {}
    for candidate_id, _sequence, display_path in rows.all():
        if candidate_id in paths:
            continue
        paths[candidate_id] = _folder_path(display_path)
    return paths


def _site_name(title: str, host: str) -> str:
    name = " ".join(title.split())[:160]
    return name or host[:160]


async def apply_candidates(
    session: AsyncSession,
    user_id: str,
    run_id: str,
    *,
    batch_size: int = BATCH_SIZE,
) -> ApplyOutcome:
    """Write every actionable staged candidate into the account's library."""

    total = int(
        await session.scalar(
            select(func.count())
            .select_from(BookmarkStagingCandidate)
            .where(
                BookmarkStagingCandidate.user_id == user_id,
                BookmarkStagingCandidate.run_id == run_id,
            )
        )
        or 0
    )
    created = skipped_existing = skipped_needs_review = failed = 0
    cursor: tuple[int, str] | None = None
    category_cache = await _category_ids(session, user_id)

    while True:
        conditions: list[Any] = [
            BookmarkStagingCandidate.user_id == user_id,
            BookmarkStagingCandidate.run_id == run_id,
        ]
        if cursor is not None:
            sequence, item_id = cursor
            conditions.append(
                (BookmarkStagingCandidate.first_source_sequence > sequence)
                | (
                    (BookmarkStagingCandidate.first_source_sequence == sequence)
                    & (BookmarkStagingCandidate.id > item_id)
                )
            )
        batch = list(
            (
                await session.scalars(
                    select(BookmarkStagingCandidate)
                    .where(*conditions)
                    .order_by(
                        BookmarkStagingCandidate.first_source_sequence,
                        BookmarkStagingCandidate.id,
                    )
                    .limit(batch_size)
                )
            ).all()
        )
        if not batch:
            break
        cursor = (batch[-1].first_source_sequence, batch[-1].id)

        actionable = [row for row in batch if row.proposed_action not in _SKIPPED_ACTIONS]
        skipped_needs_review += len(batch) - len(actionable)

        # One lookup per batch rather than per row: 2000 candidates would
        # otherwise mean 2000 round trips just to answer "do I already have it".
        folder_paths = await _candidate_folder_paths(
            session,
            user_id,
            run_id,
            [row.id for row in actionable],
        )
        present = await _existing_identity_urls(
            session,
            user_id,
            [row.identity_url for row in actionable],
        )

        now = utc_now()
        # Guards against the same URL appearing twice inside one batch, which
        # would otherwise trip the unique index mid-transaction.
        claimed: set[str] = set()
        for row in actionable:
            if row.identity_url in present or row.identity_url in claimed:
                skipped_existing += 1
                continue
            try:
                original_url, identity_url = normalize_site_url(row.identity_url)
            except LibraryError:
                failed += 1
                continue
            if identity_url in present or identity_url in claimed:
                skipped_existing += 1
                continue

            suggestion = suggest_category(
                folder_paths.get(row.id, ()),
                row.display_title,
                row.host,
            )
            category_name = suggestion.category or DEFAULT_CATEGORY_NAME
            category_id = await _ensure_category(session, user_id, category_name, category_cache)
            name = _site_name(row.display_title, row.host)
            session.add(
                Site(
                    id=new_id(),
                    user_id=user_id,
                    category_id=category_id,
                    name=name,
                    normalized_name=name.casefold(),
                    original_url=original_url,
                    identity_url=identity_url,
                    description=None,
                    favicon_url=None,
                    pinned=False,
                    source="browser_import",
                    analysis_status="not_analyzed",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            claimed.add(identity_url)
            created += 1

        try:
            await session.commit()
        except IntegrityError as error:
            # A concurrent write took one of these URLs between the lookup and
            # the commit.  Lose the batch rather than the whole import.
            await session.rollback()
            failed += len(claimed)
            created -= len(claimed)
            if created < 0:  # pragma: no cover - defensive
                created = 0
            raise BookmarkApplyError(
                409,
                "bookmark_apply_conflict",
                "导入过程中资料库发生了并发修改，请重新发起导入",
            ) from error

    return ApplyOutcome(
        total_candidates=total,
        created=created,
        skipped_existing=skipped_existing,
        skipped_needs_review=skipped_needs_review,
        failed=failed,
    )


__all__ = [
    "BATCH_SIZE",
    "ApplyOutcome",
    "BookmarkApplyError",
    "apply_candidates",
]
