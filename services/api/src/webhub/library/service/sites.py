from __future__ import annotations

from typing import Literal

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import (
    Category,
    Site,
    SiteTag,
    Space,
    SpaceMember,
    Tag,
    utc_now,
)
from webhub.library.schemas import (
    CategoryReference,
    SiteBulkDeleteRequest,
    SiteBulkDeleteResponse,
    SiteCreateRequest,
    SiteDeleteResponse,
    SiteResponse,
    SiteUpdateRequest,
    TagReference,
)

# ``_owned_site`` 用限定访问：测试要 patch 它来制造并发窗口，
# ``from ._common import`` 在导入时就绑死，patch 一处覆盖不到这里。
from . import _common
from ._common import (
    LibraryConflictError,
    LibraryValidationError,
    _default_category,
    _display_name,
    _owned_category,
    _owned_tags,
    _safe_favicon_url,
    _site_url,
)

SortDirection = Literal["asc", "desc"]


async def _site_response(
    session: AsyncSession,
    user_id: str,
    site: Site,
    category: Category | None = None,
    tags: list[Tag] | None = None,
) -> SiteResponse:
    selected_category = category or await _owned_category(session, user_id, site.category_id)
    selected_tags = tags
    if selected_tags is None:
        selected_tags = list(
            (
                await session.scalars(
                    select(Tag)
                    .join(
                        SiteTag,
                        and_(SiteTag.user_id == Tag.user_id, SiteTag.tag_id == Tag.id),
                    )
                    .where(
                        SiteTag.user_id == user_id,
                        SiteTag.site_id == site.id,
                    )
                    .order_by(Tag.normalized_name, Tag.id)
                )
            ).all()
        )
    return SiteResponse(
        id=site.id,
        name=site.name,
        original_url=site.original_url,
        identity_url=site.identity_url,
        description=site.description,
        favicon_url=_safe_favicon_url(site.favicon_url),
        preview_url=site.preview_url,
        category=CategoryReference(
            id=selected_category.id,
            name=selected_category.name,
            is_default=selected_category.is_default,
            icon=selected_category.icon,
        ),
        tags=[TagReference(id=tag.id, name=tag.name) for tag in selected_tags],
        pinned=site.pinned,
        source=site.source,  # type: ignore[arg-type]
        analysis_status=site.analysis_status,  # type: ignore[arg-type]
        version=site.version,
        created_at=site.created_at,
        updated_at=site.updated_at,
    )


async def get_site(session: AsyncSession, user_id: str, site_id: str) -> SiteResponse:
    site = await _common._owned_site(session, user_id, site_id)
    return await _site_response(session, user_id, site)


async def create_site(
    session: AsyncSession,
    user_id: str,
    payload: SiteCreateRequest,
) -> SiteResponse:
    name, normalized_name = _display_name(payload.name, maximum=160, field="网站名称")
    original_url, identity_url = _site_url(payload.url)
    category = (
        await _owned_category(session, user_id, payload.category_id)
        if payload.category_id
        else await _default_category(session, user_id)
    )
    tags = await _owned_tags(session, user_id, payload.tag_ids)
    # 新站排在该分类末尾：唯一索引要求位置不重复，所以取当前最大值 +1。
    next_position = int(
        await session.scalar(
            select(func.coalesce(func.max(Site.position), -1) + 1).where(
                Site.user_id == user_id,
                Site.category_id == category.id,
            )
        )
        or 0
    )
    site = Site(
        user_id=user_id,
        category_id=category.id,
        position=next_position,
        name=name,
        normalized_name=normalized_name,
        original_url=original_url,
        identity_url=identity_url,
        description=payload.description.strip(),
        favicon_url=payload.favicon_url,
        pinned=payload.pinned,
    )
    session.add(site)
    try:
        await session.flush()
        session.add_all(SiteTag(user_id=user_id, site_id=site.id, tag_id=tag.id) for tag in tags)
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise LibraryConflictError(
            "该网址已存在于当前账号的资料库",
            code="duplicate_url",
        ) from error
    return await _site_response(session, user_id, site, category, tags)


