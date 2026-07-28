from __future__ import annotations

from typing import Literal

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.locking import reserve_account_taxonomy
from webhub.db.models import (
    Category,
    Site,
    SiteMetadataPreference,
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
        summary=site.summary,
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


async def _metadata_preference(
    session: AsyncSession,
    *,
    user_id: str,
    site_id: str,
) -> SiteMetadataPreference | None:
    return await session.get(
        SiteMetadataPreference,
        {"user_id": user_id, "site_id": site_id},
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
    if not await reserve_account_taxonomy(session, user_id):
        raise LibraryConflictError("账号状态已发生变化，请刷新后重试")
    category = (
        await _owned_category(session, user_id, payload.category_id)
        if payload.category_id
        else await _default_category(session, user_id)
    )
    tags = await _owned_tags(session, user_id, payload.tag_ids)
    # Creation has no prior derived value to clear. Empty form defaults are
    # therefore absence, not a permanent user veto on later enrichment.
    manual_summary = bool(payload.summary.strip())
    manual_description = bool(payload.description.strip())
    manual_favicon = payload.favicon_url is not None
    manual_category = "category_id" in payload.model_fields_set and not category.is_default
    # The create form always sends an empty tag array. Only actual tag choices
    # are user intent at creation time; a later PATCH with [] is an explicit
    # clear and is handled separately below.
    manual_tags = bool(tags)
    # Reserve the category row before reading its tail. On SQLite this also
    # acquires the writer reservation; on multi-writer databases it prevents
    # two creates in the same category from choosing one position.
    reserved_category = await session.execute(
        update(Category)
        .where(Category.user_id == user_id, Category.id == category.id)
        .values(updated_at=Category.updated_at)
    )
    if reserved_category.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise LibraryConflictError(
            "目标分类已发生变化，请刷新后重试",
            code="category_conflict",
        )
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
        summary=payload.summary.strip(),
        description=payload.description.strip(),
        favicon_url=payload.favicon_url,
        pinned=payload.pinned,
        source=payload.source,
    )
    session.add(site)
    try:
        await session.flush()
        if (
            manual_summary
            or manual_description
            or manual_favicon
            or manual_category
            or manual_tags
        ):
            session.add(
                SiteMetadataPreference(
                    user_id=user_id,
                    site_id=site.id,
                    summary_is_manual=manual_summary,
                    description_is_manual=manual_description,
                    favicon_is_manual=manual_favicon,
                    category_is_manual=manual_category,
                    tags_are_manual=manual_tags,
                )
            )
        session.add_all(SiteTag(user_id=user_id, site_id=site.id, tag_id=tag.id) for tag in tags)
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise LibraryConflictError(
            "该网址已存在于当前账号的网址库",
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
    if {"url", "category_id", "tag_ids"} & fields and not await reserve_account_taxonomy(
        session, user_id
    ):
        raise LibraryConflictError("账号状态已发生变化，请刷新后重试")

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
    summary_update = (payload.summary or "").strip() if "summary" in fields else None
    explicit_new_summary = summary_update is not None and summary_update != (site.summary or "")
    description_update = (payload.description or "").strip() if "description" in fields else None
    explicit_new_description = (
        description_update is not None and description_update != (site.description or "")
    )
    # An explicit empty field is a decision even when it was already empty.
    # The current UI sends only dirty fields, so this preserves a clear without
    # turning an ordinary rename into a metadata decision.
    manual_summary = "summary" in fields and (
        explicit_new_summary
        or (not url_changed and summary_update == "")
    )
    manual_description = "description" in fields and (
        explicit_new_description
        or (not url_changed and description_update == "")
    )
    manual_favicon = "favicon_url" in fields and (
        explicit_new_favicon
        or (not url_changed and payload.favicon_url is None)
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
    # PATCH field presence is the user's decision for the new target, even when
    # the chosen value happens to equal the old target's category.
    manual_category = category is not None

    tags: list[Tag] | None = None
    if "tag_ids" in fields:
        if payload.tag_ids is None:
            raise LibraryValidationError("标签列表不能为空")
        tags = await _owned_tags(session, user_id, payload.tag_ids)
    # An explicit empty list is also a manual choice and must remain protected
    # from a later LLM pass.
    manual_tags = tags is not None
    if url_changed:
        # A changed URL is a new website identity. Omitted taxonomy fields must
        # not pin the old site's semantics onto the new target; fields that are
        # present above remain explicit manual decisions.
        if not manual_category:
            category = await _default_category(session, user_id)
        if not manual_tags:
            tags = []

    try:
        if category is not None and category.id != site.category_id:
            reserved_category = await session.execute(
                update(Category)
                .where(Category.user_id == user_id, Category.id == category.id)
                .values(updated_at=Category.updated_at)
            )
            if reserved_category.rowcount != 1:  # type: ignore[attr-defined]
                await session.rollback()
                raise LibraryConflictError(
                    "目标分类已发生变化，请刷新后重试",
                    code="category_conflict",
                )
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
        preference: SiteMetadataPreference | None = None
        if (
            url_changed
            or manual_summary
            or manual_description
            or manual_favicon
            or manual_category
            or manual_tags
        ):
            preference = await _metadata_preference(session, user_id=user_id, site_id=site_id)
        if url_changed and preference is not None:
            # A metadata decision belongs to the previous target. A true URL
            # change makes the new page eligible for derived values again.
            preference.summary_is_manual = False
            preference.description_is_manual = False
            preference.favicon_is_manual = False
            preference.category_is_manual = False
            preference.tags_are_manual = False
            preference.summary_is_llm = False
            preference.description_is_llm = False
            preference.category_is_llm = False
            preference.tags_are_llm = False
            preference.preview_checked_at = None
            preference.llm_analyzed_at = None
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
            site.summary = ""
            site.description = ""
            site.analysis_status = "not_analyzed"
            site.analysis_updated_at = None
        if "summary" in fields and (not url_changed or manual_summary):
            site.summary = summary_update or ""
            if manual_summary:
                if preference is None:
                    preference = SiteMetadataPreference(user_id=user_id, site_id=site_id)
                    session.add(preference)
                preference.summary_is_manual = True
                preference.summary_is_llm = False
        if "description" in fields and (not url_changed or manual_description):
            site.description = description_update or ""
            if manual_description:
                if preference is None:
                    preference = SiteMetadataPreference(user_id=user_id, site_id=site_id)
                    session.add(preference)
                preference.description_is_manual = True
                preference.description_is_llm = False
        if "favicon_url" in fields and (not url_changed or manual_favicon):
            site.favicon_url = payload.favicon_url
            if manual_favicon:
                if preference is None:
                    preference = SiteMetadataPreference(user_id=user_id, site_id=site_id)
                    session.add(preference)
                preference.favicon_is_manual = True
        if "pinned" in fields:
            site.pinned = bool(payload.pinned)
        if category is not None:
            if category.id != site.category_id:
                site.position = int(
                    await session.scalar(
                        select(func.coalesce(func.max(Site.position), -1) + 1).where(
                            Site.user_id == user_id,
                            Site.category_id == category.id,
                        )
                    )
                    or 0
                )
                site.category_id = category.id
            if manual_category:
                if preference is None:
                    preference = SiteMetadataPreference(user_id=user_id, site_id=site_id)
                    session.add(preference)
                preference.category_is_manual = True
                preference.category_is_llm = False
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
            if manual_tags:
                if preference is None:
                    preference = SiteMetadataPreference(user_id=user_id, site_id=site_id)
                    session.add(preference)
                preference.tags_are_manual = True
                preference.tags_are_llm = False
        if (
            preference is not None
            and not preference.summary_is_manual
            and not preference.description_is_manual
            and not preference.favicon_is_manual
            and not preference.category_is_manual
            and not preference.tags_are_manual
            and not preference.summary_is_llm
            and not preference.description_is_llm
            and not preference.category_is_llm
            and not preference.tags_are_llm
            and preference.preview_checked_at is None
            and preference.llm_analyzed_at is None
        ):
            await session.delete(preference)

        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise LibraryConflictError(
            "该网址已存在于当前账号的网址库",
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
