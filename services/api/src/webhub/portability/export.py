"""Stream a whitelist of account data; credentials and execution traces never enter it."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.chat.models import Conversation, ConversationMessage
from webhub.db.models import Category, Feed, Site, SiteTag, Space, SpaceMember, Tag, User


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def backup_meta(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}
    if isinstance(value.get("github"), dict):
        value["github"].pop("etag", None)
    return value


def _encode(value: object) -> bytes:
    def temporal(item: object) -> str:
        if isinstance(item, datetime):
            return utc(item).isoformat().replace("+00:00", "Z")
        raise TypeError("Unsupported backup value")

    return json.dumps(value, ensure_ascii=False, default=temporal, allow_nan=False).encode()


async def _categories(session: AsyncSession, user_id: str) -> AsyncIterator[dict[str, object]]:
    rows = await session.stream_scalars(
        select(Category)
        .where(Category.user_id == user_id)
        .order_by(Category.id)
        .execution_options(yield_per=500)
    )
    async for row in rows:
        yield {"key": row.id, "name": row.name, "icon": row.icon, "is_default": row.is_default}


async def _tags(session: AsyncSession, user_id: str) -> AsyncIterator[dict[str, object]]:
    rows = await session.stream_scalars(
        select(Tag).where(Tag.user_id == user_id).order_by(Tag.id).execution_options(yield_per=500)
    )
    async for row in rows:
        yield {"key": row.id, "name": row.name}


async def _sites(session: AsyncSession, user_id: str) -> AsyncIterator[dict[str, object]]:
    rows = await session.stream_scalars(
        select(Site)
        .where(Site.user_id == user_id)
        .order_by(Site.id)
        .execution_options(yield_per=500)
    )
    async for batch in rows.partitions(500):
        links = await session.execute(
            select(SiteTag.site_id, SiteTag.tag_id)
            .where(SiteTag.user_id == user_id, SiteTag.site_id.in_([row.id for row in batch]))
            .order_by(SiteTag.tag_id)
        )
        tags: dict[str, list[str]] = {}
        for site_id, tag_id in links:
            tags.setdefault(site_id, []).append(tag_id)
        for row in batch:
            yield {
                "key": row.id,
                "url": row.original_url,
                "name": row.name,
                "summary": row.summary,
                "description": row.description,
                "kind": row.kind,
                "kind_source": row.kind_source,
                "category_key": row.category_id,
                "tag_keys": tags.get(row.id, []),
                "pinned": row.pinned,
                "favicon_url": row.favicon_url,
                "preview_url": row.preview_url,
                "meta": backup_meta(row.meta_json),
                "created_at": row.created_at,
                "open_count": row.open_count,
                "last_opened_at": row.last_opened_at,
                "seen_at": row.seen_at,
                "starred_at": row.starred_at,
                "resurface_muted": row.resurface_muted,
            }


async def _spaces(session: AsyncSession, user_id: str) -> AsyncIterator[dict[str, object]]:
    rows = await session.stream_scalars(
        select(Space)
        .where(Space.user_id == user_id)
        .order_by(Space.id)
        .execution_options(yield_per=500)
    )
    async for row in rows:
        members = (
            await session.scalars(
                select(SpaceMember.site_id)
                .where(SpaceMember.user_id == user_id, SpaceMember.space_id == row.id)
                .order_by(SpaceMember.position, SpaceMember.site_id)
            )
        ).all()
        yield {"key": row.id, "name": row.name, "site_keys": list(members)}


async def _feeds(session: AsyncSession, user_id: str) -> AsyncIterator[dict[str, object]]:
    rows = await session.stream_scalars(
        select(Feed)
        .where(Feed.user_id == user_id)
        .order_by(Feed.id)
        .execution_options(yield_per=500)
    )
    async for row in rows:
        yield {"url": row.url, "title": row.title, "kind": row.kind, "enabled": row.enabled}


async def _conversations(session: AsyncSession, user_id: str) -> AsyncIterator[dict[str, object]]:
    rows = await session.stream_scalars(
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.id)
        .execution_options(yield_per=100)
    )
    async for row in rows:
        # Only visible text. parts/sources/artifacts/metadata may contain reasoning or tool output.
        messages = (
            await session.execute(
                select(
                    ConversationMessage.role,
                    ConversationMessage.content,
                    ConversationMessage.created_at,
                )
                .where(
                    ConversationMessage.user_id == user_id,
                    ConversationMessage.conversation_id == row.id,
                    ConversationMessage.role.in_(("user", "assistant")),
                )
                .order_by(ConversationMessage.sequence)
            )
        ).all()
        yield {
            "title": row.title,
            "created_at": row.created_at,
            "messages": [
                {"role": role, "text": content, "created_at": created_at}
                for role, content, created_at in messages
            ],
        }


async def export_backup(
    session: AsyncSession, user: User, *, now: datetime | None = None
) -> AsyncIterator[bytes]:
    # sqlite3 legacy transaction mode does not BEGIN for SELECT. One explicit
    # read transaction keeps category/tag references consistent across chunks.
    await session.execute(text("BEGIN"))
    try:
        head = {
            "format": "webhub-backup",
            "version": 1,
            "exported_at": now or datetime.now(UTC),
            "account": {"username": user.username, "display_name": user.display_name},
        }
        yield _encode(head)[:-1]
        for name, reader in (
            ("categories", _categories),
            ("tags", _tags),
            ("sites", _sites),
            ("spaces", _spaces),
            ("feeds", _feeds),
            ("conversations", _conversations),
        ):
            yield b", " + _encode(name) + b": ["
            first = True
            async for row in reader(session, user.id):
                if not first:
                    yield b","
                yield _encode(row)
                first = False
            yield b"]"
        yield b"}"
    finally:
        await session.rollback()