async def update_site(
    session: AsyncSession,
    user_id: str,
    site_id: str,
    payload: SiteUpdateRequest,
) -> SiteResponse:
    site = await _common._owned_site(session, user_id, site_id)
    if site.version != payload.expected_version:
        raise LibraryConflictError(
            "网站已被修改，请刷新后重试",
            code="version_conflict",
        )

    fields = payload.model_fields_set - {"expected_version"}
    if not fields:
        raise LibraryValidationError("网站更新至少需要一个字段")

    name_update: tuple[str, str] | None = None
    if "name" in fields:
        if payload.name is None:
            raise LibraryValidationError("网站名称不能为空")
        name_update = _display_name(payload.name, maximum=160, field="网站名称")

    url_update: tuple[str, str] | None = None
    if "url" in fields:
        if payload.url is None:
            raise LibraryValidationError("网址不能为空")
        url_update = _site_url(payload.url)
    url_changed = url_update is not None and url_update[1] != site.identity_url
    # The web form now sends only dirty fields. Keep this comparison as a
    # compatibility guard for older clients that submitted the old favicon on
    # every PATCH: repeating the stored value is not a new icon decision.
    explicit_new_favicon = (
        "favicon_url" in fields and payload.favicon_url != site.favicon_url
    )

    if "pinned" in fields and payload.pinned is None:
        raise LibraryValidationError("置顶状态不能为空")

    category: Category | None = None
    if "category_id" in fields:
        category = (
            await _owned_category(session, user_id, payload.category_id)
            if payload.category_id
            else await _default_category(session, user_id)
        )

    tags: list[Tag] | None = None
    if "tag_ids" in fields:
        if payload.tag_ids is None:
            raise LibraryValidationError("标签列表不能为空")
        tags = await _owned_tags(session, user_id, payload.tag_ids)

    try:
        claimed_at = utc_now()
        claim = await session.execute(
            update(Site)
            .where(
                Site.user_id == user_id,
                Site.id == site_id,
                Site.version == payload.expected_version,
            )
            .values(version=Site.version + 1, updated_at=claimed_at)
            .execution_options(synchronize_session=False)
        )
        if claim.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            raise LibraryConflictError(
                "网站已被修改，请刷新后重试",
                code="version_conflict",
            )

        await session.refresh(site)
        if name_update is not None:
            site.name, site.normalized_name = name_update
        if url_update is not None:
            site.original_url, site.identity_url = url_update
        if url_changed:
            # Scraped media belongs to the old target. Keeping it would show
            # one website's identity on another and, because analysis only
            # fills blanks, even an explicit re-analysis could never repair it.
            site.favicon_url = None
            site.preview_url = None
            site.analysis_status = "not_analyzed"
        if "description" in fields:
            site.description = (payload.description or "").strip()
        if "favicon_url" in fields and (not url_changed or explicit_new_favicon):
            site.favicon_url = payload.favicon_url
        if "pinned" in fields:
            site.pinned = bool(payload.pinned)
        if category is not None:
            site.category_id = category.id
        if tags is not None:
            await session.execute(
                delete(SiteTag).where(
                    SiteTag.user_id == user_id,
                    SiteTag.site_id == site.id,
                )
            )
            session.add_all(
                SiteTag(user_id=user_id, site_id=site.id, tag_id=tag.id) for tag in tags
            )

        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise LibraryConflictError(
            "该网址已存在于当前账号的资料库",
            code="duplicate_url",
        ) from error
    return await _site_response(session, user_id, site)


async def delete_site(
    session: AsyncSession,
    user_id: str,
    site_id: str,
    *,
    expected_version: int,
) -> SiteDeleteResponse:
    site = await _common._owned_site(session, user_id, site_id)
    if site.version != expected_version:
        raise LibraryConflictError(
            "网站已被修改，请刷新后重试",
            code="version_conflict",
        )
    now = utc_now()
    related_space_ids = list(
        (
            await session.scalars(
                select(SpaceMember.space_id).where(
                    SpaceMember.user_id == user_id,
                    SpaceMember.site_id == site_id,
                )
            )
        ).all()
    )
    deleted = await session.execute(
        delete(Site)
        .where(
            Site.user_id == user_id,
            Site.id == site_id,
            Site.version == expected_version,
        )
        .execution_options(synchronize_session=False)
    )
    if deleted.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise LibraryConflictError(
            "网站已被修改，请刷新后重试",
            code="version_conflict",
        )
    if related_space_ids:
        await session.execute(
            update(Space)
            .where(Space.user_id == user_id, Space.id.in_(related_space_ids))
            .values(version=Space.version + 1, updated_at=now)
        )
    await session.commit()
    return SiteDeleteResponse(message="网站已删除", site_id=site_id)


async def bulk_delete_sites(
    session: AsyncSession,
    user_id: str,
    payload: SiteBulkDeleteRequest,
) -> SiteBulkDeleteResponse:
    """Atomically delete an account-scoped, versioned set of sites."""

    expected_versions = {item.site_id: item.expected_version for item in payload.items}
    site_ids = list(expected_versions)
    sites = list(
        (
            await session.scalars(
                select(Site).where(
                    Site.user_id == user_id,
                    Site.id.in_(site_ids),
                )
            )
        ).all()
    )
    if len(sites) != len(site_ids) or any(
        site.version != expected_versions[site.id] for site in sites
    ):
        await session.rollback()
        raise LibraryConflictError(
            "所选网站已发生变化，请刷新后重新选择",
            code="bulk_delete_conflict",
        )

    related_space_ids = list(
        (
            await session.scalars(
                select(SpaceMember.space_id)
                .where(
                    SpaceMember.user_id == user_id,
                    SpaceMember.site_id.in_(site_ids),
                )
                .distinct()
            )
        ).all()
    )
    version_matches = or_(
        *(
            and_(Site.id == site_id, Site.version == expected_version)
            for site_id, expected_version in expected_versions.items()
        )
    )
    deleted = await session.execute(
        delete(Site)
        .where(
            Site.user_id == user_id,
            version_matches,
        )
        .execution_options(synchronize_session=False)
    )
    if deleted.rowcount != len(site_ids):  # type: ignore[attr-defined]
        await session.rollback()
        raise LibraryConflictError(
            "所选网站已发生变化，请刷新后重新选择",
            code="bulk_delete_conflict",
        )

    if related_space_ids:
        await session.execute(
            update(Space)
            .where(
                Space.user_id == user_id,
                Space.id.in_(related_space_ids),
            )
            .values(version=Space.version + 1, updated_at=utc_now())
        )
    await session.commit()
    return SiteBulkDeleteResponse(
        message=f"已删除 {len(site_ids)} 个网站",
        deleted_site_ids=site_ids,
    )
