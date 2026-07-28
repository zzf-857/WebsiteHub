"""Apply a fetch outcome to one stored Site.

The rule that shapes everything here: **analysis fills blanks, it never
overwrites the user.**  A name or description the user typed is a decision;
a page's ``<title>`` is a guess about that decision.  Guesses lose.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import and_, case, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.database import Database
from webhub.db.locking import reserve_account_taxonomy
from webhub.db.models import (
    Category,
    Site,
    SiteMetadataPreference,
    SiteTag,
    Tag,
    utc_now,
)

from .enrichment import (
    MAX_NEW_SITE_TAGS,
    MAX_SITE_TAGS,
    MIN_SITE_TAGS,
    SiteCategoryOption,
    SiteEnricher,
    SiteEnrichmentRequest,
    SiteEnrichmentResult,
    SiteEnrichmentUnavailableError,
    SiteTagOption,
    normalize_site_description,
    normalize_site_tag_name,
)
from .fetcher import DEFAULT_TIMEOUT_SECONDS, FetchOutcome, fetch_site_metadata

_LOGGER = logging.getLogger(__name__)

MAX_DESCRIPTION_CHARS = 1_000
AUTO_PENDING_STALE_AFTER = timedelta(minutes=5)
AUTO_PARTIAL_RETRY_AFTER = timedelta(minutes=30)
_LEGACY_FAVICON_PREFIXES = (
    "https://www.google.com/s2/favicons?domain=",
    "https://www.google.com/s2/favicons?domain_url=",
)


@dataclass(frozen=True, slots=True)
class AnalysisClaim:
    user_id: str
    site_id: str
    url: str
    version: int
    claimed_at: datetime
    partial_retry: bool = False
    missing_description: bool = False
    missing_icon: bool = False


@dataclass(frozen=True, slots=True)
class AnalysisProviderSignal:
    """One Provider invocation result, independent from the final Site CAS."""

    failed: bool | None
    stop_batch: bool


@dataclass(frozen=True, slots=True)
class _EnrichmentPreferenceSnapshot:
    description_is_manual: bool
    favicon_is_manual: bool
    category_is_manual: bool
    tags_are_manual: bool
    description_is_llm: bool
    category_is_llm: bool
    tags_are_llm: bool
    llm_analyzed_at: datetime | None


@dataclass(frozen=True, slots=True)
class _EnrichmentSnapshot:
    request: SiteEnrichmentRequest
    preference: _EnrichmentPreferenceSnapshot


_ACTIVE_CLAIMS: dict[tuple[str, str], datetime] = {}


def _claim_key(user_id: str, site_id: str) -> tuple[str, str]:
    return user_id, site_id


def _claim_is_current(claim: AnalysisClaim) -> bool:
    return _ACTIVE_CLAIMS.get(_claim_key(claim.user_id, claim.site_id)) == claim.claimed_at


def _release_claim(claim: AnalysisClaim) -> None:
    key = _claim_key(claim.user_id, claim.site_id)
    if _ACTIVE_CLAIMS.get(key) == claim.claimed_at:
        _ACTIVE_CLAIMS.pop(key, None)


def _blank(value: str | None) -> bool:
    return value is None or not value.strip()


def _is_legacy_favicon_url(value: str | None) -> bool:
    return bool(value and value.strip().casefold().startswith(_LEGACY_FAVICON_PREFIXES))


def _description_allows_derived_value() -> object:
    """A missing preference means the user has not made a description choice."""

    return ~select(SiteMetadataPreference.site_id).where(
        SiteMetadataPreference.user_id == Site.user_id,
        SiteMetadataPreference.site_id == Site.id,
        SiteMetadataPreference.description_is_manual.is_(True),
    ).exists()


def _favicon_allows_derived_value() -> object:
    """Keep an explicitly chosen or cleared favicon untouched."""

    return ~select(SiteMetadataPreference.site_id).where(
        SiteMetadataPreference.user_id == Site.user_id,
        SiteMetadataPreference.site_id == Site.id,
        SiteMetadataPreference.favicon_is_manual.is_(True),
    ).exists()


def _legacy_favicon_condition() -> object:
    """Recognize only the retired generated URL shape, never arbitrary CDNs."""

    normalized = func.lower(Site.favicon_url)
    return or_(
        *(normalized.like(f"{prefix}%") for prefix in _LEGACY_FAVICON_PREFIXES),
    )


def _partial_metadata_condition() -> object:
    """Sites whose basic library identity is still visibly incomplete."""

    return or_(
        and_(
            _description_allows_derived_value(),
            or_(Site.description.is_(None), func.trim(Site.description) == ""),
        ),
        and_(
            _favicon_allows_derived_value(),
            or_(
                Site.favicon_url.is_(None),
                func.trim(Site.favicon_url) == "",
                _legacy_favicon_condition(),
            ),
        ),
    )


def _backfill_metadata_condition() -> object:
    """Metadata work that never implies permission to spend model tokens."""

    return or_(
        _partial_metadata_condition(),
        and_(
            or_(Site.preview_url.is_(None), func.trim(Site.preview_url) == ""),
            ~select(SiteMetadataPreference.site_id)
            .where(
                SiteMetadataPreference.user_id == Site.user_id,
                SiteMetadataPreference.site_id == Site.id,
                SiteMetadataPreference.preview_checked_at.is_not(None),
            )
            .exists(),
        ),
    )


def llm_enrichment_missing_condition() -> object:
    """A completed three-tool enrichment is recorded outside ``sites``."""

    return ~select(SiteMetadataPreference.site_id).where(
        SiteMetadataPreference.user_id == Site.user_id,
        SiteMetadataPreference.site_id == Site.id,
        or_(
            SiteMetadataPreference.llm_analyzed_at.is_not(None),
            and_(
                SiteMetadataPreference.description_is_manual.is_(True),
                SiteMetadataPreference.category_is_manual.is_(True),
                SiteMetadataPreference.tags_are_manual.is_(True),
            ),
        ),
    ).exists()


def _manual_backfill_condition() -> object:
    """Explicit Q17 runs may also spend tokens on never-enriched sites."""

    return or_(_backfill_metadata_condition(), llm_enrichment_missing_condition())


def _automatic_partial_retry_condition(*, partial_before: datetime) -> object:
    """Retry derived blanks once, but never overwrite a later user decision."""

    return and_(
        Site.analysis_status == "complete",
        _partial_metadata_condition(),
        Site.analysis_updated_at.is_not(None),
        Site.analysis_updated_at < partial_before,
        # `analysis_updated_at` is derived-only. A later user edit, including
        # an intentional clear, must keep the record out of automatic retries.
        Site.updated_at <= Site.analysis_updated_at,
    )


def _automatic_eligibility(*, stale_before: datetime, partial_before: datetime) -> object:
    return or_(
        and_(Site.analysis_status == "not_analyzed", _backfill_metadata_condition()),
        and_(
            Site.analysis_status == "pending",
            or_(
                Site.analysis_updated_at.is_(None),
                Site.analysis_updated_at < stale_before,
            ),
        ),
        _automatic_partial_retry_condition(partial_before=partial_before),
    )


def metadata_backfill_eligibility(*, stale_before: datetime) -> object:
    """Work that a user explicitly asked a durable metadata run to revisit.

    Unlike the quiet automatic sweep, an explicit run may revisit an immediately
    incomplete ``complete`` record.  The version guard used when claiming still
    makes an intentional user clear or any later edit win over this derived
    write.  A fresh ``pending`` record is deliberately excluded: it belongs to
    another live request until its claim becomes stale.
    """

    metadata_status = or_(
        Site.analysis_status == "not_analyzed",
        and_(
            Site.analysis_status.in_(("failed", "limited")),
            or_(
                Site.analysis_updated_at.is_(None),
                Site.updated_at <= Site.analysis_updated_at,
            ),
        ),
        and_(
            Site.analysis_status == "pending",
            or_(
                Site.analysis_updated_at.is_(None),
                Site.analysis_updated_at < stale_before,
            ),
            or_(
                Site.analysis_updated_at.is_(None),
                Site.updated_at <= Site.analysis_updated_at,
            ),
        ),
        and_(
            Site.analysis_status == "complete",
            Site.analysis_updated_at.is_not(None),
            Site.updated_at <= Site.analysis_updated_at,
        ),
    )
    llm_status = or_(
        Site.analysis_status != "pending",
        Site.analysis_updated_at.is_(None),
        Site.analysis_updated_at < stale_before,
    )
    return or_(
        and_(_backfill_metadata_condition(), metadata_status),
        and_(llm_enrichment_missing_condition(), llm_status),
    )


async def _claim_analysis(
    session: AsyncSession,
    user_id: str,
    site_id: str,
    *,
    automatic: bool = False,
    bulk: bool = False,
    stale_before: datetime | None = None,
    expected_version: int | None = None,
    expected_analysis_status: str | None = None,
    expected_analysis_claimed_at: datetime | None = None,
    on_claimed: Callable[[AsyncSession, AnalysisClaim], Awaitable[bool]] | None = None,
) -> AnalysisClaim | None:
    ownership = [Site.user_id == user_id, Site.id == site_id]
    # A durable batch snapshots the user-visible version and analysis state
    # before it starts.  Do not turn an old, queued fetch into a write after a
    # user edits the bookmark or another analysis has already claimed it.
    if expected_version is not None:
        ownership.append(Site.version == expected_version)
    if automatic and bulk:
        raise ValueError("automatic and bulk analysis modes cannot be combined")
    cutoff = stale_before or (utc_now() - AUTO_PENDING_STALE_AFTER)
    if expected_analysis_status is not None:
        if bulk:
            if expected_analysis_claimed_at is not None:
                # The timestamp is written in the same transaction as this
                # run's pending claim. It is the only safe way to resume a
                # 90-second item lease without stealing a fresh foreground
                # analysis or waiting for the broader five-minute stale rule.
                expected_status = or_(
                    and_(
                        Site.analysis_status == "pending",
                        Site.analysis_updated_at == expected_analysis_claimed_at,
                    ),
                    Site.analysis_status == "not_analyzed",
                )
            else:
                # Metadata-only work may legitimately finish between the run
                # snapshot and this claim. User edits are already excluded by
                # expected_version; only a fresh pending owner must be deferred.
                expected_status = or_(
                    Site.analysis_status != "pending",
                    Site.analysis_updated_at.is_(None),
                    Site.analysis_updated_at < cutoff,
                )
        else:
            expected_status = Site.analysis_status == expected_analysis_status
        ownership.append(expected_status)
    if automatic:
        ownership.append(
            _automatic_eligibility(
                stale_before=cutoff,
                partial_before=utc_now() - AUTO_PARTIAL_RETRY_AFTER,
            )
        )
    elif bulk:
        # A recovered item has already been validated against its immutable
        # version/status snapshot above. Its own persisted pending token is
        # stricter than the generic stale-pending rule, which must otherwise
        # keep protecting unrelated foreground work.
        ownership.append(
            _manual_backfill_condition()
            if expected_analysis_claimed_at is not None
            else metadata_backfill_eligibility(stale_before=cutoff)
        )
    else:
        # A normal in-process queue cannot see a durable Q17 lease owned by
        # another process. Refuse to steal a fresh database claim even when
        # the local deduplication map has no entry for it.
        ownership.append(
            or_(
                Site.analysis_status != "pending",
                Site.analysis_updated_at.is_(None),
                Site.analysis_updated_at < cutoff,
            )
        )
    site = await session.scalar(select(Site).where(*ownership))
    if site is None:
        return None

    key = _claim_key(user_id, site_id)
    previous = _ACTIVE_CLAIMS.get(key)
    claimed_at = utc_now()
    if previous is not None and claimed_at <= previous:
        claimed_at = previous + timedelta(microseconds=1)
    _ACTIVE_CLAIMS[key] = claimed_at
    claim = AnalysisClaim(
        user_id=user_id,
        site_id=site_id,
        url=site.original_url,
        version=site.version,
        claimed_at=claimed_at,
        partial_retry=automatic and site.analysis_status == "complete",
        missing_description=_blank(site.description),
        missing_icon=(
            _blank(site.favicon_url) or _is_legacy_favicon_url(site.favicon_url)
        ),
    )
    try:
        claimed = await session.execute(
            update(Site)
            .where(
                *ownership,
                Site.original_url == site.original_url,
                Site.version == site.version,
            )
            .values(
                analysis_status="pending",
                analysis_updated_at=claimed_at,
                # ``updated_at`` has a Python onupdate default. Explicitly keep
                # it unchanged: derived analysis is not a user edit.
                updated_at=Site.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            if _ACTIVE_CLAIMS.get(key) == claimed_at:
                if previous is None:
                    _ACTIVE_CLAIMS.pop(key, None)
                else:
                    _ACTIVE_CLAIMS[key] = previous
            return None
        if on_claimed is not None and not await on_claimed(session, claim):
            await session.rollback()
            if _ACTIVE_CLAIMS.get(key) == claimed_at:
                if previous is None:
                    _ACTIVE_CLAIMS.pop(key, None)
                else:
                    _ACTIVE_CLAIMS[key] = previous
            return None
        await session.commit()
    except BaseException:
        if _ACTIVE_CLAIMS.get(key) == claimed_at:
            if previous is None:
                _ACTIVE_CLAIMS.pop(key, None)
            else:
                _ACTIVE_CLAIMS[key] = previous
        raise
    return claim


async def auto_backfill_site_ids(
    session: AsyncSession,
    user_id: str,
    *,
    limit: int,
    excluded_site_ids: frozenset[str] = frozenset(),
    stale_before: datetime | None = None,
) -> list[str]:
    """Discover fresh, abandoned, and once-retryable partial analysis work."""

    cutoff = stale_before or (utc_now() - AUTO_PENDING_STALE_AFTER)
    partial_cutoff = utc_now() - AUTO_PARTIAL_RETRY_AFTER
    selected: list[str] = []
    excluded = set(excluded_site_ids)

    async def extend(
        eligibility: object,
        *ordering: object,
    ) -> None:
        remaining = limit - len(selected)
        if remaining <= 0:
            return
        conditions = [Site.user_id == user_id, eligibility]
        if excluded:
            conditions.append(Site.id.not_in(excluded))
        site_ids = list(
            (
                await session.scalars(
                    select(Site.id)
                    .where(*conditions)
                    .order_by(*ordering)
                    .limit(remaining)
                )
            ).all()
        )
        selected.extend(site_ids)
        excluded.update(site_ids)

    # These are deliberately separate indexed queries rather than one
    # `OR` plus conditional ordering expression. A sweep over 10,000 old
    # bookmarks otherwise asks SQLite to scan and sort the remaining corpus for
    # every tiny discovery batch. The model index orders exactly these columns.
    await extend(
        and_(
            Site.analysis_status == "not_analyzed",
            _backfill_metadata_condition(),
        ),
        Site.analysis_updated_at,
        Site.created_at,
        Site.id,
    )
    await extend(
        and_(
            Site.analysis_status == "pending",
            Site.analysis_updated_at.is_(None),
        ),
        Site.created_at,
        Site.id,
    )
    await extend(
        and_(
            Site.analysis_status == "pending",
            Site.analysis_updated_at < cutoff,
        ),
        Site.analysis_updated_at,
        Site.created_at,
        Site.id,
    )
    await extend(
        _automatic_partial_retry_condition(partial_before=partial_cutoff),
        Site.analysis_updated_at,
        Site.created_at,
        Site.id,
    )
    return selected


async def recent_not_analyzed_site_ids(
    session: AsyncSession,
    user_id: str,
    *,
    limit: int,
) -> list[str]:
    """Return a tiny newest-first slice for a just-completed import.

    This is deliberately separate from the oldest-first background sweep.  A
    large historical library must make forward progress fairly, while the few
    links the user just imported should begin showing basic metadata promptly.
    """

    if limit <= 0:
        return []
    return list(
        (
            await session.scalars(
                select(Site.id)
                .where(
                    Site.user_id == user_id,
                    Site.analysis_status == "not_analyzed",
                )
                .order_by(Site.created_at.desc(), Site.id.desc())
                .limit(limit)
            )
        ).all()
    )


async def not_analyzed_site_ids(
    session: AsyncSession,
    user_id: str,
    *,
    limit: int,
    excluded_site_ids: frozenset[str] = frozenset(),
) -> tuple[list[str], int]:
    """Select manual retry work without stealing a fresh background claim."""

    conditions = [
        Site.user_id == user_id,
        Site.analysis_status.in_(("not_analyzed", "failed", "limited")),
    ]
    if excluded_site_ids:
        conditions.append(Site.id.not_in(excluded_site_ids))
    total = int(
        await session.scalar(select(func.count()).select_from(Site).where(*conditions)) or 0
    )
    site_ids = list(
        (
            await session.scalars(
                select(Site.id).where(*conditions).order_by(Site.created_at, Site.id).limit(limit)
            )
        ).all()
    )
    return site_ids, max(total - len(site_ids), 0)


def _safe_image_url(url: str | None) -> str | None:
    """Only http(s) URLs; both columns feed an ``img src``."""

    if not url:
        return None
    candidate = url.strip()
    if not candidate.lower().startswith(("http://", "https://")):
        return None
    return candidate[:2_048]


def _preference_snapshot(
    preference: SiteMetadataPreference | None,
) -> _EnrichmentPreferenceSnapshot:
    return _EnrichmentPreferenceSnapshot(
        description_is_manual=bool(preference and preference.description_is_manual),
        favicon_is_manual=bool(preference and preference.favicon_is_manual),
        category_is_manual=bool(preference and preference.category_is_manual),
        tags_are_manual=bool(preference and preference.tags_are_manual),
        description_is_llm=bool(preference and preference.description_is_llm),
        category_is_llm=bool(preference and preference.category_is_llm),
        tags_are_llm=bool(preference and preference.tags_are_llm),
        llm_analyzed_at=preference.llm_analyzed_at if preference else None,
    )


def _hostname(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


async def _load_enrichment_snapshot(
    session: AsyncSession,
    claim: AnalysisClaim,
    outcome: FetchOutcome,
) -> _EnrichmentSnapshot | None:
    """Freeze model inputs only while the analysis claim is still exact."""

    site = await session.scalar(
        select(Site).where(
            Site.user_id == claim.user_id,
            Site.id == claim.site_id,
            Site.original_url == claim.url,
            Site.version == claim.version,
            Site.analysis_status == "pending",
            Site.analysis_updated_at == claim.claimed_at,
        )
    )
    if site is None:
        return None

    categories = tuple(
        (
            await session.scalars(
                select(Category)
                .where(Category.user_id == claim.user_id)
                .order_by(Category.is_default.desc(), Category.normalized_name, Category.id)
            )
        ).all()
    )
    tags = tuple(
        (
            await session.scalars(
                select(Tag)
                .where(Tag.user_id == claim.user_id)
                .order_by(Tag.normalized_name, Tag.id)
            )
        ).all()
    )
    current_tag_ids = tuple(
        (
            await session.scalars(
                select(SiteTag.tag_id)
                .where(
                    SiteTag.user_id == claim.user_id,
                    SiteTag.site_id == claim.site_id,
                )
                .order_by(SiteTag.tag_id)
            )
        ).all()
    )
    preference = await session.get(
        SiteMetadataPreference,
        {"user_id": claim.user_id, "site_id": claim.site_id},
    )
    metadata = outcome.metadata
    request = SiteEnrichmentRequest(
        user_id=claim.user_id,
        site_id=claim.site_id,
        expected_url=claim.url,
        expected_version=claim.version,
        hostname=_hostname(claim.url) or "unknown.host",
        final_hostname=_hostname(outcome.final_url),
        site_name=site.name,
        page_title=metadata.title or "",
        meta_description=metadata.description or "",
        page_text=metadata.page_text,
        current_category_id=site.category_id,
        current_tag_ids=current_tag_ids,
        categories=tuple(
            SiteCategoryOption(
                id=category.id,
                name=category.name,
                is_default=category.is_default,
            )
            for category in categories
        ),
        existing_tags=tuple(SiteTagOption(id=tag.id, name=tag.name) for tag in tags),
    )
    return _EnrichmentSnapshot(
        request=request,
        preference=_preference_snapshot(preference),
    )


def _effective_outcome(
    claim: AnalysisClaim,
    outcome: FetchOutcome,
    *,
    enrichment: SiteEnrichmentResult | None,
    enrichment_error: str | None,
) -> FetchOutcome:
    final = outcome
    if enrichment is not None and outcome.status == "limited":
        final = replace(outcome, status="complete", reason="已完成网页抓取与 LLM 资料分析")
    elif enrichment_error and outcome.status != "failed":
        final = replace(outcome, status="limited", reason=enrichment_error)
    if claim.partial_retry and final.status == "complete":
        repaired_description = (
            not claim.missing_description
            or bool(final.metadata.description)
            or enrichment is not None
        )
        repaired_icon = (
            not claim.missing_icon
            or _safe_image_url(final.metadata.icon_url) is not None
        )
        if not repaired_description or not repaired_icon:
            final = replace(final, status="limited", reason="网站资料仍有部分字段无法补全")
    return final


async def _reject_enrichment_commit(
    session: AsyncSession,
    claim: AnalysisClaim,
    *,
    status: str,
) -> bool:
    await session.rollback()
    await _record_claim_terminal(session, claim, status)
    return False


async def _apply_enriched_outcome(
    session: AsyncSession,
    claim: AnalysisClaim,
    outcome: FetchOutcome,
    snapshot: _EnrichmentSnapshot,
    enrichment: SiteEnrichmentResult,
) -> bool:
    """Validate the frozen model draft and commit every derived field once."""

    await session.rollback()
    if enrichment.category_id not in {
        option.id for option in snapshot.request.categories
    }:
        return await _reject_enrichment_commit(session, claim, status="limited")
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "sqlite":
        # The project runs on SQLite today. Reserve the one writer before the
        # fresh snapshot so no taxonomy or position change can slip between
        # validation and commit.
        await session.execute(text("BEGIN IMMEDIATE"))
    # Provider I/O has already finished. The shared account mutex blocks only
    # the short taxonomy commit phase and is also honored by ordinary library
    # category/tag writers, so the snapshot cannot acquire a phantom row.
    if not await reserve_account_taxonomy(session, claim.user_id):
        return await _reject_enrichment_commit(session, claim, status="not_analyzed")
    reserved_category = await session.execute(
        update(Category)
        .where(
            Category.user_id == claim.user_id,
            Category.id == enrichment.category_id,
        )
        .values(updated_at=Category.updated_at)
    )
    if reserved_category.rowcount != 1:  # type: ignore[attr-defined]
        return await _reject_enrichment_commit(session, claim, status="not_analyzed")

    site_statement = select(Site).where(
        Site.user_id == claim.user_id,
        Site.id == claim.site_id,
        Site.original_url == claim.url,
        Site.version == claim.version,
        Site.analysis_status == "pending",
        Site.analysis_updated_at == claim.claimed_at,
    )
    if dialect_name != "sqlite":
        site_statement = site_statement.with_for_update()
    site = await session.scalar(site_statement)
    if site is None:
        return await _reject_enrichment_commit(session, claim, status="not_analyzed")

    category_rows = tuple(
        (
            await session.execute(
                select(Category.id, Category.name, Category.is_default)
                .where(Category.user_id == claim.user_id)
                .order_by(Category.is_default.desc(), Category.normalized_name, Category.id)
            )
        ).all()
    )
    tag_rows = tuple(
        (
            await session.execute(
                select(Tag.id, Tag.name, Tag.normalized_name)
                .where(Tag.user_id == claim.user_id)
                .order_by(Tag.normalized_name, Tag.id)
            )
        ).all()
    )
    current_tag_ids = tuple(
        (
            await session.scalars(
                select(SiteTag.tag_id)
                .where(
                    SiteTag.user_id == claim.user_id,
                    SiteTag.site_id == claim.site_id,
                )
                .order_by(SiteTag.tag_id)
            )
        ).all()
    )
    preference = await session.get(
        SiteMetadataPreference,
        {"user_id": claim.user_id, "site_id": claim.site_id},
    )

    expected_categories = tuple(
        (option.id, option.name, option.is_default) for option in snapshot.request.categories
    )
    expected_tags = tuple((option.id, option.name) for option in snapshot.request.existing_tags)
    current_categories = tuple(
        (category_id, name, is_default) for category_id, name, is_default in category_rows
    )
    current_tags = tuple((tag_id, name) for tag_id, name, _ in tag_rows)
    snapshot_is_current = (
        site.category_id == snapshot.request.current_category_id
        and current_tag_ids == snapshot.request.current_tag_ids
        and current_categories == expected_categories
        and current_tags == expected_tags
        and _preference_snapshot(preference) == snapshot.preference
    )
    if not snapshot_is_current:
        return await _reject_enrichment_commit(session, claim, status="not_analyzed")

    category_by_id = {
        category_id: (name, is_default)
        for category_id, name, is_default in category_rows
    }
    tag_by_id = {
        tag_id: (name, normalized_name)
        for tag_id, name, normalized_name in tag_rows
    }
    if enrichment.category_id not in category_by_id:
        return await _reject_enrichment_commit(session, claim, status="limited")
    if len(enrichment.new_tag_names) > MAX_NEW_SITE_TAGS:
        return await _reject_enrichment_commit(session, claim, status="limited")

    selected_tag_ids: list[str] = []
    selected_seen: set[str] = set()
    for tag_id in enrichment.existing_tag_ids:
        if tag_id not in tag_by_id:
            return await _reject_enrichment_commit(session, claim, status="limited")
        if tag_id not in selected_seen:
            selected_seen.add(tag_id)
            selected_tag_ids.append(tag_id)

    normalized_new_tags: list[tuple[str, str]] = []
    normalized_new_seen: set[str] = set()
    try:
        description = normalize_site_description(enrichment.description)
        for raw_name in enrichment.new_tag_names:
            display, normalized_name = normalize_site_tag_name(raw_name)
            if normalized_name not in normalized_new_seen:
                normalized_new_seen.add(normalized_name)
                normalized_new_tags.append((display, normalized_name))
    except ValueError:
        return await _reject_enrichment_commit(session, claim, status="limited")

    tag_id_by_normalized = {
        normalized_name: tag_id for tag_id, _, normalized_name in tag_rows
    }
    planned_new_tags: list[tuple[str, str]] = []
    for display, normalized_name in normalized_new_tags:
        existing_id = tag_id_by_normalized.get(normalized_name)
        if existing_id is not None:
            if existing_id not in selected_seen:
                selected_seen.add(existing_id)
                selected_tag_ids.append(existing_id)
            continue
        planned_new_tags.append((display, normalized_name))

    final_tag_count = len(selected_tag_ids) + len(planned_new_tags)
    if not MIN_SITE_TAGS <= final_tag_count <= MAX_SITE_TAGS:
        return await _reject_enrichment_commit(session, claim, status="limited")

    current_category_is_default = category_by_id[site.category_id][1]
    description_writable = (
        not snapshot.preference.description_is_manual
        and (_blank(site.description) or snapshot.preference.description_is_llm)
    )
    category_writable = (
        not snapshot.preference.category_is_manual
        and (current_category_is_default or snapshot.preference.category_is_llm)
    )
    tags_writable = (
        not snapshot.preference.tags_are_manual
        and (not current_tag_ids or snapshot.preference.tags_are_llm)
    )

    target_tag_ids = list(selected_tag_ids)
    if tags_writable and planned_new_tags:
        created_tags = [
            Tag(
                user_id=claim.user_id,
                name=display,
                normalized_name=normalized_name,
            )
            for display, normalized_name in planned_new_tags
        ]
        session.add_all(created_tags)
        await session.flush()
        target_tag_ids.extend(tag.id for tag in created_tags)

    target_category_id = site.category_id
    target_position = site.position
    if category_writable:
        target_category_id = enrichment.category_id
        if target_category_id != site.category_id:
            target_position = int(
                await session.scalar(
                    select(func.coalesce(func.max(Site.position), -1) + 1).where(
                        Site.user_id == claim.user_id,
                        Site.category_id == target_category_id,
                    )
                )
                or 0
            )

    target_tag_set = set(target_tag_ids) if tags_writable else set(current_tag_ids)
    structure_changed = (
        target_category_id != site.category_id or target_tag_set != set(current_tag_ids)
    )
    completed_at = utc_now()
    values: dict[str, object] = {
        "analysis_status": outcome.status,
        "analysis_updated_at": completed_at,
        "updated_at": Site.updated_at,
    }
    if description_writable:
        values["description"] = description

    icon = _safe_image_url(outcome.metadata.icon_url)
    if not snapshot.preference.favicon_is_manual:
        if icon and (_blank(site.favicon_url) or _is_legacy_favicon_url(site.favicon_url)):
            values["favicon_url"] = icon
        elif not icon and _is_legacy_favicon_url(site.favicon_url):
            values["favicon_url"] = None
    preview = _safe_image_url(outcome.metadata.image_url)
    if preview and _blank(site.preview_url):
        values["preview_url"] = preview
    if target_category_id != site.category_id:
        values["category_id"] = target_category_id
        values["position"] = target_position
    if structure_changed:
        values["version"] = Site.version + 1

    applied = await session.execute(
        update(Site)
        .where(
            Site.user_id == claim.user_id,
            Site.id == claim.site_id,
            Site.original_url == claim.url,
            Site.version == claim.version,
            Site.analysis_status == "pending",
            Site.analysis_updated_at == claim.claimed_at,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if applied.rowcount != 1:  # type: ignore[attr-defined]
        return await _reject_enrichment_commit(session, claim, status="not_analyzed")

    if tags_writable and target_tag_set != set(current_tag_ids):
        await session.execute(
            delete(SiteTag).where(
                SiteTag.user_id == claim.user_id,
                SiteTag.site_id == claim.site_id,
            )
        )
        session.add_all(
            SiteTag(user_id=claim.user_id, site_id=claim.site_id, tag_id=tag_id)
            for tag_id in target_tag_ids
        )

    if preference is None:
        preference = SiteMetadataPreference(user_id=claim.user_id, site_id=claim.site_id)
        session.add(preference)
    preference.description_is_llm = description_writable
    preference.category_is_llm = category_writable
    preference.tags_are_llm = tags_writable
    preference.llm_analyzed_at = completed_at
    if outcome.preview_checked:
        preference.preview_checked_at = completed_at

    await session.commit()
    return True


async def apply_outcome(
    session: AsyncSession,
    claim: AnalysisClaim,
    outcome: FetchOutcome,
    *,
    enrichment_snapshot: _EnrichmentSnapshot | None = None,
    enrichment: SiteEnrichmentResult | None = None,
) -> bool:
    """Atomically apply one outcome and report whether its strict write won."""

    if not _claim_is_current(claim):
        return False
    if enrichment_snapshot is not None or enrichment is not None:
        if enrichment_snapshot is None or enrichment is None:
            raise ValueError("enrichment snapshot and result must be supplied together")
        return await _apply_enriched_outcome(
            session,
            claim,
            outcome,
            enrichment_snapshot,
            enrichment,
        )

    completed_at = utc_now()
    final_status = outcome.status
    values: dict[str, object] = {
        "analysis_status": final_status,
        "analysis_updated_at": completed_at,
        "updated_at": Site.updated_at,
    }
    metadata = outcome.metadata
    if metadata.description:
        values["description"] = case(
            (
                and_(
                    _description_allows_derived_value(),
                    or_(Site.description.is_(None), func.trim(Site.description) == ""),
                ),
                metadata.description[:MAX_DESCRIPTION_CHARS],
            ),
            else_=Site.description,
        )
    icon = _safe_image_url(metadata.icon_url)
    if icon:
        values["favicon_url"] = case(
            (
                and_(
                    _favicon_allows_derived_value(),
                    or_(
                        Site.favicon_url.is_(None),
                        func.trim(Site.favicon_url) == "",
                        _legacy_favicon_condition(),
                    ),
                ),
                icon,
            ),
            else_=Site.favicon_url,
        )
    else:
        # Earlier builds stored this exact Google service URL as a synthetic
        # fallback. It is neither a real site icon nor safe to keep rendering;
        # only remove it when there is no recorded manual favicon choice.
        values["favicon_url"] = case(
            (and_(_favicon_allows_derived_value(), _legacy_favicon_condition()), None),
            else_=Site.favicon_url,
        )
    preview = _safe_image_url(metadata.image_url)
    if preview:
        values["preview_url"] = case(
            (
                or_(Site.preview_url.is_(None), func.trim(Site.preview_url) == ""),
                preview,
            ),
            else_=Site.preview_url,
        )

    # Do not load the row and mutate an ORM object here. Sessions deliberately
    # use expire_on_commit=False, so a fetch that took several seconds would be
    # writing decisions based on a stale identity-map snapshot. Every blank
    # check belongs in the UPDATE itself, where it observes the database's
    # current value atomically.
    applied = await session.execute(
        update(Site)
        .where(
            Site.user_id == claim.user_id,
            Site.id == claim.site_id,
            Site.original_url == claim.url,
            Site.version == claim.version,
            Site.analysis_status == "pending",
            Site.analysis_updated_at == claim.claimed_at,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )

    did_apply = applied.rowcount == 1  # type: ignore[attr-defined]
    if did_apply and outcome.preview_checked:
        # A complete HTML read can validly find no og:image. Persist that fact
        # separately from the broad outcome status: an existing name/icon can
        # make the fetch `limited` even though the preview question was fully
        # answered. Truncated pages and transport failures remain eligible for
        # a later explicit retry.
        preference = await session.get(
            SiteMetadataPreference,
            {"user_id": claim.user_id, "site_id": claim.site_id},
        )
        if preference is None:
            session.add(
                SiteMetadataPreference(
                    user_id=claim.user_id,
                    site_id=claim.site_id,
                    preview_checked_at=completed_at,
                )
            )
        else:
            preference.preview_checked_at = completed_at
    if not did_apply and _claim_is_current(claim):
        # Any user edit invalidates metadata from the in-flight fetch. This is
        # stricter than checking each field for blanks: explicitly clearing a
        # description or favicon is itself a user decision and must stay clear.
        # The same URL can still receive the terminal outcome; a changed URL
        # returns to `not_analyzed` because the outcome describes the old page.
        await session.execute(
            update(Site)
            .where(
                Site.user_id == claim.user_id,
                Site.id == claim.site_id,
                Site.analysis_status == "pending",
                Site.analysis_updated_at == claim.claimed_at,
            )
            .values(
                analysis_status=case(
                    (Site.original_url == claim.url, final_status),
                    else_="not_analyzed",
                ),
                analysis_updated_at=completed_at,
                updated_at=Site.updated_at,
            )
            .execution_options(synchronize_session=False)
        )

    # **不 bump version。** version 是给用户可见的并发编辑用的乐观锁；
    # 后台分析是系统侧的补白，涨版本号等于系统跟用户抢锁——用户刚建完网站
    # 马上编辑就会撞上「已被修改，请刷新后重试」，而"改动"是我们自己做的。
    # 分析与用户编辑并发时，version 条件会让本次补白整体作废，保证用户永远赢。
    await session.commit()
    return did_apply


async def _record_claim_terminal(
    session: AsyncSession,
    claim: AnalysisClaim,
    status: str,
) -> None:
    """End only the claim owned by this task; a newer run always wins."""

    await session.rollback()
    if not _claim_is_current(claim):
        return
    completed_at = utc_now()
    await session.execute(
        update(Site)
        .where(
            Site.user_id == claim.user_id,
            Site.id == claim.site_id,
            Site.analysis_status == "pending",
            Site.analysis_updated_at == claim.claimed_at,
        )
        .values(
            analysis_status=case(
                (Site.original_url == claim.url, status),
                else_="not_analyzed",
            ),
            analysis_updated_at=completed_at,
            updated_at=Site.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def analyze_site(
    session: AsyncSession,
    user_id: str,
    site_id: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    automatic: bool = False,
    bulk: bool = False,
    stale_before: datetime | None = None,
    expected_version: int | None = None,
    expected_analysis_status: str | None = None,
    expected_analysis_claimed_at: datetime | None = None,
    on_claimed: Callable[[AsyncSession, AnalysisClaim], Awaitable[bool]] | None = None,
    on_provider_signal: (
        Callable[[AsyncSession, AnalysisProviderSignal], Awaitable[bool]] | None
    ) = None,
    before_provider_call: (
        Callable[[AsyncSession], Awaitable[bool]] | None
    ) = None,
    use_llm: bool = False,
    enricher: SiteEnricher | None = None,
) -> FetchOutcome | None:
    """Mark the site pending, fetch it, and store the result.

    Returns ``None`` when the site does not belong to this account — the same
    answer a missing site gives, so a probe cannot distinguish the two.
    """

    claim = await _claim_analysis(
        session,
        user_id,
        site_id,
        automatic=automatic,
        bulk=bulk,
        stale_before=stale_before,
        expected_version=expected_version,
        expected_analysis_status=expected_analysis_status,
        expected_analysis_claimed_at=expected_analysis_claimed_at,
        on_claimed=on_claimed,
    )
    if claim is None:
        return None

    provider_invoked = False
    local_failure_fuse_attempted = False

    async def stop_bulk_after_local_failure() -> None:
        nonlocal local_failure_fuse_attempted
        if (
            local_failure_fuse_attempted
            or not bulk
            or not provider_invoked
            or on_provider_signal is None
        ):
            return
        try:
            await session.rollback()
            await on_provider_signal(
                session,
                AnalysisProviderSignal(failed=None, stop_batch=True),
            )
        except Exception:  # noqa: BLE001 - preserve the original failure
            _LOGGER.exception(
                "could not stop bulk enrichment after finalization failed for %s",
                site_id,
            )
            return
        local_failure_fuse_attempted = True

    try:
        should_use_llm = use_llm
        if bulk and should_use_llm:
            preference = await session.get(
                SiteMetadataPreference,
                {"user_id": user_id, "site_id": site_id},
            )
            should_use_llm = preference is None or preference.llm_analyzed_at is None
            await session.rollback()
        outcome = await fetch_site_metadata(claim.url, timeout_seconds=timeout_seconds)
        enrichment_snapshot: _EnrichmentSnapshot | None = None
        enrichment: SiteEnrichmentResult | None = None
        enrichment_error: str | None = None
        stop_batch = False
        provider_failed: bool | None = None
        if should_use_llm:
            enrichment_snapshot = await _load_enrichment_snapshot(session, claim, outcome)
            # Never retain a SQLite read snapshot while Provider I/O is in
            # flight. The exact site/taxonomy state is re-read under the final
            # write lock before anything is stored.
            await session.rollback()
            if enrichment_snapshot is None:
                enrichment_error = "网站在分析期间已发生变化"
            elif enricher is None:
                enrichment_error = "LLM 网站分析服务尚未初始化"
                stop_batch = True
            else:
                if before_provider_call is not None:
                    provider_allowed = await before_provider_call(session)
                    # The check opens a short read transaction. Never retain
                    # it across Provider I/O, especially on SQLite.
                    await session.rollback()
                    if not provider_allowed:
                        await _record_claim_terminal(session, claim, "not_analyzed")
                        return None
                try:
                    provider_invoked = True
                    enrichment = await enricher.enrich(enrichment_snapshot.request)
                    provider_failed = False
                except SiteEnrichmentUnavailableError as error:
                    enrichment_error = error.safe_message
                    stop_batch = error.stop_batch
                    if error.provider_failure:
                        provider_failed = True
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - contain optional enrichment
                    _LOGGER.warning(
                        "site enrichment failed for %s (%s)",
                        site_id,
                        type(error).__name__,
                    )
                    enrichment_error = "模型未能完成网站资料分析"
                    # Provider adapters translate remote failures into the
                    # typed exception above. An unknown exception here may be
                    # a local programming/configuration fault. Stop this batch
                    # without pretending it is a retryable Provider failure;
                    # otherwise the same local fault could repeat thousands
                    # of times while poisoning the Provider health streak.
                    stop_batch = True
                    provider_failed = None

        effective_outcome = _effective_outcome(
            claim,
            outcome,
            enrichment=enrichment,
            enrichment_error=enrichment_error,
        )

        async def finalize_outcome() -> bool:
            try:
                if (
                    bulk
                    and on_provider_signal is not None
                    and (stop_batch or provider_failed is not None)
                ):
                    recorded = await on_provider_signal(
                        session,
                        AnalysisProviderSignal(
                            failed=provider_failed,
                            stop_batch=stop_batch,
                        ),
                    )
                    if not recorded:
                        # The durable item lost its lease or another worker
                        # tripped the fuse while this Provider call was in flight.
                        await _record_claim_terminal(session, claim, "not_analyzed")
                        return False
                return await apply_outcome(
                    session,
                    claim,
                    effective_outcome,
                    enrichment_snapshot=(
                        enrichment_snapshot if enrichment is not None else None
                    ),
                    enrichment=enrichment,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # This function is itself shielded after a Provider call, so
                # the fuse is persisted even if its parent is being cancelled.
                await stop_bulk_after_local_failure()
                raise

        if bulk and provider_invoked:
            # Once a paid call has returned, normal task cancellation must not
            # create a second paid call on recovery. Drain the short, fenced
            # database finalization before propagating cancellation.
            finalization_task = asyncio.create_task(finalize_outcome())
            try:
                did_apply = await asyncio.shield(finalization_task)
            except asyncio.CancelledError:
                finalization_result = await asyncio.gather(
                    finalization_task,
                    return_exceptions=True,
                )
                if finalization_result and isinstance(finalization_result[0], Exception):
                    # The finalizer normally records its own fuse. Retry once
                    # if that storage attempt failed while the parent was also
                    # being cancelled.
                    await stop_bulk_after_local_failure()
                raise
        else:
            did_apply = await finalize_outcome()
        if bulk and not did_apply:
            return None
        return effective_outcome
    except asyncio.CancelledError:
        try:
            await _record_claim_terminal(session, claim, "not_analyzed")
        except Exception:  # noqa: BLE001 - cancellation cleanup is best effort and logged
            _LOGGER.exception("could not release cancelled analysis claim for %s", site_id)
        raise
    except Exception:
        # Also covers local faults between Provider return and finalizer setup.
        await stop_bulk_after_local_failure()
        try:
            await _record_claim_terminal(session, claim, "failed")
        except Exception:  # noqa: BLE001 - preserve the original error
            _LOGGER.exception("could not record analysis failure for %s", site_id)
        raise
    finally:
        _release_claim(claim)


async def analyze_in_background(
    database: Database,
    user_id: str,
    site_id: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    automatic: bool = False,
    bulk: bool = False,
    stale_before: datetime | None = None,
    expected_version: int | None = None,
    expected_analysis_status: str | None = None,
    expected_analysis_claimed_at: datetime | None = None,
    on_claimed: Callable[[AsyncSession, AnalysisClaim], Awaitable[bool]] | None = None,
    on_provider_signal: (
        Callable[[AsyncSession, AnalysisProviderSignal], Awaitable[bool]] | None
    ) = None,
    before_provider_call: (
        Callable[[AsyncSession], Awaitable[bool]] | None
    ) = None,
    propagate_errors: bool = False,
    use_llm: bool = False,
    enricher: SiteEnricher | None = None,
) -> FetchOutcome | None:
    """Run one analysis detached from the request that triggered it.

    Saving a site must not wait on someone else's slow server, and must not
    fail because that server is down.
    """

    try:
        async with database.sessions() as session:
            return await analyze_site(
                session,
                user_id,
                site_id,
                timeout_seconds=timeout_seconds,
                automatic=automatic,
                bulk=bulk,
                stale_before=stale_before,
                expected_version=expected_version,
                expected_analysis_status=expected_analysis_status,
                expected_analysis_claimed_at=expected_analysis_claimed_at,
                on_claimed=on_claimed,
                on_provider_signal=on_provider_signal,
                before_provider_call=before_provider_call,
                use_llm=use_llm,
                enricher=enricher,
            )
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - a background failure must stay contained
        _LOGGER.warning("site analysis failed for %s", site_id, exc_info=error)
        if propagate_errors:
            raise
        return None


__all__ = [
    "analyze_in_background",
    "analyze_site",
    "AnalysisClaim",
    "AnalysisProviderSignal",
    "AUTO_PENDING_STALE_AFTER",
    "AUTO_PARTIAL_RETRY_AFTER",
    "apply_outcome",
    "auto_backfill_site_ids",
    "llm_enrichment_missing_condition",
    "metadata_backfill_eligibility",
    "not_analyzed_site_ids",
    "recent_not_analyzed_site_ids",
]
