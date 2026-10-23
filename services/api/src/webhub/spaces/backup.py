"""Space-owned recovery writes; the caller commits the complete backup once."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import Space, SpaceMember, utc_now
from webhub.spaces.service import _owned_site, _space_name


def normalize_backup_name(value: str) -> tuple[str, str]:
    return _space_name(value)


async def restore_space(
    session: AsyncSession, user_id: str, *, name: str, site_ids: list[str]
) -> tuple[str, bool]:
    display, normalized = normalize_backup_name(name)
    row = await session.scalar(
        select(Space).where(Space.user_id == user_id, Space.normalized_name == normalized)
    )
    created = row is None
    if row is None:
        row = Space(user_id=user_id, name=display, normalized_name=normalized)
        session.add(row)
        await session.flush()
    members = (
        await session.execute(
            select(SpaceMember.site_id, SpaceMember.position).where(
                SpaceMember.user_id == user_id, SpaceMember.space_id == row.id
            )
        )
    ).all()
    existing = {site_id for site_id, _ in members}
    position = max((position for _, position in members), default=-1) + 1
    initial_position = position
    for site_id in dict.fromkeys(site_ids):
        if site_id in existing:
            continue
        await _owned_site(session, user_id, site_id)
        session.add(
            SpaceMember(user_id=user_id, space_id=row.id, site_id=site_id, position=position)
        )
        position += 1
    if not created and position > initial_position:
        await session.execute(
            update(Space)
            .where(Space.user_id == user_id, Space.id == row.id)
            .values(version=Space.version + 1, updated_at=utc_now())
        )
    await session.flush()
    return row.id, created
