"""Bookmark source ledger: the long-lived "which bookmark became which site" facts.

PRD §4.2 / FR-DA-005 / FR-DA-007: every bookmark position in an imported file is
kept as its own occurrence, under the original folder tree, even after staging
is gone; an occurrence that landed in the library is linked to that Site.

This runs inside the caller's single apply transaction, right after the Site
writes were flushed, so the ledger and the library commit or roll back together.

* **Source facts are copied from the applied run's staging rows.** Folder and
  occurrence ids are reused verbatim: staging ids are ``uuid5(run_id, …)`` and a
  snapshot only ever gets one applied run (its job cannot be applied twice), so
  ids stay unique while parent links copy over without a remapping table.
* **Every occurrence is kept**, including invalid / unsupported ones (with their
  ``validation_status`` and ``reason_code``) and accepted ones apply skipped.
  Those simply have no ``site_import_origins`` row: "kept but not linked" is the
  recorded skip, rather than a second status column that could drift.
* **Links follow what apply actually did.** A candidate is linked to the Site
  that now holds its identity URL (or, for a child page merged into its
  homepage, the homepage Site). ``created_site`` means this import created that
  Site; ``matched_existing`` means it was already in the library.
* **Idempotent.** Re-running against a snapshot whose facts already came from
  this run is a no-op copy, and links use ``ON CONFLICT DO NOTHING`` on the
  one-link-per-occurrence constraint. Facts from a *different* run are refused
  instead of being silently mixed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import DateTime, String, and_, func, literal, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import (
    BookmarkImportJob,
    BookmarkImportRun,
    BookmarkSourceFolder,
    BookmarkSourceOccurrence,
    BookmarkStagingCandidate,
    BookmarkStagingCandidateOccurrence,
    BookmarkStagingFolder,
    BookmarkStagingOccurrence,
    Site,
    SiteImportOrigin,
    new_id,
    utc_now,
)

from .models import NormalizationStatus
from .normalization import normalize_bookmark_url


class _MergeTarget(Protocol):
    @property
    def canonical_url(self) -> str: ...


@dataclass(frozen=True, slots=True)
class LedgerOutcome:
    snapshot_id: str
    folder_count: int
    occurrence_count: int
    linked_occurrences: int


class SourceLedgerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_FOLDER_COLUMNS = (
    "id",
    "user_id",
    "snapshot_id",
    "source_folder_key",
    "parent_id",
    "source_sequence",
    "source_order",
    "depth",
    "title",
    "display_path",
    "created_at",
)

_OCCURRENCE_COLUMNS = (
    "id",
    "user_id",
    "snapshot_id",
    "source_occurrence_key",
    "folder_id",
    "source_sequence",
    "source_order",
    "raw_title",
    "raw_url",
    "add_date",
    "last_modified",
    "validation_status",
    "fetch_policy",
    "reason_code",
    "has_sensitive_url",
    "created_at",
)


async def _snapshot_for_run(session: AsyncSession, user_id: str, run_id: str) -> str:
    snapshot_id = await session.scalar(
        select(BookmarkImportJob.snapshot_id)
        .join(
            BookmarkImportRun,
            and_(
                BookmarkImportRun.user_id == BookmarkImportJob.user_id,
                BookmarkImportRun.job_id == BookmarkImportJob.id,
            ),
        )
        .where(BookmarkImportRun.user_id == user_id, BookmarkImportRun.id == run_id)
    )
    if snapshot_id is None:
        raise SourceLedgerError("bookmark_source_ledger_missing_run", "导入任务不存在")
    return snapshot_id


async def _count(session: AsyncSession, statement: Any) -> int:
    return int(await session.scalar(statement) or 0)


async def _copy_source_facts(
    session: AsyncSession,
    user_id: str,
    run_id: str,
    snapshot_id: str,
) -> tuple[int, int]:
    staged_folders = await _count(
        session,
        select(func.count())
        .select_from(BookmarkStagingFolder)
        .where(BookmarkStagingFolder.user_id == user_id, BookmarkStagingFolder.run_id == run_id),
    )
    staged_occurrences = await _count(
        session,
        select(func.count())
        .select_from(BookmarkStagingOccurrence)
        .where(
            BookmarkStagingOccurrence.user_id == user_id,
            BookmarkStagingOccurrence.run_id == run_id,
        ),
    )
    existing_folders = await _count(
        session,
        select(func.count())
        .select_from(BookmarkSourceFolder)
        .where(
            BookmarkSourceFolder.user_id == user_id,
            BookmarkSourceFolder.snapshot_id == snapshot_id,
        ),
    )
    existing_occurrences = await _count(
        session,
        select(func.count())
        .select_from(BookmarkSourceOccurrence)
        .where(
            BookmarkSourceOccurrence.user_id == user_id,
            BookmarkSourceOccurrence.snapshot_id == snapshot_id,
        ),
    )
    if existing_folders or existing_occurrences:
        # Replay: accept only facts that are exactly this run's rows.
        same_folders = await _count(
            session,
            select(func.count())
            .select_from(BookmarkSourceFolder)
            .join(
                BookmarkStagingFolder,
                and_(
                    BookmarkStagingFolder.user_id == BookmarkSourceFolder.user_id,
                    BookmarkStagingFolder.run_id == run_id,
                    BookmarkStagingFolder.id == BookmarkSourceFolder.id,
                ),
            )
            .where(
                BookmarkSourceFolder.user_id == user_id,
                BookmarkSourceFolder.snapshot_id == snapshot_id,
            ),
        )
        same_occurrences = await _count(
            session,
            select(func.count())
            .select_from(BookmarkSourceOccurrence)
            .join(
                BookmarkStagingOccurrence,
                and_(
                    BookmarkStagingOccurrence.user_id == BookmarkSourceOccurrence.user_id,
                    BookmarkStagingOccurrence.run_id == run_id,
                    BookmarkStagingOccurrence.id == BookmarkSourceOccurrence.id,
                ),
            )
            .where(
                BookmarkSourceOccurrence.user_id == user_id,
                BookmarkSourceOccurrence.snapshot_id == snapshot_id,
            ),
        )
        if (
            existing_folders,
            existing_occurrences,
            same_folders,
            same_occurrences,
        ) != (staged_folders, staged_occurrences, staged_folders, staged_occurrences):
            raise SourceLedgerError(
                "bookmark_source_ledger_conflict",
                "该书签文件的来源记录已由另一次解析写入，请重新发起导入",
            )
        return staged_folders, staged_occurrences

    now = utc_now()
    folder_select = (
        select(
            BookmarkStagingFolder.id,
            BookmarkStagingFolder.user_id,
            literal(snapshot_id, String(36)),
            BookmarkStagingFolder.source_folder_key,
            BookmarkStagingFolder.parent_id,
            BookmarkStagingFolder.source_sequence,
            BookmarkStagingFolder.source_order,
            BookmarkStagingFolder.depth,
            BookmarkStagingFolder.title,
            BookmarkStagingFolder.display_path,
            literal(now, DateTime(timezone=True)),
        )
        .where(BookmarkStagingFolder.user_id == user_id, BookmarkStagingFolder.run_id == run_id)
        # Parents precede children in document order, so the self-referencing
        # foreign key is satisfied row by row.
        .order_by(BookmarkStagingFolder.source_sequence)
    )
    await session.execute(
        sqlite_insert(BookmarkSourceFolder).from_select(list(_FOLDER_COLUMNS), folder_select)
    )
    occurrence_select = (
        select(
            BookmarkStagingOccurrence.id,
            BookmarkStagingOccurrence.user_id,
            literal(snapshot_id, String(36)),
            BookmarkStagingOccurrence.source_occurrence_key,
            BookmarkStagingOccurrence.folder_id,
            BookmarkStagingOccurrence.source_sequence,
            BookmarkStagingOccurrence.source_order,
            BookmarkStagingOccurrence.raw_title,
            BookmarkStagingOccurrence.raw_url,
            BookmarkStagingOccurrence.add_date,
            BookmarkStagingOccurrence.last_modified,
            BookmarkStagingOccurrence.validation_status,
            BookmarkStagingOccurrence.fetch_policy,
            BookmarkStagingOccurrence.reason_code,
            BookmarkStagingOccurrence.has_sensitive_url,
            literal(now, DateTime(timezone=True)),
        )
        .where(
            BookmarkStagingOccurrence.user_id == user_id,
            BookmarkStagingOccurrence.run_id == run_id,
        )
        .order_by(BookmarkStagingOccurrence.source_sequence)
    )
    await session.execute(
        sqlite_insert(BookmarkSourceOccurrence).from_select(
            list(_OCCURRENCE_COLUMNS), occurrence_select
        )
    )
    return staged_folders, staged_occurrences


def _identity(url: str) -> str | None:
    normalized = normalize_bookmark_url(url)
    if normalized.status is not NormalizationStatus.ACCEPTED or not normalized.normalized_url:
        return None
    return normalized.normalized_url


async def record_source_ledger(
    session: AsyncSession,
    user_id: str,
    run_id: str,
    *,
    merge_targets: Mapping[str, _MergeTarget],
    created_site_ids: frozenset[str] | set[str],
    skipped_actions: frozenset[str],
    batch_size: int,
) -> LedgerOutcome:
    """Persist source facts and Site links for one applied run.

    ``merge_targets`` maps a candidate id to its accepted merge-to-homepage
    plan; ``created_site_ids`` are the Sites this apply inserted.
    """

    snapshot_id = await _snapshot_for_run(session, user_id, run_id)
    folder_count, occurrence_count = await _copy_source_facts(
        session, user_id, run_id, snapshot_id
    )

    cursor: tuple[int, str] | None = None
    while True:
        conditions: list[Any] = [
            BookmarkStagingCandidate.user_id == user_id,
            BookmarkStagingCandidate.run_id == run_id,
            BookmarkStagingCandidate.proposed_action.not_in(tuple(skipped_actions)),
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
        batch = (
            await session.execute(
                select(
                    BookmarkStagingCandidate.id,
                    BookmarkStagingCandidate.identity_url,
                    BookmarkStagingCandidate.proposed_action,
                    BookmarkStagingCandidate.first_source_sequence,
                )
                .where(*conditions)
                .order_by(
                    BookmarkStagingCandidate.first_source_sequence,
                    BookmarkStagingCandidate.id,
                )
                .limit(batch_size)
            )
        ).all()
        if not batch:
            break
        cursor = (batch[-1].first_source_sequence, batch[-1].id)

        targets: dict[str, str] = {}
        for candidate_id, identity_url, proposed_action, _sequence in batch:
            plan = merge_targets.get(candidate_id)
            # A child page the user merged into its homepage belongs to the
            # homepage Site; one that already had its own Site keeps it.
            source_url = (
                plan.canonical_url
                if plan is not None and proposed_action == "create"
                else identity_url
            )
            target = _identity(source_url)
            if target is not None:
                targets[candidate_id] = target
        if not targets:
            continue

        sites = {
            identity_url: (site_id, version)
            for site_id, identity_url, version in (
                await session.execute(
                    select(Site.id, Site.identity_url, Site.version).where(
                        Site.user_id == user_id,
                        Site.identity_url.in_(sorted(set(targets.values()))),
                    )
                )
            ).all()
        }
        occurrence_rows = (
            await session.execute(
                select(
                    BookmarkStagingCandidateOccurrence.candidate_id,
                    BookmarkStagingCandidateOccurrence.occurrence_id,
                ).where(
                    BookmarkStagingCandidateOccurrence.user_id == user_id,
                    BookmarkStagingCandidateOccurrence.run_id == run_id,
                    BookmarkStagingCandidateOccurrence.candidate_id.in_(list(targets)),
                )
            )
        ).all()
        now = utc_now()
        values: list[dict[str, object]] = []
        for candidate_id, occurrence_id in occurrence_rows:
            site = sites.get(targets[candidate_id])
            if site is None:
                continue
            site_id, version = site
            values.append(
                {
                    "id": new_id(),
                    "user_id": user_id,
                    "site_id": site_id,
                    "source_occurrence_id": occurrence_id,
                    "link_action": (
                        "created_site" if site_id in created_site_ids else "matched_existing"
                    ),
                    "site_version_at_link": version,
                    "created_at": now,
                }
            )
        if values:
            await session.execute(
                sqlite_insert(SiteImportOrigin).on_conflict_do_nothing(
                    index_elements=["user_id", "source_occurrence_id"]
                ),
                values,
            )
    linked = await _count(
        session,
        select(func.count())
        .select_from(SiteImportOrigin)
        .join(
            BookmarkSourceOccurrence,
            and_(
                BookmarkSourceOccurrence.user_id == SiteImportOrigin.user_id,
                BookmarkSourceOccurrence.id == SiteImportOrigin.source_occurrence_id,
            ),
        )
        .where(
            SiteImportOrigin.user_id == user_id,
            BookmarkSourceOccurrence.snapshot_id == snapshot_id,
        ),
    )
    return LedgerOutcome(
        snapshot_id=snapshot_id,
        folder_count=folder_count,
        occurrence_count=occurrence_count,
        linked_occurrences=linked,
    )


__all__ = ["LedgerOutcome", "SourceLedgerError", "record_source_ledger"]
