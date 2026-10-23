from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import ScalarResult
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.database import Database
from webhub.db.locking import reserve_sites
from webhub.db.migrations import upgrade_database
from webhub.db.models import Category, Site, Space, SpaceMember, User
from webhub.library.schemas import SiteBulkDeleteRequest
from webhub.library.service import sites as site_service
from webhub.spaces import service as space_service
from webhub.spaces.schemas import SpaceMemberAddRequest, SpaceMemberBatchRequest


def _database(tmp_path: Path) -> Database:
    url = f"sqlite+aiosqlite:///{tmp_path / 'main.sqlite3'}"
    upgrade_database(url)
    return Database(url)


async def _seed(database: Database) -> None:
    async with database.sessions() as session:
        session.add(User(id="alice", username="alice", display_name="Alice", password_hash="test"))
        await session.flush()
        session.add(
            Category(
                id="category", user_id="alice", name="收藏", normalized_name="收藏", is_default=True
            )
        )
        session.add_all(
            Space(id=space_id, user_id="alice", name=space_id, normalized_name=space_id)
            for space_id in ("old", "new")
        )
        await session.flush()
        session.add(
            Site(
                id="site",
                user_id="alice",
                category_id="category",
                name="Needle",
                normalized_name="needle",
                original_url="https://example.test/",
                identity_url="https://example.test/",
                position=0,
            )
        )
        await session.flush()
        session.add(SpaceMember(user_id="alice", space_id="old", site_id="site", position=0))
        await session.commit()


async def _delete(session: AsyncSession, *, bulk: bool) -> None:
    if bulk:
        await site_service.bulk_delete_sites(
            session,
            "alice",
            SiteBulkDeleteRequest(
                items=[{"site_id": "site", "expected_version": 1}],
            ),
        )
    else:
        await site_service.delete_site(session, "alice", "site", expected_version=1)


@pytest.mark.parametrize("bulk_delete", [False, True])
@pytest.mark.parametrize("batch_add", [False, True])
def test_delete_holds_membership_snapshot_until_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bulk_delete: bool,
    batch_add: bool,
) -> None:
    database = _database(tmp_path)

    async def scenario() -> None:
        await _seed(database)
        members_read, release_delete = asyncio.Event(), asyncio.Event()
        add_started, add_reserved = asyncio.Event(), asyncio.Event()
        original_reserve = space_service.reserve_sites

        async def observed_reserve(
            session: AsyncSession,
            user_id: str,
            site_ids: Sequence[str],
        ) -> int:
            add_started.set()
            result = await original_reserve(session, user_id, site_ids)
            add_reserved.set()
            return result

        monkeypatch.setattr(space_service, "reserve_sites", observed_reserve)

        async def delete_paused() -> None:
            async with database.sessions() as session:
                original_scalars = session.scalars

                async def paused_scalars(
                    statement: object,
                    *args: object,
                    **kwargs: object,
                ) -> ScalarResult[object]:
                    result = await original_scalars(statement, *args, **kwargs)
                    descriptions = getattr(statement, "column_descriptions", ())
                    if descriptions and descriptions[0].get("entity") is SpaceMember:
                        members_read.set()
                        await release_delete.wait()
                    return result

                monkeypatch.setattr(session, "scalars", paused_scalars)
                await _delete(session, bulk=bulk_delete)

        async def add_attempt() -> None:
            async with database.sessions() as session:
                with pytest.raises(space_service.SpaceNotFoundError) as caught:
                    if batch_add:
                        await space_service.add_members_batch(
                            session,
                            "alice",
                            SpaceMemberBatchRequest(
                                operation_id="race-operation",
                                site_ids=["site"],
                                target={
                                    "mode": "existing",
                                    "space_name": "new",
                                    "space_id": "new",
                                    "expected_version": 1,
                                },
                            ),
                        )
                    else:
                        await space_service.add_member(
                            session,
                            "alice",
                            "new",
                            SpaceMemberAddRequest(site_id="site", expected_version=1),
                        )
                assert caught.value.status_code == 404

        deleting = asyncio.create_task(delete_paused())
        try:
            await asyncio.wait_for(members_read.wait(), 5)
            # A zero-timeout independent connection proves the writer lock is
            # already held; this fails deterministically if deletion stops
            # reserving Sites before its membership snapshot.
            with (
                sqlite3.connect(tmp_path / "main.sqlite3", timeout=0) as probe,
                pytest.raises(sqlite3.OperationalError, match="database is locked"),
            ):
                probe.execute("UPDATE sites SET version=version WHERE id='site'")
            adding = asyncio.create_task(add_attempt())
            await asyncio.wait_for(add_started.wait(), 5)
            # This event sits immediately after the real UPDATE. It cannot fire
            # while deletion owns SQLite's writer reservation.
            assert not add_reserved.is_set()
            assert not adding.done()
            release_delete.set()
            await asyncio.wait_for(asyncio.gather(deleting, adding), 5)
            async with database.sessions() as session:
                assert await session.get(Site, "site") is None
                old_space = await session.get(Space, "old")
                new_space = await session.get(Space, "new")
                assert old_space.version == 2
                assert new_space.version == 1
                assert (await session.scalars(select(SpaceMember))).all() == []
        finally:
            release_delete.set()
            await database.dispose()

    asyncio.run(scenario())


def test_reserve_sites_preserves_revision_time_and_fts(tmp_path: Path) -> None:
    database = _database(tmp_path)

    async def scenario() -> None:
        await _seed(database)
        async with database.sessions() as session:
            before = await session.get(Site, "site")
            version, updated_at = before.version, before.updated_at
            query = text("SELECT rowid FROM site_search WHERE site_search MATCH 'Needle'")
            matches = (await session.execute(query)).all()
            assert matches
            assert await reserve_sites(session, "other", ["site"]) == 0
            assert await reserve_sites(session, "alice", ["site", "missing"]) == 1
            await session.commit()
            await session.refresh(before)
            assert (before.version, before.updated_at) == (version, updated_at)
            assert (await session.execute(query)).all() == matches
        await database.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("bulk", [False, True])
def test_reservation_mismatch_rolls_back_as_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bulk: bool,
) -> None:
    database = _database(tmp_path)

    async def lost_reservation(
        _session: AsyncSession,
        _user_id: str,
        _site_ids: Sequence[str],
    ) -> int:
        return 0

    monkeypatch.setattr(site_service, "reserve_sites", lost_reservation)

    async def scenario() -> None:
        await _seed(database)
        async with database.sessions() as session:
            with pytest.raises(site_service.LibraryConflictError) as caught:
                await _delete(session, bulk=bulk)
            assert caught.value.code == ("bulk_delete_conflict" if bulk else "version_conflict")
            assert not session.in_transaction()
            assert await session.get(Site, "site") is not None
        await database.dispose()

    asyncio.run(scenario())
