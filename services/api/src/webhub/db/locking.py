"""Small cross-database write reservations shared by service layers."""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import User


async def reserve_account_taxonomy(session: AsyncSession, user_id: str) -> bool:
    """Serialize one account's category/tag snapshot and its dependent writes.

    Updating a column to itself is intentional: SQLite obtains its one writer
    reservation, while multi-writer databases take a row lock on the account.
    Every taxonomy writer uses this same mutex before Category, Tag, or Site.
    """

    reserved = await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(updated_at=User.updated_at)
        .execution_options(synchronize_session=False)
    )
    return reserved.rowcount == 1  # type: ignore[attr-defined]


__all__ = ["reserve_account_taxonomy"]
