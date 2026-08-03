"""Deterministic library-wide similarity review and safe merge transactions."""

from __future__ import annotations

import base64
import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.bookmarks.privacy import sensitive_url_keys
from webhub.bookmarks.similarity import (
    is_shared_content_site_key,
    site_key_for_url,
)
from webhub.db.locking import reserve_account_taxonomy
from webhub.db.models import (
    Category,
    Site,
    SiteImportOrigin,
    SiteMetadataPreference,
    SiteSimilarityDecision,
    SiteSimilarityDecisionMember,
    SiteSimilarityGroup,
    SiteSimilarityGroupMember,
    SiteSimilarityScanRun,
    SiteTag,
    Space,
    SpaceMember,
    Tag,
    new_id,
    utc_now,
)
from webhub.library.schemas import (
    CategoryReference,
    SiteSimilarityApplyResponse,
    SiteSimilarityDecisionResponse,
    SiteSimilarityGroupPageResponse,
    SiteSimilarityGroupResponse,
    SiteSimilarityMemberResponse,
    SiteSimilarityRecommendedDecisionResponse,
    SiteSimilarityScanResponse,
    TagReference,
)
from webhub.library.service._common import (
    LibraryConflictError,
    LibraryNotFoundError,
    LibraryValidationError,
    _safe_favicon_url,
)

SIMILARITY_RULESET_VERSION = "library-site-similarity.v1"
SimilarityKind = Literal["duplicate", "same_site"]
SimilarityKindFilter = Literal["duplicate", "same_site", "all"]

_TRACKING_QUERY_KEYS = frozenset(
    {
        "dclid",
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "msclkid",
    }
)


@dataclass(frozen=True, slots=True)
class _SiteSnapshot:
    site: Site
    category: Category
    tags: tuple[Tag, ...]


@dataclass(frozen=True, slots=True)
class SimilarityProjection:
    site_key: str
    kind: SimilarityKind
    display_host: str
    members: tuple[_SiteSnapshot, ...]
    recommended_site_id: str
    stable_key: str


def _normalized_query(url: str) -> str:
    values = []
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        normalized_key = key.strip().casefold().replace("-", "_").replace(".", "_")
        if normalized_key.startswith("utm_") or normalized_key in _TRACKING_QUERY_KEYS:
            continue
        values.append((key, value))
    return urlencode(sorted(values), doseq=True)


def page_fingerprint(url: str) -> str | None:
    """Identify page variants without network calls or fuzzy pairwise matching."""

    site_key = site_key_for_url(url)
    if site_key is None:
        return None
    parts = urlsplit(url)
    path = (parts.path or "/").rstrip("/") or "/"
    return "\n".join((site_key, path, _normalized_query(url)))


def library_fingerprint(sites: Iterable[Site]) -> str:
    """Hash every versioned library row so an old review can never delete new state."""

    digest = hashlib.sha256()
    for site in sorted(sites, key=lambda item: item.id):
        for value in (site.id, str(site.version), site.identity_url):
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


def _metadata_score(snapshot: _SiteSnapshot) -> int:
    site = snapshot.site
    return sum(
        (
            bool(site.favicon_url),
            bool(site.summary),
            bool(site.description),
            site.analysis_status == "complete",
            bool(snapshot.tags),
        )
    )


def _recommended(members: list[_SiteSnapshot], *, kind: SimilarityKind) -> _SiteSnapshot:
    def rank(snapshot: _SiteSnapshot) -> tuple[object, ...]:
        site = snapshot.site
        parts = urlsplit(site.identity_url)
        path = parts.path or "/"
        path_depth = len(tuple(part for part in path.split("/") if part))
        common = (
            parts.scheme.casefold() != "https",
            bool(parts.query),
            bool(parts.fragment),
            -_metadata_score(snapshot),
            site.created_at,
            site.id,
        )
        if kind == "same_site":
            return (path_depth, len(path), *common)
        return (*common, len(site.identity_url))

    return min(members, key=rank)


