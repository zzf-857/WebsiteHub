"""Turn a staged bookmark import into real Site rows.

This is the half of the import pipeline that was missing: parsing, staging and
preview all worked, but nothing ever wrote a ``Site``.

Three properties this module leans on rather than re-implements:

* **Idempotency has two layers.** The job is claimed with an optimistic state
  transition, and ``sites`` also has ``UNIQUE (user_id, identity_url)``. A
  completed job cannot be applied again, while concurrent library writes are
  still stopped by the schema.
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
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.locking import reserve_account_taxonomy
from webhub.db.models import (
    DEFAULT_CATEGORY_NAME,
    BookmarkSimilarityCluster,
    BookmarkSimilarityClusterMember,
    BookmarkSimilarityDecision,
    BookmarkStagingCandidate,
    BookmarkStagingCandidateFolder,
    BookmarkStagingFolder,
    Category,
    Site,
    SiteMetadataPreference,
    SiteTag,
    Tag,
    new_id,
    utc_now,
)

from .classification import meaningful_folder_path, suggest_category
from .classification_history import (
    load_account_host_category_history,
    normalized_history_host,
)
from .models import CategorySuggestion, NormalizationStatus
from .normalization import normalize_bookmark_url
from .privacy import agent_safe_label

# Rows per read/flush window. The caller owns one atomic transaction for the
# whole apply, so this bounds ORM memory without exposing partial commits.
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
    merged_candidates: int
    failed: int

    def as_dict(self) -> dict[str, int]:
        return {
            "total_candidates": self.total_candidates,
            "created": self.created,
            "skipped_existing": self.skipped_existing,
            "skipped_needs_review": self.skipped_needs_review,
            "merged_candidates": self.merged_candidates,
            "failed": self.failed,
        }


@dataclass(frozen=True, slots=True)
class _MergePlan:
    cluster_id: str
    canonical_url: str
    canonical_title: str
    canonical_candidate_id: str | None
    trigger_candidate_id: str
    member_ids: frozenset[str]
    folder_path: tuple[str, ...]
    tag_names: tuple[str, ...]
    merge_create_count: int
    reduction_count: int


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


async def _category_taxonomy(
    session: AsyncSession,
    user_id: str,
) -> dict[str, tuple[str, str]]:
    rows = await session.execute(
        select(Category.normalized_name, Category.id, Category.name).where(
            Category.user_id == user_id
        )
    )
    return {
        normalized_name: (identifier, name)
        for normalized_name, identifier, name in rows.all()
    }


async def _ensure_category(
    session: AsyncSession,
    user_id: str,
    name: str,
    cache: dict[str, tuple[str, str]],
) -> str:
    normalized = name.strip().casefold()
    existing = cache.get(normalized)
    if existing is not None:
        return existing[0]
    category = Category(
        id=new_id(),
        user_id=user_id,
        name=name.strip(),
        normalized_name=normalized,
    )
    session.add(category)
    await session.flush()
    cache[normalized] = (category.id, category.name)
    return category.id


async def _tag_taxonomy(session: AsyncSession, user_id: str) -> dict[str, str]:
    rows = await session.execute(
        select(Tag.normalized_name, Tag.id).where(Tag.user_id == user_id)
    )
    return {normalized_name: identifier for normalized_name, identifier in rows.all()}


async def _ensure_tag(
    session: AsyncSession,
    user_id: str,
    name: str,
    cache: dict[str, str],
) -> str:
    normalized = name.casefold()
    existing = cache.get(normalized)
    if existing is not None:
        return existing
    tag = Tag(
        id=new_id(),
        user_id=user_id,
        name=name,
        normalized_name=normalized,
    )
    session.add(tag)
    await session.flush()
    cache[normalized] = tag.id
    return tag.id


def _folder_tag_names(paths: tuple[tuple[str, ...], ...]) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for path in paths:
        for raw_name in reversed(meaningful_folder_path(path)):
            normalized = " ".join(unicodedata.normalize("NFKC", raw_name).split())
            safe = agent_safe_label(normalized, max_chars=40)
            if safe is None:
                continue
            key = safe.casefold()
            if key in seen:
                continue
            seen.add(key)
            names.append(safe)
            if len(names) == 8:
                return tuple(names)
    return tuple(names)


async def _existing_sites(
    session: AsyncSession,
    user_id: str,
    identity_urls: list[str],
) -> dict[str, tuple[Site, SiteMetadataPreference | None]]:
    if not identity_urls:
        return {}
    rows = await session.execute(
        select(Site, SiteMetadataPreference)
        .outerjoin(
            SiteMetadataPreference,
            and_(
                SiteMetadataPreference.user_id == Site.user_id,
                SiteMetadataPreference.site_id == Site.id,
            ),
        )
        .where(
            Site.user_id == user_id,
            Site.identity_url.in_(identity_urls),
        )
    )
    return {site.identity_url: (site, preference) for site, preference in rows.all()}


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


async def _merge_plans(
    session: AsyncSession,
    user_id: str,
    run_id: str,
) -> tuple[dict[str, _MergePlan], int]:
    clusters = list(
        (
            await session.scalars(
                select(BookmarkSimilarityCluster)
                .join(
                    BookmarkSimilarityDecision,
                    and_(
                        BookmarkSimilarityDecision.user_id
                        == BookmarkSimilarityCluster.user_id,
                        BookmarkSimilarityDecision.run_id
                        == BookmarkSimilarityCluster.run_id,
                        BookmarkSimilarityDecision.cluster_id
                        == BookmarkSimilarityCluster.id,
                    ),
                )
                .where(
                    BookmarkSimilarityCluster.user_id == user_id,
                    BookmarkSimilarityCluster.run_id == run_id,
                    BookmarkSimilarityDecision.decision == "merge_to_homepage",
                )
                .order_by(
                    BookmarkSimilarityCluster.first_source_sequence,
                    BookmarkSimilarityCluster.id,
                )
            )
        ).all()
    )
    if not clusters:
        return {}, 0

    member_rows = (
        await session.execute(
            select(
                BookmarkSimilarityClusterMember.cluster_id,
                BookmarkSimilarityClusterMember.candidate_id,
                BookmarkSimilarityClusterMember.first_source_sequence,
            )
            .join(
                BookmarkSimilarityDecision,
                and_(
                    BookmarkSimilarityDecision.user_id
                    == BookmarkSimilarityClusterMember.user_id,
                    BookmarkSimilarityDecision.run_id
                    == BookmarkSimilarityClusterMember.run_id,
                    BookmarkSimilarityDecision.cluster_id
                    == BookmarkSimilarityClusterMember.cluster_id,
                ),
            )
            .where(
                BookmarkSimilarityClusterMember.user_id == user_id,
                BookmarkSimilarityClusterMember.run_id == run_id,
                BookmarkSimilarityDecision.decision == "merge_to_homepage",
            )
            .order_by(
                BookmarkSimilarityClusterMember.cluster_id,
                BookmarkSimilarityClusterMember.first_source_sequence,
                BookmarkSimilarityClusterMember.candidate_id,
            )
        )
    ).all()
    members: defaultdict[str, list[tuple[str, int]]] = defaultdict(list)
    for cluster_id, candidate_id, first_sequence in member_rows:
        members[cluster_id].append((candidate_id, first_sequence))

    folder_rows = (
        await session.execute(
            select(
                BookmarkSimilarityClusterMember.cluster_id,
                BookmarkStagingCandidateFolder.first_source_sequence,
                BookmarkStagingCandidateFolder.occurrence_count,
                BookmarkStagingFolder.display_path,
            )
            .join(
                BookmarkSimilarityDecision,
                and_(
                    BookmarkSimilarityDecision.user_id
                    == BookmarkSimilarityClusterMember.user_id,
                    BookmarkSimilarityDecision.run_id
                    == BookmarkSimilarityClusterMember.run_id,
                    BookmarkSimilarityDecision.cluster_id
                    == BookmarkSimilarityClusterMember.cluster_id,
                ),
            )
            .join(
                BookmarkStagingCandidateFolder,
                and_(
                    BookmarkStagingCandidateFolder.user_id
                    == BookmarkSimilarityClusterMember.user_id,
                    BookmarkStagingCandidateFolder.run_id
                    == BookmarkSimilarityClusterMember.run_id,
                    BookmarkStagingCandidateFolder.candidate_id
                    == BookmarkSimilarityClusterMember.candidate_id,
                ),
            )
            .join(
                BookmarkStagingFolder,
                and_(
                    BookmarkStagingFolder.user_id
                    == BookmarkStagingCandidateFolder.user_id,
                    BookmarkStagingFolder.run_id
                    == BookmarkStagingCandidateFolder.run_id,
                    BookmarkStagingFolder.id == BookmarkStagingCandidateFolder.folder_id,
                ),
                isouter=True,
            )
            .where(
                BookmarkSimilarityClusterMember.user_id == user_id,
                BookmarkSimilarityClusterMember.run_id == run_id,
                BookmarkSimilarityDecision.decision == "merge_to_homepage",
            )
            .order_by(
                BookmarkSimilarityClusterMember.cluster_id,
                BookmarkStagingCandidateFolder.first_source_sequence,
            )
        )
    ).all()
    paths: defaultdict[str, list[tuple[str, ...]]] = defaultdict(list)
    path_weights: defaultdict[str, Counter[tuple[str, ...]]] = defaultdict(Counter)
    for cluster_id, _sequence, occurrence_count, display_path in folder_rows:
        path = _folder_path(display_path)
        if not path:
            continue
        paths[cluster_id].append(path)
        path_weights[cluster_id][path] += int(occurrence_count)

    by_candidate: dict[str, _MergePlan] = {}
    reduction = 0
    for cluster in clusters:
        cluster_members = members.get(cluster.id, [])
        member_ids = frozenset(candidate_id for candidate_id, _ in cluster_members)
        if len(member_ids) != cluster.candidate_count:
            raise BookmarkApplyError(
                409,
                "bookmark_similarity_projection_invalid",
                "相似书签预览不完整，请重新解析书签文件",
            )
        if cluster.canonical_candidate_id is not None:
            if cluster.canonical_candidate_id not in member_ids:
                raise BookmarkApplyError(
                    409,
                    "bookmark_similarity_projection_invalid",
                    "相似书签推荐主页与候选不一致，请重新解析书签文件",
                )
            trigger_candidate_id = cluster.canonical_candidate_id
        else:
            trigger_candidate_id = min(cluster_members, key=lambda item: (item[1], item[0]))[0]
        representative = ()
        if path_weights[cluster.id]:
            representative = min(
                path_weights[cluster.id],
                key=lambda path: (-path_weights[cluster.id][path], len(path), path),
            )
        plan = _MergePlan(
            cluster_id=cluster.id,
            canonical_url=cluster.canonical_url,
            canonical_title=cluster.canonical_title,
            canonical_candidate_id=cluster.canonical_candidate_id,
            trigger_candidate_id=trigger_candidate_id,
            member_ids=member_ids,
            folder_path=representative,
            tag_names=_folder_tag_names(tuple(paths[cluster.id])),
            merge_create_count=cluster.merge_create_count,
            reduction_count=(
                cluster.keep_original_create_count - cluster.merge_create_count
            ),
        )
        for candidate_id in member_ids:
            by_candidate[candidate_id] = plan
        reduction += plan.reduction_count
    return by_candidate, reduction


def _site_name(title: str, host: str) -> str:
    name = " ".join(title.split())[:160]
    return name or host[:160]


def _url_path(value: str) -> str:
    try:
        return urlsplit(value).path
    except ValueError:
        return ""


def _candidate_category(
    *,
    folder_path: tuple[str, ...],
    title: str,
    host: str,
    identity_url: str,
    host_history: dict[str, CategorySuggestion],
    category_taxonomy: dict[str, tuple[str, str]],
) -> CategorySuggestion:
    exact_folder: CategorySuggestion | None = None
    for folder_name in reversed(meaningful_folder_path(folder_path)):
        existing = category_taxonomy.get(folder_name.strip().casefold())
        if existing is not None:
            exact_folder = CategorySuggestion(
                existing[1],
                "high",
                (f"existing_category_folder:{existing[0]}",),
            )
            break
    history_key = normalized_history_host(identity_url)
    history = host_history.get(history_key) if history_key is not None else None
    if history is not None:
        return history
    if exact_folder is not None:
        return exact_folder
    return suggest_category(folder_path, title, host, _url_path(identity_url))


async def _next_category_position(
    session: AsyncSession,
    user_id: str,
    category_id: str,
    cache: dict[str, int],
) -> int:
    if category_id not in cache:
        maximum = await session.scalar(
            select(func.max(Site.position)).where(
                Site.user_id == user_id,
                Site.category_id == category_id,
            )
        )
        cache[category_id] = int(maximum) if maximum is not None else -1
    cache[category_id] += 1
    return cache[category_id]


async def _attach_tags(
    session: AsyncSession,
    user_id: str,
    site_id: str,
    tag_names: tuple[str, ...],
    tag_cache: dict[str, str],
    *,
    check_existing: bool,
) -> bool:
    changed = False
    for tag_name in tag_names:
        tag_id = await _ensure_tag(session, user_id, tag_name, tag_cache)
        existing = None
        if check_existing:
            existing = await session.scalar(
                select(SiteTag.site_id).where(
                    SiteTag.user_id == user_id,
                    SiteTag.site_id == site_id,
                    SiteTag.tag_id == tag_id,
                )
            )
        if existing is None:
            session.add(SiteTag(user_id=user_id, site_id=site_id, tag_id=tag_id))
            changed = True
    return changed


async def apply_candidates(
    session: AsyncSession,
    user_id: str,
    run_id: str,
    *,
    batch_size: int = BATCH_SIZE,
) -> ApplyOutcome:
    """Stage all Site writes in the caller's single import transaction."""

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
    merge_by_candidate, merged_candidates = await _merge_plans(session, user_id, run_id)
    if total and not await reserve_account_taxonomy(session, user_id):
        raise BookmarkApplyError(
            409,
            "bookmark_apply_conflict",
            "账号状态已发生变化，请重新发起导入",
        )
    host_history = await load_account_host_category_history(session, user_id)
    category_taxonomy = await _category_taxonomy(session, user_id)
    tag_cache = await _tag_taxonomy(session, user_id)
    category_positions: dict[str, int] = {}
    claimed: set[str] = set()
    processed_merge_clusters: set[str] = set()
    cursor: tuple[int, str] | None = None
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

        actionable: list[tuple[BookmarkStagingCandidate, _MergePlan | None]] = []
        for row in batch:
            if row.proposed_action in _SKIPPED_ACTIONS:
                skipped_needs_review += 1
                continue
            merge_plan = merge_by_candidate.get(row.id)
            if merge_plan is not None:
                if (
                    merge_plan.cluster_id in processed_merge_clusters
                    or row.id != merge_plan.trigger_candidate_id
                ):
                    if row.proposed_action != "create":
                        skipped_existing += 1
                    continue
                processed_merge_clusters.add(merge_plan.cluster_id)
            actionable.append((row, merge_plan))

        folder_paths = await _candidate_folder_paths(
            session,
            user_id,
            run_id,
            [row.id for row, merge_plan in actionable if merge_plan is None],
        )
        target_urls = [
            merge_plan.canonical_url if merge_plan is not None else row.identity_url
            for row, merge_plan in actionable
        ]
        present = await _existing_sites(
            session,
            user_id,
            target_urls,
        )

        now = utc_now()
        for row, merge_plan in actionable:
            source_url = merge_plan.canonical_url if merge_plan is not None else row.identity_url
            normalized = normalize_bookmark_url(source_url)
            if (
                normalized.status is not NormalizationStatus.ACCEPTED
                or not normalized.normalized_url
            ):
                failed += 1
                continue
            original_url = source_url.strip()
            identity_url = normalized.normalized_url
            if identity_url in claimed:
                skipped_existing += 1
                continue

            existing_entry = present.get(identity_url)
            if existing_entry is not None and merge_plan is None:
                skipped_existing += 1
                claimed.add(identity_url)
                continue

            folder_path = (
                merge_plan.folder_path
                if merge_plan is not None
                else folder_paths.get(row.id, ())
            )
            title = merge_plan.canonical_title if merge_plan is not None else row.display_title
            host = normalized.host or row.host
            existing_site = existing_entry[0] if existing_entry is not None else None
            preference = existing_entry[1] if existing_entry is not None else None
            category_is_manual = bool(
                existing_site is not None
                and preference is not None
                and preference.category_is_manual
            )
            if category_is_manual:
                category_id = existing_site.category_id
            else:
                suggestion = _candidate_category(
                    folder_path=folder_path,
                    title=title,
                    host=host,
                    identity_url=identity_url,
                    host_history=host_history,
                    category_taxonomy=category_taxonomy,
                )
                category_name = suggestion.category or DEFAULT_CATEGORY_NAME
                category_id = await _ensure_category(
                    session,
                    user_id,
                    category_name,
                    category_taxonomy,
                )
            tags_are_manual = bool(
                existing_site is not None
                and preference is not None
                and preference.tags_are_manual
            )
            tag_names = (
                ()
                if tags_are_manual
                else (
                    merge_plan.tag_names
                    if merge_plan is not None
                    else _folder_tag_names((folder_path,))
                )
            )

            if existing_entry is not None:
                assert existing_site is not None
                changed = False
                if merge_plan is not None:
                    if (
                        not category_is_manual
                        and existing_site.category_id != category_id
                    ):
                        existing_site.category_id = category_id
                        existing_site.position = await _next_category_position(
                            session,
                            user_id,
                            category_id,
                            category_positions,
                        )
                        changed = True
                        if preference is not None:
                            preference.category_is_llm = False
                    if not tags_are_manual:
                        tags_changed = await _attach_tags(
                            session,
                            user_id,
                            existing_site.id,
                            tag_names,
                            tag_cache,
                            check_existing=True,
                        )
                        changed = tags_changed or changed
                        if tags_changed and preference is not None:
                            preference.tags_are_llm = False
                    if changed:
                        existing_site.version += 1
                        existing_site.updated_at = now
                if merge_plan is None or (
                    merge_plan.merge_create_count > 0
                    or row.proposed_action != "create"
                ):
                    skipped_existing += 1
                claimed.add(identity_url)
                continue

            # A merge containing only already-existing child pages must not
            # create a new root behind the user's back. It can only suppress
            # candidates that this import itself would otherwise create.
            if merge_plan is not None and merge_plan.merge_create_count == 0:
                if row.proposed_action != "create":
                    skipped_existing += 1
                claimed.add(identity_url)
                continue

            site_pos = await _next_category_position(
                session,
                user_id,
                category_id,
                category_positions,
            )
            site_id = new_id()
            name = _site_name(title, host)
            session.add(
                Site(
                    id=site_id,
                    user_id=user_id,
                    category_id=category_id,
                    name=name,
                    normalized_name=name.casefold(),
                    original_url=original_url,
                    identity_url=identity_url,
                    position=site_pos,
                    summary="",
                    description="",
                    favicon_url=None,
                    preview_url=None,
                    pinned=False,
                    source="browser_import",
                    analysis_status="not_analyzed",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            await _attach_tags(
                session,
                user_id,
                site_id,
                tag_names,
                tag_cache,
                check_existing=False,
            )
            claimed.add(identity_url)
            created += 1
        await session.flush()
    return ApplyOutcome(
        total_candidates=total,
        created=created,
        skipped_existing=skipped_existing,
        skipped_needs_review=skipped_needs_review,
        merged_candidates=merged_candidates,
        failed=failed,
    )


__all__ = [
    "BATCH_SIZE",
    "ApplyOutcome",
    "BookmarkApplyError",
    "apply_candidates",
    "category_distribution",
]


async def category_distribution(
    session: AsyncSession,
    user_id: str,
    run_id: str,
    *,
    batch_size: int = BATCH_SIZE,
) -> dict[str, int]:
    """Count how the staged candidates would fall across categories.

    This exists so an Agent can reason about a 2000-bookmark import without the
    2000 rows entering its context: it returns roughly a dozen counts computed
    entirely server-side, using the same classifier apply will use.  Shipping the
    candidate list to a model instead would cost hundreds of thousands of tokens
    and tell it nothing the aggregate does not.
    """

    counts: dict[str, int] = {}
    cursor: tuple[int, str] | None = None
    host_history = await load_account_host_category_history(session, user_id)
    category_taxonomy = await _category_taxonomy(session, user_id)
    while True:
        conditions: list[Any] = [
            BookmarkStagingCandidate.user_id == user_id,
            BookmarkStagingCandidate.run_id == run_id,
            BookmarkStagingCandidate.proposed_action.not_in(tuple(_SKIPPED_ACTIONS)),
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
        folder_paths = await _candidate_folder_paths(
            session,
            user_id,
            run_id,
            [row.id for row in batch],
        )
        for row in batch:
            suggestion = _candidate_category(
                folder_path=folder_paths.get(row.id, ()),
                title=row.display_title,
                host=row.host,
                identity_url=row.identity_url,
                host_history=host_history,
                category_taxonomy=category_taxonomy,
            )
            name = suggestion.category or DEFAULT_CATEGORY_NAME
            counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
