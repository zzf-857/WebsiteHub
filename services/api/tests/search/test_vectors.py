from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import SiteEmbedding, User
from webhub.main import create_app
from webhub.search.vectors import (
    content_digest,
    cosine_similarity,
    drop_index,
    embedding_text,
    nearest,
    pack_vector,
    stale_sites,
    store_embedding,
    unpack_vector,
)

ORIGIN = {"Origin": "http://testserver"}


@contextmanager
def _two_accounts(tmp_path: Path) -> Iterator[Settings]:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'main.sqlite3').as_posix()}",
        data_directory=tmp_path,
        provider_master_key=b"provider-test-master-key-32bytes",
    )
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as client:
        for name in ("alice", "bob"):
            client.cookies.clear()
            assert (
                client.post(
                    "/api/auth/register",
                    json={"username": name, "password": "a sufficiently secure password"},
                    headers=ORIGIN,
                ).status_code
                == 201
            )
            created = client.post(
                "/api/library/sites",
                json={"name": f"{name} 的站点", "url": f"https://{name}.example.com"},
                headers=ORIGIN,
            )
            assert created.status_code == 201, created.text
    yield settings


def _run(settings: Settings, scenario) -> object:
    async def wrapped() -> object:
        database = Database(settings.database_url)
        try:
            async with database.sessions() as session:
                return await scenario(session)
        finally:
            await database.dispose()

    return asyncio.run(wrapped())


async def _user_and_site(session, username: str) -> tuple[str, str]:
    from webhub.db.models import Site

    user_id = await session.scalar(select(User.id).where(User.username == username))
    site_id = await session.scalar(select(Site.id).where(Site.user_id == user_id))
    return str(user_id), str(site_id)


def test_vectors_survive_a_round_trip_without_a_numeric_dependency() -> None:
    values = [0.5, -0.25, 1.0, 0.0]
    assert unpack_vector(pack_vector(values)) == values


def test_cosine_handles_the_cases_that_would_otherwise_raise() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0
    # Mismatched dimensions mean different models — meaningless, not fatal.
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0
    # A zero vector has no direction; scoring it must not divide by zero.
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine_similarity([], []) == 0.0


def test_the_digest_changes_with_the_model_not_just_the_text() -> None:
    """Same text, different model → incomparable vector → cache must miss."""

    assert content_digest("abc", "model-a") != content_digest("abc", "model-b")
    assert content_digest("abc", "model-a") == content_digest("abc", "model-a")


def test_embedding_text_excludes_the_url() -> None:
    # URLs add tokens that look like content and can carry secrets.
    text = embedding_text("Figma", "界面设计工具", "设计")
    assert "figma.com" not in text
    assert "Figma" in text and "界面设计工具" in text


def test_semantic_search_never_crosses_accounts(tmp_path: Path) -> None:
    """The criterion stated as the failure it prevents."""

    with _two_accounts(tmp_path) as settings:

        async def scenario(session) -> tuple[list[str], list[str]]:
            alice_id, alice_site = await _user_and_site(session, "alice")
            bob_id, bob_site = await _user_and_site(session, "bob")
            # Identical vectors on purpose: only scoping can separate them.
            for user_id, site_id in ((alice_id, alice_site), (bob_id, bob_site)):
                await store_embedding(
                    session,
                    user_id,
                    site_id,
                    model="m",
                    vector=[1.0, 0.0],
                    content_hash=content_digest("x", "m"),
                )
            alice_hits = await nearest(session, alice_id, [1.0, 0.0], model="m", limit=10)
            bob_hits = await nearest(session, bob_id, [1.0, 0.0], model="m", limit=10)
            return (
                [hit.site_id for hit in alice_hits],
                [hit.site_id for hit in bob_hits],
            )

        alice_hits, bob_hits = _run(settings, scenario)  # type: ignore[misc]

    assert len(alice_hits) == 1
    assert len(bob_hits) == 1
    assert set(alice_hits).isdisjoint(bob_hits)


def test_a_different_model_is_not_mixed_into_results(tmp_path: Path) -> None:
    with _two_accounts(tmp_path) as settings:

        async def scenario(session) -> list[str]:
            user_id, site_id = await _user_and_site(session, "alice")
            await store_embedding(
                session,
                user_id,
                site_id,
                model="old-model",
                vector=[1.0, 0.0],
                content_hash=content_digest("x", "old-model"),
            )
            hits = await nearest(session, user_id, [1.0, 0.0], model="new-model", limit=10)
            return [hit.site_id for hit in hits]

        assert _run(settings, scenario) == []


def test_unchanged_sites_are_not_re_embedded(tmp_path: Path) -> None:
    """Re-embedding spends the user's quota; the digest is what prevents it."""

    with _two_accounts(tmp_path) as settings:

        async def scenario(session) -> tuple[int, int]:
            user_id, site_id = await _user_and_site(session, "alice")
            before = await stale_sites(session, user_id, model="m", limit=50)
            for pending_id, text in before:
                await store_embedding(
                    session,
                    user_id,
                    pending_id,
                    model="m",
                    vector=[1.0, 0.0],
                    content_hash=content_digest(text, "m"),
                )
            after = await stale_sites(session, user_id, model="m", limit=50)
            return len(before), len(after)

        before_count, after_count = _run(settings, scenario)  # type: ignore[misc]

    assert before_count == 1
    assert after_count == 0


def test_the_index_can_be_dropped_and_rebuilt_from_sqlite(tmp_path: Path) -> None:
    """Vectors are a derived cache; sites are the record."""

    with _two_accounts(tmp_path) as settings:

        async def scenario(session) -> tuple[int, int, int]:
            user_id, site_id = await _user_and_site(session, "alice")
            await store_embedding(
                session,
                user_id,
                site_id,
                model="m",
                vector=[1.0, 0.0],
                content_hash=content_digest("x", "m"),
            )
            removed = await drop_index(session, user_id)
            remaining = len(
                (
                    await session.scalars(
                        select(SiteEmbedding).where(SiteEmbedding.user_id == user_id)
                    )
                ).all()
            )
            # Everything needed to rebuild is still in `sites`.
            rebuildable = len(await stale_sites(session, user_id, model="m", limit=50))
            return removed, remaining, rebuildable

        removed, remaining, rebuildable = _run(settings, scenario)  # type: ignore[misc]

    assert removed == 1
    assert remaining == 0
    assert rebuildable == 1


def test_dropping_one_account_index_leaves_the_other_alone(tmp_path: Path) -> None:
    with _two_accounts(tmp_path) as settings:

        async def scenario(session) -> int:
            alice_id, alice_site = await _user_and_site(session, "alice")
            bob_id, bob_site = await _user_and_site(session, "bob")
            for user_id, site_id in ((alice_id, alice_site), (bob_id, bob_site)):
                await store_embedding(
                    session,
                    user_id,
                    site_id,
                    model="m",
                    vector=[1.0, 0.0],
                    content_hash=content_digest("x", "m"),
                )
            await drop_index(session, alice_id)
            return len(
                (
                    await session.scalars(
                        select(SiteEmbedding).where(SiteEmbedding.user_id == bob_id)
                    )
                ).all()
            )

        assert _run(settings, scenario) == 1