def detect_library_similarity(
    snapshots: Iterable[_SiteSnapshot],
) -> tuple[SimilarityProjection, ...]:
    """Return disjoint O(n) groups, with high-confidence page variants first."""

    authorities: defaultdict[str, list[_SiteSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        url = snapshot.site.identity_url
        if sensitive_url_keys(url):
            continue
        site_key = site_key_for_url(url)
        if site_key is None or is_shared_content_site_key(site_key):
            continue
        authorities[site_key].append(snapshot)

    projections: list[SimilarityProjection] = []
    for site_key, authority_members in authorities.items():
        if len(authority_members) < 2:
            continue
        by_page: defaultdict[str, list[_SiteSnapshot]] = defaultdict(list)
        for snapshot in authority_members:
            fingerprint = page_fingerprint(snapshot.site.identity_url)
            if fingerprint is not None:
                by_page[fingerprint].append(snapshot)

        singleton_members: list[_SiteSnapshot] = []
        for fingerprint, page_members in by_page.items():
            if len(page_members) < 2:
                singleton_members.extend(page_members)
                continue
            recommended = _recommended(page_members, kind="duplicate")
            members = tuple(
                sorted(
                    page_members,
                    key=lambda item: (
                        item.site.id != recommended.site.id,
                        item.site.created_at,
                        item.site.id,
                    ),
                )
            )
            projections.append(
                SimilarityProjection(
                    site_key=site_key,
                    kind="duplicate",
                    display_host=urlsplit(recommended.site.identity_url).netloc,
                    members=members,
                    recommended_site_id=recommended.site.id,
                    stable_key=fingerprint,
                )
            )

        if len(singleton_members) >= 2:
            recommended = _recommended(singleton_members, kind="same_site")
            members = tuple(
                sorted(
                    singleton_members,
                    key=lambda item: (
                        item.site.id != recommended.site.id,
                        len(urlsplit(item.site.identity_url).path or "/"),
                        item.site.created_at,
                        item.site.id,
                    ),
                )
            )
            projections.append(
                SimilarityProjection(
                    site_key=site_key,
                    kind="same_site",
                    display_host=urlsplit(recommended.site.identity_url).netloc,
                    members=members,
                    recommended_site_id=recommended.site.id,
                    stable_key=site_key,
                )
            )

    return tuple(
        sorted(
            projections,
            key=lambda item: (
                item.kind != "duplicate",
                -len(item.members),
                item.site_key,
                item.stable_key,
            ),
        )
    )


async def _snapshots(session: AsyncSession, *, user_id: str) -> tuple[_SiteSnapshot, ...]:
    sites = tuple(
        (
            await session.scalars(
                select(Site).where(Site.user_id == user_id).order_by(Site.id)
            )
        ).all()
    )
    if not sites:
        return ()
    categories = {
        category.id: category
        for category in (
            await session.scalars(
                select(Category).where(
                    Category.user_id == user_id,
                    Category.id.in_({site.category_id for site in sites}),
                )
            )
        ).all()
    }
    tags_by_site: defaultdict[str, list[Tag]] = defaultdict(list)
    tag_rows = (
        await session.execute(
            select(SiteTag.site_id, Tag)
            .join(
                Tag,
                and_(Tag.user_id == SiteTag.user_id, Tag.id == SiteTag.tag_id),
            )
            .where(SiteTag.user_id == user_id)
            .order_by(SiteTag.site_id, Tag.normalized_name, Tag.id)
        )
    ).all()
    for site_id, tag in tag_rows:
        tags_by_site[site_id].append(tag)
    return tuple(
        _SiteSnapshot(
            site=site,
            category=categories[site.category_id],
            tags=tuple(tags_by_site[site.id]),
        )
        for site in sites
    )


async def _selection_counts(
    session: AsyncSession,
    *,
    user_id: str,
    run_id: str,
) -> tuple[int, int]:
    kept_counts = (
        select(
            SiteSimilarityDecisionMember.user_id.label("user_id"),
            SiteSimilarityDecisionMember.run_id.label("run_id"),
            SiteSimilarityDecisionMember.group_id.label("group_id"),
            func.count(SiteSimilarityDecisionMember.site_id).label("kept_count"),
        )
        .where(
            SiteSimilarityDecisionMember.user_id == user_id,
            SiteSimilarityDecisionMember.run_id == run_id,
        )
        .group_by(
            SiteSimilarityDecisionMember.user_id,
            SiteSimilarityDecisionMember.run_id,
            SiteSimilarityDecisionMember.group_id,
        )
        .subquery()
    )
    row = (
        await session.execute(
            select(
                func.count(kept_counts.c.group_id),
                func.coalesce(
                    func.sum(SiteSimilarityGroup.member_count - kept_counts.c.kept_count),
                    0,
                ),
            )
            .select_from(kept_counts)
            .join(
                SiteSimilarityGroup,
                and_(
                    SiteSimilarityGroup.user_id == kept_counts.c.user_id,
                    SiteSimilarityGroup.run_id == kept_counts.c.run_id,
                    SiteSimilarityGroup.id == kept_counts.c.group_id,
                ),
            )
            .where(
                kept_counts.c.kept_count < SiteSimilarityGroup.member_count,
            )
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


async def _run_response(
    session: AsyncSession,
    run: SiteSimilarityScanRun,
) -> SiteSimilarityScanResponse:
    selected_groups, selected_deletes = await _selection_counts(
        session,
        user_id=run.user_id,
        run_id=run.id,
    )
    group_count = run.duplicate_group_count + run.same_site_group_count
    return SiteSimilarityScanResponse(
        id=run.id,
        status=run.status,  # type: ignore[arg-type]
        ruleset_version=run.ruleset_version,
        source_site_count=run.site_count,
        group_count=group_count,
        duplicate_group_count=run.duplicate_group_count,
        same_site_group_count=run.same_site_group_count,
        candidate_site_count=run.member_count,
        selected_group_count=selected_groups,
        selected_delete_count=selected_deletes,
        version=run.version,
        decision_version=run.version,
        created_at=run.created_at,
        applied_at=run.applied_at,
    )


async def start_scan(session: AsyncSession, *, user_id: str) -> SiteSimilarityScanResponse:
    if not await reserve_account_taxonomy(session, user_id):
        raise LibraryNotFoundError("账号不存在")
    now = utc_now()
    # Keep one replaced snapshot so an already-open tab receives an explicit
    # superseded conflict, without letting repeated scans grow forever.
    await session.execute(
        delete(SiteSimilarityScanRun).where(
            SiteSimilarityScanRun.user_id == user_id,
            SiteSimilarityScanRun.status == "superseded",
        )
    )
    await session.execute(
        update(SiteSimilarityScanRun)
        .where(
            SiteSimilarityScanRun.user_id == user_id,
            SiteSimilarityScanRun.status == "ready",
        )
        .values(status="superseded", updated_at=now)
    )
    snapshots = await _snapshots(session, user_id=user_id)
    projections = detect_library_similarity(snapshots)
    duplicate_count = sum(item.kind == "duplicate" for item in projections)
    same_site_count = len(projections) - duplicate_count
    run = SiteSimilarityScanRun(
        id=new_id(),
        user_id=user_id,
        status="ready",
        ruleset_version=SIMILARITY_RULESET_VERSION,
        library_fingerprint=library_fingerprint(item.site for item in snapshots),
        site_count=len(snapshots),
        duplicate_group_count=duplicate_count,
        same_site_group_count=same_site_count,
        member_count=sum(len(item.members) for item in projections),
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(run)
    # Explicit flush phases keep SQLite foreign-key ordering deterministic;
    # these models intentionally have no ORM relationships because the rows
    # are immutable snapshots, so the unit-of-work cannot infer every insert
    # dependency from object references alone.
    try:
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        raise LibraryConflictError(
            "网址库在扫描期间发生变化，请重新排查",
            code="site_similarity_scan_conflict",
        ) from error
    group_rows: list[SiteSimilarityGroup] = []
    member_rows: list[SiteSimilarityGroupMember] = []
    for ordinal, projection in enumerate(projections):
        group_id = new_id()
        group_rows.append(
            SiteSimilarityGroup(
                id=group_id,
                user_id=user_id,
                run_id=run.id,
                site_key=projection.site_key,
                kind=projection.kind,
                display_host=projection.display_host,
                member_count=len(projection.members),
                recommended_site_id=projection.recommended_site_id,
                ordinal=ordinal,
                created_at=now,
            )
        )
        for sort_order, snapshot in enumerate(projection.members):
            site = snapshot.site
            member_rows.append(
                SiteSimilarityGroupMember(
                    user_id=user_id,
                    run_id=run.id,
                    group_id=group_id,
                    site_id=site.id,
                    expected_version=site.version,
                    name=site.name,
                    original_url=site.original_url,
                    identity_url=site.identity_url,
                    summary=site.summary,
                    description=site.description,
                    favicon_url=_safe_favicon_url(site.favicon_url),
                    preview_url=site.preview_url,
                    category_id=snapshot.category.id,
                    category_name=snapshot.category.name,
                    category_is_default=snapshot.category.is_default,
                    category_icon=snapshot.category.icon,
                    tags_json=json.dumps(
                        [{"id": tag.id, "name": tag.name} for tag in snapshot.tags],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    pinned=site.pinned,
                    source=site.source,
                    analysis_status=site.analysis_status,
                    site_created_at=site.created_at,
                    site_updated_at=site.updated_at,
                    sort_order=sort_order,
                    is_recommended=site.id == projection.recommended_site_id,
                )
            )
    session.add_all(group_rows)
    try:
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        raise LibraryConflictError(
            "网址库在扫描期间发生变化，请重新排查",
            code="site_similarity_scan_conflict",
        ) from error
    session.add_all(member_rows)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise LibraryConflictError(
            "网址库在扫描期间发生变化，请重新排查",
            code="site_similarity_scan_conflict",
        ) from error
    return await _run_response(session, run)


async def active_scan(
    session: AsyncSession,
    *,
    user_id: str,
) -> SiteSimilarityScanResponse | None:
    run = await session.scalar(
        select(SiteSimilarityScanRun)
        .where(
            SiteSimilarityScanRun.user_id == user_id,
            SiteSimilarityScanRun.status == "ready",
        )
        .order_by(SiteSimilarityScanRun.created_at.desc(), SiteSimilarityScanRun.id.desc())
    )
    return None if run is None else await _run_response(session, run)


async def _owned_run(
    session: AsyncSession,
    *,
    user_id: str,
    run_id: str,
) -> SiteSimilarityScanRun:
    run = await session.scalar(
        select(SiteSimilarityScanRun).where(
            SiteSimilarityScanRun.user_id == user_id,
            SiteSimilarityScanRun.id == run_id,
        )
    )
    if run is None:
        raise LibraryNotFoundError("相似网站扫描不存在", code="site_similarity_scan_not_found")
    return run


def _encode_cursor(*, run_id: str, kind: str, ordinal: int) -> str:
    payload = json.dumps(
        {"run_id": run_id, "kind": kind, "ordinal": ordinal},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str, *, run_id: str, kind: str) -> int:
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        )
        if payload.get("run_id") != run_id or payload.get("kind") != kind:
            raise ValueError
        ordinal = int(payload["ordinal"])
        if ordinal < 0:
            raise ValueError
        return ordinal
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LibraryValidationError("分页游标无效", code="invalid_similarity_cursor") from error


def _member_response(member: SiteSimilarityGroupMember) -> SiteSimilarityMemberResponse:
    tags = json.loads(member.tags_json)
    return SiteSimilarityMemberResponse(
        id=member.site_id,
        name=member.name,
        original_url=member.original_url,
        identity_url=member.identity_url,
        summary=member.summary,
        description=member.description,
        favicon_url=_safe_favicon_url(member.favicon_url),
        preview_url=member.preview_url,
        category=CategoryReference(
            id=member.category_id,
            name=member.category_name,
            is_default=member.category_is_default,
            icon=member.category_icon,
        ),
        tags=[TagReference.model_validate(tag) for tag in tags],
        pinned=member.pinned,
        source=member.source,  # type: ignore[arg-type]
        analysis_status=member.analysis_status,  # type: ignore[arg-type]
        analysis_phase=None,
        version=member.expected_version,
        created_at=member.site_created_at,
        updated_at=member.site_updated_at,
        is_recommended=member.is_recommended,
    )


async def list_groups(
    session: AsyncSession,
    *,
    user_id: str,
    run_id: str,
    kind: SimilarityKindFilter,
    limit: int,
    cursor: str | None,
    page: int | None,
) -> SiteSimilarityGroupPageResponse:
    run = await _owned_run(session, user_id=user_id, run_id=run_id)
    if run.status == "superseded":
        raise LibraryConflictError(
            "该扫描已被新的排查结果替代",
            code="site_similarity_scan_superseded",
        )
    if cursor is not None and page is not None:
        raise LibraryValidationError(
            "分页游标和页码不能同时使用",
            code="invalid_similarity_pagination",
        )
    total_count = {
        "all": run.duplicate_group_count + run.same_site_group_count,
        "duplicate": run.duplicate_group_count,
        "same_site": run.same_site_group_count,
    }[kind]
    total_pages = (total_count + limit - 1) // limit
    group_scope = [
        SiteSimilarityGroup.user_id == user_id,
        SiteSimilarityGroup.run_id == run_id,
    ]
    if kind != "all":
        group_scope.append(SiteSimilarityGroup.kind == kind)

    after_ordinal = -1
    current_page = page or 1
    offset = 0
    if cursor is not None:
        after_ordinal = _decode_cursor(cursor, run_id=run_id, kind=kind)
        preceding_count = int(
            await session.scalar(
                select(func.count())
                .select_from(SiteSimilarityGroup)
                .where(*group_scope, SiteSimilarityGroup.ordinal <= after_ordinal)
            )
            or 0
        )
        current_page = preceding_count // limit + 1
    else:
        if current_page > max(total_pages, 1):
            raise LibraryValidationError(
                "相似网站页码超出范围",
                code="invalid_similarity_page",
            )
        offset = (current_page - 1) * limit
    statement = (
        select(SiteSimilarityGroup)
        .where(
            *group_scope,
        )
        .order_by(SiteSimilarityGroup.ordinal)
        .limit(limit + 1)
    )
    if cursor is not None:
        statement = statement.where(SiteSimilarityGroup.ordinal > after_ordinal)
    else:
        statement = statement.offset(offset)
    rows = list((await session.scalars(statement)).all())
    page_rows = rows[:limit]
    group_ids = [group.id for group in page_rows]
    members_by_group: defaultdict[str, list[SiteSimilarityGroupMember]] = defaultdict(list)
    selected_by_group: defaultdict[str, set[str]] = defaultdict(set)
    if group_ids:
        members, selected_rows = await session.scalars(
            select(SiteSimilarityGroupMember)
            .where(
                SiteSimilarityGroupMember.user_id == user_id,
                SiteSimilarityGroupMember.run_id == run_id,
                SiteSimilarityGroupMember.group_id.in_(group_ids),
            )
            .order_by(
                SiteSimilarityGroupMember.group_id,
                SiteSimilarityGroupMember.sort_order,
            )
        ), await session.execute(
            select(
                SiteSimilarityDecisionMember.group_id,
                SiteSimilarityDecisionMember.site_id,
            ).where(
                SiteSimilarityDecisionMember.user_id == user_id,
                SiteSimilarityDecisionMember.run_id == run_id,
                SiteSimilarityDecisionMember.group_id.in_(group_ids),
            )
        )
        members = members.all()
        for member in members:
            members_by_group[member.group_id].append(member)
        for group_id, site_id in selected_rows:
            selected_by_group[group_id].add(site_id)
    items = [
        SiteSimilarityGroupResponse(
            id=group.id,
            kind=group.kind,  # type: ignore[arg-type]
            site_key=group.site_key,
            display_host=group.display_host,
            member_count=group.member_count,
            recommended_site_id=group.recommended_site_id,
            keep_site_ids=[
                member.site_id
                for member in members_by_group[group.id]
                if member.site_id in selected_by_group[group.id]
            ],
            members=[_member_response(member) for member in members_by_group[group.id]],
        )
        for group in page_rows
    ]
    next_cursor = None
    if len(rows) > limit and page_rows:
        next_cursor = _encode_cursor(
            run_id=run_id,
            kind=kind,
            ordinal=page_rows[-1].ordinal,
        )
    return SiteSimilarityGroupPageResponse(
        items=items,
        next_cursor=next_cursor,
        page=current_page,
        page_size=limit,
        total_count=total_count,
        total_pages=total_pages,
        decision_version=run.version,
    )


async def save_decision(
    session: AsyncSession,
    *,
    user_id: str,
    run_id: str,
    group_id: str,
    keep_site_ids: list[str],
    expected_version: int,
) -> SiteSimilarityDecisionResponse:
    run = await _owned_run(session, user_id=user_id, run_id=run_id)
    if run.status != "ready":
        raise LibraryConflictError(
            "该扫描已结束，请重新排查",
            code="site_similarity_scan_not_active",
        )
    group = await session.scalar(
        select(SiteSimilarityGroup).where(
            SiteSimilarityGroup.user_id == user_id,
            SiteSimilarityGroup.run_id == run_id,
            SiteSimilarityGroup.id == group_id,
        )
    )
    if group is None:
        raise LibraryNotFoundError("相似网站分组不存在", code="site_similarity_group_not_found")
    member_rows = tuple(
        (
            await session.scalars(
                select(SiteSimilarityGroupMember)
                .where(
                    SiteSimilarityGroupMember.user_id == user_id,
                    SiteSimilarityGroupMember.run_id == run_id,
                    SiteSimilarityGroupMember.group_id == group_id,
                )
                .order_by(SiteSimilarityGroupMember.sort_order)
            )
        ).all()
    )
    member_ids = {member.site_id for member in member_rows}
    requested_ids = set(keep_site_ids)
    if len(requested_ids) != len(keep_site_ids):
        raise LibraryValidationError(
            "保留项不能包含重复网站",
            code="invalid_similarity_keep_sites",
        )
    if not requested_ids.issubset(member_ids):
        raise LibraryValidationError(
            "保留项必须全部来自当前分组",
            code="invalid_similarity_keep_sites",
        )
    # Selecting every member is exactly the safe default: keep the whole group.
    if requested_ids == member_ids:
        requested_ids.clear()
    ordered_keep_ids = tuple(
        member.site_id for member in member_rows if member.site_id in requested_ids
    )
    primary_keep_id = None
    if ordered_keep_ids:
        primary_keep_id = (
            group.recommended_site_id
            if group.recommended_site_id in requested_ids
            else ordered_keep_ids[0]
        )
    now = utc_now()
    try:
        advanced = await session.execute(
            update(SiteSimilarityScanRun)
            .where(
                SiteSimilarityScanRun.user_id == user_id,
                SiteSimilarityScanRun.id == run_id,
                SiteSimilarityScanRun.status == "ready",
                SiteSimilarityScanRun.version == expected_version,
            )
            .values(version=SiteSimilarityScanRun.version + 1, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if advanced.rowcount != 1:  # type: ignore[attr-defined]
            raise LibraryConflictError(
                "分组选择已在其他页面更新，请刷新后重试",
                code="site_similarity_version_conflict",
            )
        decision = await session.get(
            SiteSimilarityDecision,
            {"user_id": user_id, "run_id": run_id, "group_id": group_id},
        )
        if decision is None:
            session.add(
                SiteSimilarityDecision(
                    user_id=user_id,
                    run_id=run_id,
                    group_id=group_id,
                    keep_site_id=primary_keep_id,
                    updated_at=now,
                )
            )
        else:
            decision.keep_site_id = primary_keep_id
            decision.updated_at = now
        await session.flush()
        await session.execute(
            delete(SiteSimilarityDecisionMember).where(
                SiteSimilarityDecisionMember.user_id == user_id,
                SiteSimilarityDecisionMember.run_id == run_id,
                SiteSimilarityDecisionMember.group_id == group_id,
            )
        )
        session.add_all(
            SiteSimilarityDecisionMember(
                user_id=user_id,
                run_id=run_id,
                group_id=group_id,
                site_id=site_id,
            )
            for site_id in ordered_keep_ids
        )
        await session.flush()
        selected_groups, selected_deletes = await _selection_counts(
            session,
            user_id=user_id,
            run_id=run_id,
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise LibraryConflictError(
            "保存选择期间结果发生变化，请刷新后重试",
            code="site_similarity_version_conflict",
        ) from error
    except Exception:
        await session.rollback()
        raise
    return SiteSimilarityDecisionResponse(
        group_id=group_id,
        keep_site_ids=list(ordered_keep_ids),
        decision_version=expected_version + 1,
        selected_group_count=selected_groups,
        selected_delete_count=selected_deletes,
    )


async def select_recommended_decisions(
    session: AsyncSession,
    *,
    user_id: str,
    run_id: str,
    kind: SimilarityKindFilter,
    expected_version: int,
) -> SiteSimilarityRecommendedDecisionResponse:
    run = await _owned_run(session, user_id=user_id, run_id=run_id)
    if run.status != "ready":
        raise LibraryConflictError(
            "该扫描已结束，请重新排查",
            code="site_similarity_scan_not_active",
        )
    decision_join = and_(
        SiteSimilarityDecision.user_id == SiteSimilarityGroup.user_id,
        SiteSimilarityDecision.run_id == SiteSimilarityGroup.run_id,
        SiteSimilarityDecision.group_id == SiteSimilarityGroup.id,
    )
    group_statement = (
        select(SiteSimilarityGroup, SiteSimilarityDecision)
        .outerjoin(SiteSimilarityDecision, decision_join)
        .where(
            SiteSimilarityGroup.user_id == user_id,
            SiteSimilarityGroup.run_id == run_id,
        )
        .order_by(SiteSimilarityGroup.ordinal)
    )
    if kind != "all":
        group_statement = group_statement.where(SiteSimilarityGroup.kind == kind)
    group_rows = list((await session.execute(group_statement)).all())
    selected_statement = (
        select(
            SiteSimilarityDecisionMember.group_id,
            SiteSimilarityDecisionMember.site_id,
        )
        .join(
            SiteSimilarityGroup,
            and_(
                SiteSimilarityGroup.user_id == SiteSimilarityDecisionMember.user_id,
                SiteSimilarityGroup.run_id == SiteSimilarityDecisionMember.run_id,
                SiteSimilarityGroup.id == SiteSimilarityDecisionMember.group_id,
            ),
        )
        .where(
            SiteSimilarityGroup.user_id == user_id,
            SiteSimilarityGroup.run_id == run_id,
        )
    )
    if kind != "all":
        selected_statement = selected_statement.where(SiteSimilarityGroup.kind == kind)
    selected_by_group: defaultdict[str, set[str]] = defaultdict(set)
    for group_id, site_id in (await session.execute(selected_statement)).all():
        selected_by_group[group_id].add(site_id)
    changed_rows = [
        (group, decision)
        for group, decision in group_rows
        if selected_by_group[group.id] != {group.recommended_site_id}
    ]
    now = utc_now()
    try:
        advanced = await session.execute(
            update(SiteSimilarityScanRun)
            .where(
                SiteSimilarityScanRun.user_id == user_id,
                SiteSimilarityScanRun.id == run_id,
                SiteSimilarityScanRun.status == "ready",
                SiteSimilarityScanRun.version == expected_version,
            )
            .values(version=SiteSimilarityScanRun.version + 1, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if advanced.rowcount != 1:  # type: ignore[attr-defined]
            raise LibraryConflictError(
                "分组选择已在其他页面更新，请刷新后重试",
                code="site_similarity_version_conflict",
            )
        for group, decision in group_rows:
            if decision is None:
                session.add(
                    SiteSimilarityDecision(
                        user_id=user_id,
                        run_id=run_id,
                        group_id=group.id,
                        keep_site_id=group.recommended_site_id,
                        updated_at=now,
                    )
                )
            else:
                decision.keep_site_id = group.recommended_site_id
                decision.updated_at = now
        await session.flush()
        target_group_ids = select(SiteSimilarityGroup.id).where(
            SiteSimilarityGroup.user_id == user_id,
            SiteSimilarityGroup.run_id == run_id,
        )
        target_group_scope = [
            SiteSimilarityGroup.user_id == user_id,
            SiteSimilarityGroup.run_id == run_id,
        ]
        if kind != "all":
            target_group_ids = target_group_ids.where(SiteSimilarityGroup.kind == kind)
            target_group_scope.append(SiteSimilarityGroup.kind == kind)
        await session.execute(
            delete(SiteSimilarityDecisionMember).where(
                SiteSimilarityDecisionMember.user_id == user_id,
                SiteSimilarityDecisionMember.run_id == run_id,
                SiteSimilarityDecisionMember.group_id.in_(target_group_ids),
            )
        )
        await session.execute(
            insert(SiteSimilarityDecisionMember).from_select(
                ["user_id", "run_id", "group_id", "site_id"],
                select(
                    SiteSimilarityGroup.user_id,
                    SiteSimilarityGroup.run_id,
                    SiteSimilarityGroup.id,
                    SiteSimilarityGroup.recommended_site_id,
                ).where(*target_group_scope),
            )
        )
        await session.flush()
        selected_groups, selected_deletes = await _selection_counts(
            session,
            user_id=user_id,
            run_id=run_id,
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise LibraryConflictError(
            "批量选择期间结果发生变化，请刷新后重试",
            code="site_similarity_version_conflict",
        ) from error
    except Exception:
        await session.rollback()
        raise
    return SiteSimilarityRecommendedDecisionResponse(
        kind=kind,
        matched_group_count=len(group_rows),
        updated_group_count=len(changed_rows),
        decision_version=expected_version + 1,
        selected_group_count=selected_groups,
        selected_delete_count=selected_deletes,
    )


async def _merge_group_relationships(
    session: AsyncSession,
    *,
    user_id: str,
    keeper: Site,
    losers: tuple[Site, ...],
    affected_space_ids: set[str],
    now: datetime,
) -> None:
    group_site_ids = (keeper.id, *(site.id for site in losers))
    tag_rows = (
        await session.execute(
            select(SiteTag.site_id, SiteTag.tag_id).where(
                SiteTag.user_id == user_id,
                SiteTag.site_id.in_(group_site_ids),
            )
        )
    ).all()
    keeper_tags = {tag_id for site_id, tag_id in tag_rows if site_id == keeper.id}
    all_tags = {tag_id for _, tag_id in tag_rows}
    added_tag_ids = all_tags - keeper_tags
    session.add_all(
        SiteTag(user_id=user_id, site_id=keeper.id, tag_id=tag_id, created_at=now)
        for tag_id in sorted(added_tag_ids)
    )
    if added_tag_ids:
        preference = await session.get(
            SiteMetadataPreference,
            {"user_id": user_id, "site_id": keeper.id},
        )
        if preference is None:
            session.add(
                SiteMetadataPreference(
                    user_id=user_id,
                    site_id=keeper.id,
                    tags_are_manual=True,
                    tags_are_llm=False,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            # Confirming a merge is an explicit user decision. Protect the
            # resulting tag union from a later metadata backfill.
            preference.tags_are_manual = True
            preference.tags_are_llm = False
            preference.updated_at = now

    space_rows = (
        await session.execute(
            select(
                SpaceMember.space_id,
                SpaceMember.site_id,
                SpaceMember.position,
                SpaceMember.created_at,
            ).where(
                SpaceMember.user_id == user_id,
                SpaceMember.site_id.in_(group_site_ids),
            )
        )
    ).all()
    by_space: defaultdict[str, list[tuple[str, int, datetime]]] = defaultdict(list)
    for space_id, site_id, position, created_at in space_rows:
        by_space[space_id].append((site_id, position, created_at))
    loser_ids = {site.id for site in losers}
    for space_id, members in by_space.items():
        affected_space_ids.add(space_id)
        keeper_present = any(site_id == keeper.id for site_id, _, _ in members)
        loser_members = [item for item in members if item[0] in loser_ids]
        if not loser_members:
            continue
        await session.execute(
            delete(SpaceMember).where(
                SpaceMember.user_id == user_id,
                SpaceMember.space_id == space_id,
                SpaceMember.site_id.in_(loser_ids),
            )
        )
        if not keeper_present:
            _, position, created_at = min(loser_members, key=lambda item: (item[1], item[0]))
            session.add(
                SpaceMember(
                    user_id=user_id,
                    space_id=space_id,
                    site_id=keeper.id,
                    position=position,
                    created_at=created_at,
                )
            )

    next_version = keeper.version + 1
    await session.execute(
        update(SiteImportOrigin)
        .where(
            SiteImportOrigin.user_id == user_id,
            SiteImportOrigin.site_id.in_(loser_ids),
        )
        .values(site_id=keeper.id, site_version_at_link=next_version)
    )


async def apply_scan(
    session: AsyncSession,
    *,
    user_id: str,
    run_id: str,
    expected_version: int,
) -> SiteSimilarityApplyResponse:
    if not await reserve_account_taxonomy(session, user_id):
        raise LibraryNotFoundError("账号不存在")
    run = await _owned_run(session, user_id=user_id, run_id=run_id)
    if run.status == "applied" and run.result_json is not None:
        return SiteSimilarityApplyResponse.model_validate_json(run.result_json)
    if run.status != "ready":
        raise LibraryConflictError(
            "该扫描已被替代，请重新排查",
            code="site_similarity_scan_not_active",
        )
    now = utc_now()
    reserved = await session.execute(
        update(SiteSimilarityScanRun)
        .where(
            SiteSimilarityScanRun.user_id == user_id,
            SiteSimilarityScanRun.id == run_id,
            SiteSimilarityScanRun.status == "ready",
            SiteSimilarityScanRun.version == expected_version,
        )
        .values(version=SiteSimilarityScanRun.version + 1, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if reserved.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise LibraryConflictError(
            "分组选择已发生变化，请刷新后重试",
            code="site_similarity_version_conflict",
        )

    current_sites = tuple(
        (
            await session.scalars(
                select(Site).where(Site.user_id == user_id).order_by(Site.id)
            )
        ).all()
    )
    if (
        len(current_sites) != run.site_count
        or library_fingerprint(current_sites) != run.library_fingerprint
    ):
        await session.rollback()
        raise LibraryConflictError(
            "网址库已发生变化，本次结果未执行，请重新排查",
            code="site_similarity_scan_stale",
        )

    selected_rows = (
        await session.execute(
            select(
                SiteSimilarityDecision.group_id,
                SiteSimilarityDecision.keep_site_id,
                SiteSimilarityDecisionMember.site_id,
            )
            .join(
                SiteSimilarityDecisionMember,
                and_(
                    SiteSimilarityDecisionMember.user_id
                    == SiteSimilarityDecision.user_id,
                    SiteSimilarityDecisionMember.run_id
                    == SiteSimilarityDecision.run_id,
                    SiteSimilarityDecisionMember.group_id
                    == SiteSimilarityDecision.group_id,
                ),
            )
            .where(
                SiteSimilarityDecision.user_id == user_id,
                SiteSimilarityDecision.run_id == run_id,
                SiteSimilarityDecision.keep_site_id.is_not(None),
            )
        )
    ).all()
    choices: defaultdict[str, set[str]] = defaultdict(set)
    primary_by_group: dict[str, str] = {}
    for group_id, primary_site_id, selected_site_id in selected_rows:
        if primary_site_id is None:
            continue
        choices[group_id].add(selected_site_id)
        primary_by_group[group_id] = primary_site_id
    members_by_group: defaultdict[str, list[SiteSimilarityGroupMember]] = defaultdict(list)
    if choices:
        members = (
            await session.scalars(
                select(SiteSimilarityGroupMember)
                .join(
                    SiteSimilarityDecision,
                    and_(
                        SiteSimilarityDecision.user_id
                        == SiteSimilarityGroupMember.user_id,
                        SiteSimilarityDecision.run_id
                        == SiteSimilarityGroupMember.run_id,
                        SiteSimilarityDecision.group_id
                        == SiteSimilarityGroupMember.group_id,
                    ),
                )
                .where(
                    SiteSimilarityGroupMember.user_id == user_id,
                    SiteSimilarityGroupMember.run_id == run_id,
                    SiteSimilarityDecision.keep_site_id.is_not(None),
                )
                .order_by(
                    SiteSimilarityGroupMember.group_id,
                    SiteSimilarityGroupMember.sort_order,
                )
            )
        ).all()
        for member in members:
            members_by_group[member.group_id].append(member)

    sites_by_id = {site.id: site for site in current_sites}
    deleted_ids: list[str] = []
    kept_ids: list[str] = []
    affected_space_ids: set[str] = set()
    for group_id, selected_site_ids in choices.items():
        member_rows = members_by_group[group_id]
        member_ids = {member.site_id for member in member_rows}
        primary_site_id = primary_by_group[group_id]
        if (
            primary_site_id not in selected_site_ids
            or not selected_site_ids.issubset(member_ids)
            or not selected_site_ids
            or len(selected_site_ids) >= len(member_rows)
            or len(member_rows) < 2
        ):
            await session.rollback()
            raise LibraryConflictError(
                "扫描分组快照不完整，请重新排查",
                code="site_similarity_scan_stale",
            )
        keeper = sites_by_id[primary_site_id]
        selected_survivors = [
            sites_by_id[member.site_id]
            for member in member_rows
            if member.site_id in selected_site_ids
        ]
        losers = tuple(
            sites_by_id[member.site_id]
            for member in member_rows
            if member.site_id not in selected_site_ids
        )
        if ({site.id for site in selected_survivors} | {site.id for site in losers}) != member_ids:
            await session.rollback()
            raise LibraryConflictError(
                "扫描分组快照不完整，请重新排查",
                code="site_similarity_scan_stale",
            )
        if member_ids.intersection(deleted_ids) or member_ids.intersection(kept_ids):
            await session.rollback()
            raise LibraryConflictError(
                "扫描分组存在重叠，请重新排查",
                code="site_similarity_scan_stale",
            )
        await _merge_group_relationships(
            session,
            user_id=user_id,
            keeper=keeper,
            losers=losers,
            affected_space_ids=affected_space_ids,
            now=now,
        )
        keeper_pinned = keeper.pinned or any(site.pinned for site in losers)
        updated = await session.execute(
            update(Site)
            .where(
                Site.user_id == user_id,
                Site.id == keeper.id,
                Site.version == keeper.version,
            )
            .values(
                pinned=keeper_pinned,
                version=Site.version + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if updated.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            raise LibraryConflictError(
                "网址库已发生变化，本次结果未执行，请重新排查",
                code="site_similarity_scan_stale",
            )
        kept_ids.extend(site.id for site in selected_survivors)
        deleted_ids.extend(site.id for site in losers)

    await session.flush()
    for offset in range(0, len(deleted_ids), 200):
        chunk = deleted_ids[offset : offset + 200]
        matches = or_(
            *(
                and_(Site.id == site_id, Site.version == sites_by_id[site_id].version)
                for site_id in chunk
            )
        )
        deleted = await session.execute(
            delete(Site)
            .where(Site.user_id == user_id, matches)
            .execution_options(synchronize_session=False)
        )
        if deleted.rowcount != len(chunk):  # type: ignore[attr-defined]
            await session.rollback()
            raise LibraryConflictError(
                "网址库已发生变化，本次结果未执行，请重新排查",
                code="site_similarity_scan_stale",
            )
    if affected_space_ids:
        await session.execute(
            update(Space)
            .where(Space.user_id == user_id, Space.id.in_(affected_space_ids))
            .values(version=Space.version + 1, updated_at=now)
        )

    response = SiteSimilarityApplyResponse(
        id=run.id,
        status="applied",
        decision_version=run.version + 1,
        merged_group_count=len(choices),
        deleted_site_count=len(deleted_ids),
        kept_site_count=len(kept_ids),
        deleted_site_ids=deleted_ids,
        kept_site_ids=kept_ids,
        applied_at=now,
    )
    run.status = "applied"
    run.version = expected_version + 1
    run.updated_at = now
    run.applied_at = now
    run.result_json = response.model_dump_json()
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise LibraryConflictError(
            "合并期间网址库发生变化，未写入任何结果",
            code="site_similarity_apply_conflict",
        ) from error
    return response


__all__ = [
    "SIMILARITY_RULESET_VERSION",
    "SimilarityProjection",
    "active_scan",
    "apply_scan",
    "detect_library_similarity",
    "library_fingerprint",
    "list_groups",
    "page_fingerprint",
    "save_decision",
    "select_recommended_decisions",
    "start_scan",
]
