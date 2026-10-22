"""MCP tool implementations for external Agents.

Each function takes an open ``AsyncSession`` and the token's ``AgentIdentity``
and returns a JSON-ready ``dict``. Account scope comes only from the identity —
never from tool arguments. Writes reuse the library service functions (same
validation, URL normalization, kind inference and usage-signal rules as the
website) and are limited to reversible, incremental changes (2026-10 D3):
there is no delete and no bulk recategorization here.

Results stay compact: list results carry the short summary, never the long
description. Text is returned in its original language.
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.agent_access.tokens import AgentIdentity, record_write
from webhub.db.database import Database
from webhub.db.locking import reserve_account_taxonomy
from webhub.db.models import Category, Site, SiteTag, Tag
from webhub.feeds import service as feed_service
from webhub.ingestion.enrichment import normalize_site_tag_name
from webhub.library import service
from webhub.library.analysis_queue import schedule_site_enrichment
from webhub.library.kinds import SiteKind, infer_kind, repo_meta
from webhub.library.schemas import (
    MAX_REDISCOVER_LIMIT,
    MAX_SNOOZE_DAYS,
    SiteCreateRequest,
    SiteResponse,
    SiteStateUpdateRequest,
    SiteUpdateRequest,
)
from webhub.library.service import SiteStateFilter

MAX_SEARCH_LIMIT = 30
MAX_QUERY_LENGTH = 300
MAX_TAGS_PER_CALL = 8
# Same bound as ``SiteUpdateRequest.tag_ids``.
MAX_TAGS_PER_RESOURCE = 50
MAX_TAXONOMY_TAGS = 200
MAX_TITLE_LENGTH = 160
MAX_NOTE_LENGTH = 4_000
UPDATE_ATTEMPTS = 3

MarkAction = Literal["opened", "seen", "snooze", "mute"]
KIND_LABELS: dict[str, str] = {
    "repo": "仓库",
    "article": "文章",
    "video": "视频",
    "website": "网站",
}


class ToolInputError(Exception):
    """A caller mistake the Agent can fix; the message is shown to it verbatim."""


# ---------------------------------------------------------------- formatting


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat(timespec="seconds").replace("+00:00", "Z")


def compact_resource(site: SiteResponse) -> dict[str, Any]:
    return {
        "id": site.id,
        "title": site.name,
        "url": site.original_url,
        "kind": site.kind,
        "summary": site.summary,
        "category": site.category.name,
        "tags": [tag.name for tag in site.tags],
        "saved_at": _iso(site.created_at),
        "opened": site.open_count > 0,
        "starred": site.starred,
    }


def resource_detail(site: SiteResponse) -> dict[str, Any]:
    detail = compact_resource(site)
    detail.update(
        {
            "description": site.description,
            "source": site.source,
            "meta": site.meta,
            "open_count": site.open_count,
            "last_opened_at": _iso(site.last_opened_at),
            "seen": site.seen_at is not None,
            "resurface_muted": site.resurface_muted,
            "analysis_status": site.analysis_status,
            "updated_at": _iso(site.updated_at),
        }
    )
    return detail


# ------------------------------------------------------------- name helpers


def _tag_key(name: str) -> str:
    try:
        return service._display_name(name, maximum=40, field="标签名称")[1]
    except service.LibraryValidationError as error:
        raise ToolInputError(f"Invalid tag name {name!r}: {error.message}") from None


def _new_tag_names(names: list[str] | None) -> list[tuple[str, str]]:
    """Display and uniqueness forms, deduplicated, in the caller's order.

    Uses the same NFKC + whitespace + casefold key as the library (so "agent"
    reuses an existing "Agent"), plus the model-label safety rules applied to
    LLM-proposed tags, because these names come from an Agent too.
    """

    selected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in names or []:
        try:
            display, key = normalize_site_tag_name(raw)
        except ValueError as error:
            raise ToolInputError(f"Invalid tag name {raw!r}: {error}") from None
        if key not in seen:
            seen.add(key)
            selected.append((display, key))
    if len(selected) > MAX_TAGS_PER_CALL:
        raise ToolInputError(f"At most {MAX_TAGS_PER_CALL} tags per call.")
    return selected


async def _ensure_tags(
    session: AsyncSession,
    user_id: str,
    names: list[tuple[str, str]],
) -> tuple[list[Tag], list[str]]:
    """Reuse tags by normalized name and stage the missing ones (flushed, not committed).

    The caller must hold the account taxonomy reservation; the following
    library write commits the new tags together with the site change, and a
    failed write rolls them back.
    """

    if not names:
        return [], []
    keys = [key for _, key in names]
    existing = {
        tag.normalized_name: tag
        for tag in (
            await session.scalars(
                select(Tag).where(Tag.user_id == user_id, Tag.normalized_name.in_(keys))
            )
        ).all()
    }
    created: list[str] = []
    selected: list[Tag] = []
    for display, key in names:
        tag = existing.get(key)
        if tag is None:
            tag = Tag(user_id=user_id, name=display, normalized_name=key)
            session.add(tag)
            existing[key] = tag
            created.append(display)
        selected.append(tag)
    if created:
        await session.flush()
    return selected, created


async def _site_tags(session: AsyncSession, user_id: str, site_id: str) -> list[Tag]:
    return list(
        (
            await session.scalars(
                select(Tag)
                .join(SiteTag, and_(SiteTag.user_id == Tag.user_id, SiteTag.tag_id == Tag.id))
                .where(SiteTag.user_id == user_id, SiteTag.site_id == site_id)
                .order_by(Tag.normalized_name, Tag.id)
            )
        ).all()
    )


def _resource_name(title: str | None, identity_url: str) -> str:
    if title:
        cleaned = " ".join(unicodedata.normalize("NFKC", title).split())
        if cleaned:
            return cleaned[:MAX_TITLE_LENGTH]
    # No title: a repository reads best as owner/repo, anything else as its
    # host. Background analysis fills summary and tags but keeps the name.
    if infer_kind(identity_url) == "repo" and (meta := repo_meta(identity_url)):
        return f"{meta['owner']}/{meta['repo']}"[:MAX_TITLE_LENGTH]
    host = (urlsplit(identity_url).hostname or identity_url).removeprefix("www.")
    return host[:MAX_TITLE_LENGTH]


async def _fresh_site(session: AsyncSession, user_id: str, site_id: str) -> SiteResponse:
    # Usage signals are bulk UPDATEs that bypass the identity map.
    session.expire_all()
    return await service.get_site(session, user_id, site_id)


# ------------------------------------------------------------------- reads


async def search_resources(
    session: AsyncSession,
    identity: AgentIdentity,
    *,
    query: str = "",
    kind: SiteKind | None = None,
    state: SiteStateFilter | None = None,
    tag: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    q = " ".join(query.split())[:MAX_QUERY_LENGTH] or None
    tag_id: str | None = None
    if tag is not None and tag.strip():
        tag_id = await session.scalar(
            select(Tag.id).where(
                Tag.user_id == identity.user_id,
                Tag.normalized_name == _tag_key(tag),
            )
        )
        if tag_id is None:
            return {
                "total": 0,
                "items": [],
                "note": f"No tag named {tag!r}. Call list_taxonomy to see existing tags.",
            }
    result = await service.list_sites(
        session,
        identity.user_id,
        q=q,
        category_id=None,
        tag_id=tag_id,
        space_id=None,
        pinned=None,
        # Keyword relevance without an embedding Provider (v1): exact title
        # hits first, then most recently updated.
        sort="relevance" if q else "created",
        direction="desc",
        cursor=None,
        limit=max(1, min(limit, MAX_SEARCH_LIMIT)),
        kind=kind,
        state=state,
    )
    payload: dict[str, Any] = {
        "total": result.aggregate.matched_count,
        "items": [compact_resource(site) for site in result.items],
    }
    if q and not result.items:
        # Keyword search ANDs every keyword; a sentence-like query from an LLM
        # misses items that are saved. Say how to retry so the Agent does not
        # conclude "never saved" and fall back to the web.
        payload["note"] = (
            "No saved item contains every keyword. Retry with fewer or different "
            "keywords (one distinctive name or topic works best, or its Chinese/English "
            "equivalent) before concluding it was never saved."
        )
    return payload


async def get_resource(
    session: AsyncSession,
    identity: AgentIdentity,
    *,
    resource_id: str,
) -> dict[str, Any]:
    return resource_detail(await service.get_site(session, identity.user_id, resource_id))


async def get_rediscover(
    session: AsyncSession,
    identity: AgentIdentity,
    *,
    limit: int = 4,
) -> dict[str, Any]:
    result = await service.rediscover(
        session,
        identity.user_id,
        limit=max(1, min(limit, MAX_REDISCOVER_LIMIT)),
        shuffle=0,
    )
    return {
        "generated_for": result.generated_for.isoformat(),
        "items": [
            {
                **compact_resource(item.site),
                "saved_days": item.saved_days,
                "reason": item.reason,
            }
            for item in result.items
        ],
    }


async def list_taxonomy(session: AsyncSession, identity: AgentIdentity) -> dict[str, Any]:
    categories = await service.list_categories(session, identity.user_id)
    tags = sorted(
        (await service.list_tags(session, identity.user_id)).items,
        key=lambda tag: (-tag.site_count, tag.name.casefold()),
    )
    payload: dict[str, Any] = {
        "categories": [
            {"name": item.name, "count": item.site_count, "is_default": item.is_default}
            for item in categories.items
        ],
        "tags": [{"name": tag.name, "count": tag.site_count} for tag in tags[:MAX_TAXONOMY_TAGS]],
    }
    if len(tags) > MAX_TAXONOMY_TAGS:
        payload["tags_truncated"] = True
        payload["tag_total"] = len(tags)
    return payload


# ------------------------------------------------------------------ writes


async def save_resource(
    session: AsyncSession,
    identity: AgentIdentity,
    database: Database,
    *,
    url: str,
    title: str | None = None,
    tags: list[str] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    user_id = identity.user_id
    try:
        original_url, identity_url = service._site_url(url)
    except service.LibraryValidationError as error:
        raise ToolInputError(f"Cannot save this URL: {error.message}") from None

    async def existing_result() -> dict[str, Any] | None:
        existing = await session.scalar(
            select(Site).where(Site.user_id == user_id, Site.identity_url == identity_url)
        )
        if existing is None:
            return None
        return {
            "already_saved": True,
            "resource": resource_detail(await service._site_response(session, user_id, existing)),
            "note": "This URL was already in the library; nothing was changed. "
            "Use update_resource to change its tags, category or star.",
        }

    if (found := await existing_result()) is not None:
        return found
    tag_names = _new_tag_names(tags)
    if note is not None and len(note.strip()) > MAX_NOTE_LENGTH:
        raise ToolInputError(f"note must be at most {MAX_NOTE_LENGTH} characters.")
    if not await reserve_account_taxonomy(session, user_id):
        raise ToolInputError("The account is not available.")
    selected_tags, _ = await _ensure_tags(session, user_id, tag_names)
    payload = SiteCreateRequest(
        name=_resource_name(title, identity_url),
        url=original_url,
        description=(note or "").strip(),
        tag_ids=[tag.id for tag in selected_tags],
        source="agent",
    )
    try:
        created = await service.create_site(session, user_id, payload)
    except service.LibraryConflictError as error:
        # Lost a race with another save of the same URL: report that one.
        if error.code == "duplicate_url" and (found := await existing_result()) is not None:
            return found
        raise
    # Same background enrichment as a save from the website.
    schedule_site_enrichment(database, user_id=user_id, site_id=created.id)
    await record_write(
        session,
        identity,
        tool="save_resource",
        site_id=created.id,
        summary=f"收藏 {original_url}",
    )
    return {
        "already_saved": False,
        "resource": resource_detail(created),
        "note": "Saved. Background analysis fills in the summary and category (and "
        "the description and tags you did not give) within a minute or two.",
    }


async def update_resource(
    session: AsyncSession,
    identity: AgentIdentity,
    *,
    resource_id: str,
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
    category: str | None = None,
    starred: bool | None = None,
    kind: SiteKind | None = None,
) -> dict[str, Any]:
    user_id = identity.user_id
    if not add_tags and not remove_tags and category is None and starred is None and kind is None:
        raise ToolInputError(
            "Nothing to update: pass at least one of add_tags, remove_tags, category, "
            "starred or kind."
        )
    added_names = _new_tag_names(add_tags)
    remove_keys = {_tag_key(name): name for name in remove_tags or []}
    if len(remove_keys) > MAX_TAGS_PER_CALL:
        raise ToolInputError(f"At most {MAX_TAGS_PER_CALL} tags per call.")

    # Plain values, not the ORM row: a version-conflict retry rolls the session
    # back, which expires every loaded instance.
    target_category: tuple[str, str] | None = None
    if category is not None:
        try:
            category_key = service._display_name(category, maximum=80, field="分类名称")[1]
        except service.LibraryValidationError as error:
            raise ToolInputError(f"Invalid category name: {error.message}") from None
        category_row = (
            await session.execute(
                select(Category.id, Category.name).where(
                    Category.user_id == user_id,
                    Category.normalized_name == category_key,
                )
            )
        ).one_or_none()
        if category_row is not None:
            target_category = (category_row.id, category_row.name)
        else:
            categories = await service.list_categories(session, user_id)
            available = [item.name for item in categories.items]
            raise ToolInputError(
                f"No category named {category!r}. Available categories: "
                f"{', '.join(available)}. Categories are created by the user on the "
                "website; choose one of these exactly."
            )

    for attempt in range(UPDATE_ATTEMPTS):
        site = await service._owned_site(session, user_id, resource_id)
        current_tags = await _site_tags(session, user_id, site.id)
        current_keys = {tag.normalized_name for tag in current_tags}
        removed = [tag for tag in current_tags if tag.normalized_name in remove_keys]
        removed_keys = {tag.normalized_name for tag in removed}
        to_add = [
            (display, key)
            for display, key in added_names
            if key not in current_keys and key not in remove_keys
        ]
        category_changed = target_category is not None and target_category[0] != site.category_id
        kind_changed = kind is not None and kind != site.kind
        parts: list[str] = []
        if to_add or removed or category_changed or kind_changed:
            fields: dict[str, Any] = {"expected_version": site.version}
            if to_add or removed:
                if not await reserve_account_taxonomy(session, user_id):
                    raise ToolInputError("The account is not available.")
                new_tags, _ = await _ensure_tags(session, user_id, to_add)
                fields["tag_ids"] = [
                    tag.id for tag in current_tags if tag.normalized_name not in removed_keys
                ] + [tag.id for tag in new_tags]
                if len(fields["tag_ids"]) > MAX_TAGS_PER_RESOURCE:
                    raise ToolInputError(
                        f"A resource can have at most {MAX_TAGS_PER_RESOURCE} tags; "
                        "remove some first."
                    )
                if new_tags:
                    # Reused tags keep their stored spelling in the summary.
                    parts.append(f"添加标签 {'、'.join(tag.name for tag in new_tags)}")
                if removed:
                    parts.append(f"移除标签 {'、'.join(tag.name for tag in removed)}")
            if category_changed and target_category is not None:
                fields["category_id"] = target_category[0]
                parts.append(f"分类改为 {target_category[1]}")
            if kind_changed and kind is not None:
                fields["kind"] = kind
                parts.append(f"类型改为{KIND_LABELS[kind]}")
            try:
                await service.update_site(session, user_id, site.id, SiteUpdateRequest(**fields))
            except service.LibraryConflictError as error:
                # Optimistic version: someone edited the site between our read
                # and write. Re-read and re-apply the same intent.
                if error.code == "version_conflict" and attempt + 1 < UPDATE_ATTEMPTS:
                    await session.rollback()
                    continue
                raise
        break

    if starred is not None:
        currently_starred = (await service.get_site(session, user_id, resource_id)).starred
        if currently_starred != starred:
            await service.update_site_state(
                session, user_id, resource_id, SiteStateUpdateRequest(starred=starred)
            )
            parts.append("加星标" if starred else "取消星标")

    final = await _fresh_site(session, user_id, resource_id)
    result: dict[str, Any] = {"changed": bool(parts), "resource": resource_detail(final)}
    ignored = [name for key, name in remove_keys.items() if key not in removed_keys]
    if ignored:
        result["not_tagged"] = ignored
    if parts:
        await record_write(
            session,
            identity,
            tool="update_resource",
            site_id=final.id,
            summary=f"{final.name}：{'；'.join(parts)}",
        )
    return result


async def mark_resource(
    session: AsyncSession,
    identity: AgentIdentity,
    *,
    resource_id: str,
    action: MarkAction,
    snooze_days: int = 7,
) -> dict[str, Any]:
    user_id = identity.user_id
    site = await service._owned_site(session, user_id, resource_id)
    name = site.name
    if action == "opened":
        await service.record_site_open(session, user_id, resource_id)
        summary = f"标记已打开 {name}"
    elif action == "seen":
        await service.update_site_state(
            session, user_id, resource_id, SiteStateUpdateRequest(seen=True)
        )
        summary = f"标记已看 {name}"
    elif action == "snooze":
        if not 1 <= snooze_days <= MAX_SNOOZE_DAYS:
            raise ToolInputError(f"snooze_days must be between 1 and {MAX_SNOOZE_DAYS}.")
        await service.update_site_state(
            session, user_id, resource_id, SiteStateUpdateRequest(snooze_days=snooze_days)
        )
        summary = f"推迟 {snooze_days} 天再重温 {name}"
    else:
        await service.update_site_state(
            session, user_id, resource_id, SiteStateUpdateRequest(resurface_muted=True)
        )
        summary = f"不再重温 {name}"
    final = await _fresh_site(session, user_id, resource_id)
    await record_write(
        session,
        identity,
        tool="mark_resource",
        site_id=final.id,
        summary=summary,
    )
    return {"action": action, "resource": resource_detail(final)}


MAX_FEED_DIGEST_LIMIT = 20


async def get_feed_digest(
    session: AsyncSession,
    identity: AgentIdentity,
    *,
    limit: int = 10,
) -> dict[str, Any]:
    """Today's picks from the user's RSS / podcast subscriptions (read-only)."""

    picks = await feed_service.daily_picks(
        session,
        identity.user_id,
        limit=max(1, min(limit, MAX_FEED_DIGEST_LIMIT)),
    )
    return {
        "generated_at": _iso(datetime.now(UTC)),
        "items": [
            {
                "title": pick.item.title,
                "url": pick.item.url,
                "source": pick.feed_title,
                "kind": pick.feed_kind,
                "summary": pick.item.summary[:280],
                "author": pick.item.author,
                "published_at": _iso(pick.item.published_at),
                "audio_url": pick.item.audio_url,
                "duration_seconds": pick.item.duration_seconds,
                "saved_to_library": pick.item.saved_site_id is not None,
                "reason": pick.reason,
            }
            for pick in picks
        ],
    }
