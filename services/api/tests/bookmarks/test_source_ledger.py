"""Q55: the bookmark source ledger written by a confirmed import apply.

Covers the PRD §4.2 / FR-DA-005 / FR-DA-007 contract: every occurrence and the
folder tree survive apply, occurrences link to new *and* already-existing
Sites (including children merged into a homepage), skipped/unsupported items
are kept unlinked, the ledger commits atomically with the library, replays do
not duplicate it, and nothing crosses accounts.
"""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select
from test_preview_api import (  # type: ignore[import-not-found]
    ORIGIN,
    PreviewEnvironment,
    _create_similarity_job,
    _use_token,
    preview_environment,
)

from webhub.bookmarks import apply as bookmark_apply
from webhub.bookmarks import queries as bookmark_queries
from webhub.bookmarks import routes as bookmark_routes
from webhub.bookmarks.source_ledger import SourceLedgerError, record_source_ledger
from webhub.db.database import Database
from webhub.db.models import (
    BookmarkImportJob,
    BookmarkSourceFolder,
    BookmarkSourceOccurrence,
    Site,
    SiteImportOrigin,
)

__all__ = ["preview_environment"]

SHARED_URL = "https://example.com/shared?q=1#keep"


@pytest.fixture(autouse=True)
def _no_background_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bookmark_routes.ingestion_worker,
        "schedule_analysis",
        lambda *_a, **_k: SimpleNamespace(rejected=False),
    )
    monkeypatch.setattr(
        bookmark_routes.ingestion_worker, "ensure_auto_backfill", lambda *_a, **_k: False
    )


def _apply(environment: PreviewEnvironment, job_id: str, *, decision_version: int = 1):
    return environment.client.post(
        f"/api/bookmark-imports/{job_id}/apply",
        json={"expected_job_version": 3, "expected_decision_version": decision_version},
        headers=ORIGIN,
    )


async def _ledger(database: Database, user_id: str) -> dict[str, object]:
    async with database.sessions() as session:
        folders = (
            await session.execute(
                select(
                    BookmarkSourceFolder.id,
                    BookmarkSourceFolder.parent_id,
                    BookmarkSourceFolder.title,
                    BookmarkSourceFolder.display_path,
                    BookmarkSourceFolder.snapshot_id,
                )
                .where(BookmarkSourceFolder.user_id == user_id)
                .order_by(BookmarkSourceFolder.source_sequence)
            )
        ).all()
        occurrences = (
            await session.execute(
                select(
                    BookmarkSourceOccurrence.id,
                    BookmarkSourceOccurrence.raw_url,
                    BookmarkSourceOccurrence.raw_title,
                    BookmarkSourceOccurrence.validation_status,
                    BookmarkSourceOccurrence.folder_id,
                    BookmarkSourceOccurrence.snapshot_id,
                )
                .where(BookmarkSourceOccurrence.user_id == user_id)
                .order_by(BookmarkSourceOccurrence.source_sequence)
            )
        ).all()
        origins = (
            await session.execute(
                select(
                    SiteImportOrigin.source_occurrence_id,
                    SiteImportOrigin.site_id,
                    SiteImportOrigin.link_action,
                    SiteImportOrigin.site_version_at_link,
                    Site.identity_url,
                    Site.version,
                ).join(
                    Site,
                    (Site.user_id == SiteImportOrigin.user_id)
                    & (Site.id == SiteImportOrigin.site_id),
                )
                .where(SiteImportOrigin.user_id == user_id)
            )
        ).all()
    return {"folders": folders, "occurrences": occurrences, "origins": origins}


async def _site_id(database: Database, user_id: str, identity_url: str) -> str:
    async with database.sessions() as session:
        site_id = await session.scalar(
            select(Site.id).where(Site.user_id == user_id, Site.identity_url == identity_url)
        )
    assert site_id is not None
    return site_id


def test_apply_writes_folder_tree_every_occurrence_and_links_new_and_existing_sites(
    preview_environment: PreviewEnvironment,
) -> None:
    environment = preview_environment
    database = Database(environment.database_url)
    _use_token(environment.client, environment.alice_token)
    try:
        existing_id = asyncio.run(_site_id(database, environment.alice_id, SHARED_URL))
        existing_before = environment.client.get(f"/api/library/sites/{existing_id}").json()

        applied = _apply(environment, environment.alice_job_id)
        assert applied.status_code == 200, applied.text
        ledger = asyncio.run(_ledger(database, environment.alice_id))

        async def stamps() -> list[object]:
            async with database.sessions() as session:
                return [
                    await session.scalar(
                        select(model.created_at).where(model.user_id == environment.alice_id)
                    )
                    for model in (BookmarkSourceFolder, BookmarkSourceOccurrence)
                ]

        # INSERT … SELECT binds the timestamp like the ORM does.
        assert all(isinstance(stamp, datetime) for stamp in asyncio.run(stamps()))

        # Folder tree: names, order and parent links survive verbatim.
        folders = ledger["folders"]
        assert [row.title for row in folders] == ["Root", "Nested", "Other"]
        by_id = {row.id: row for row in folders}
        nested = folders[1]
        assert by_id[nested.parent_id].title == "Root"
        assert json.loads(nested.display_path) == ["Root", "Nested"]
        assert folders[0].parent_id is None and folders[2].parent_id is None

        # Every bookmark position is its own occurrence, invalid/unsupported too.
        occurrences = ledger["occurrences"]
        assert len(occurrences) == 6
        assert {row.snapshot_id for row in occurrences} == {folders[0].snapshot_id}
        statuses = {row.raw_url: row.validation_status for row in occurrences}
        assert statuses["file:///C:/private.txt"] == "unsupported"
        assert statuses["https://"] == "invalid"
        shared = [row for row in occurrences if row.raw_url == SHARED_URL]
        assert [row.raw_title for row in shared] == ["Short title", "A much longer shared title"]
        assert {by_id[row.folder_id].title for row in shared} == {"Root", "Nested"}

        # Links: both shared occurrences append to the existing Site without
        # touching it; skipped/unsupported occurrences stay unlinked.
        origins = {row.source_occurrence_id: row for row in ledger["origins"]}
        for row in shared:
            link = origins[row.id]
            assert link.site_id == existing_id
            assert link.link_action == "matched_existing"
            assert link.site_version_at_link == existing_before["version"]
        unlinked = {row.raw_url for row in occurrences if row.id not in origins}
        assert {"file:///C:/private.txt", "https://"} <= unlinked
        created = [row for row in ledger["origins"] if row.link_action == "created_site"]
        assert created, "new sites must be linked as created_site"
        assert all(row.version == 1 for row in created)
        assert applied.json()["created"] == len({row.site_id for row in created})

        existing_after = environment.client.get(f"/api/library/sites/{existing_id}").json()
        assert existing_after["name"] == existing_before["name"]
        assert existing_after["version"] == existing_before["version"]

        origin_view = environment.client.get(f"/api/library/sites/{existing_id}/import-origins")
        assert origin_view.status_code == 200, origin_view.text
        assert origin_view.json() == {
            "site_id": existing_id,
            "occurrence_count": 2,
            "folder_paths": [["Root"], ["Root", "Nested"]],
            "truncated": False,
        }
    finally:
        asyncio.run(database.dispose())


def test_children_merged_into_homepage_link_to_the_homepage_site(
    preview_environment: PreviewEnvironment,
) -> None:
    environment = preview_environment
    client = environment.client
    database = Database(environment.database_url)
    _use_token(client, environment.alice_token)
    try:
        job_id = asyncio.run(
            _create_similarity_job(database, environment.alice_id, key="ledger-merge")
        )
        base = f"/api/bookmark-imports/{job_id}"
        cluster = client.get(f"{base}/preview/similarity-clusters").json()["items"][0]
        decided = client.put(
            f"{base}/preview/similarity-clusters/{cluster['id']}/decision",
            json={
                "expected_job_version": 3,
                "expected_decision_version": 1,
                "decision": "merge_to_homepage",
            },
            headers=ORIGIN,
        )
        assert decided.status_code == 200, decided.text
        applied = _apply(environment, job_id, decision_version=2)
        assert applied.status_code == 200, applied.text
        assert applied.json()["created"] == 1

        homepage_id = asyncio.run(
            _site_id(database, environment.alice_id, "https://example.com/")
        )
        ledger = asyncio.run(_ledger(database, environment.alice_id))
        homepage_links = [row for row in ledger["origins"] if row.site_id == homepage_id]
        assert len(homepage_links) == 3
        assert {row.link_action for row in homepage_links} == {"created_site"}
        linked_urls = {
            row.raw_url
            for row in ledger["occurrences"]
            if row.id in {link.source_occurrence_id for link in homepage_links}
        }
        assert linked_urls == {
            "https://example.com/",
            "https://example.com/docs",
            "https://example.com/pricing?ref=bookmark",
        }
        view = client.get(f"/api/library/sites/{homepage_id}/import-origins").json()
        assert view["occurrence_count"] == 3
        assert view["folder_paths"] == [["AI 工具"]]
    finally:
        asyncio.run(database.dispose())


def test_merge_into_already_existing_homepage_links_as_matched_existing(
    preview_environment: PreviewEnvironment,
) -> None:
    environment = preview_environment
    client = environment.client
    database = Database(environment.database_url)
    _use_token(client, environment.alice_token)
    try:
        existing = client.post(
            "/api/library/sites",
            json={"name": "Manual home", "url": "https://home.example/"},
            headers=ORIGIN,
        )
        assert existing.status_code == 201, existing.text
        job_id = asyncio.run(
            _create_similarity_job(
                database, environment.alice_id, key="ledger-existing", host="home.example"
            )
        )
        base = f"/api/bookmark-imports/{job_id}"
        cluster = client.get(f"{base}/preview/similarity-clusters").json()["items"][0]
        decided = client.put(
            f"{base}/preview/similarity-clusters/{cluster['id']}/decision",
            json={
                "expected_job_version": 3,
                "expected_decision_version": 1,
                "decision": "merge_to_homepage",
            },
            headers=ORIGIN,
        )
        assert decided.status_code == 200, decided.text
        applied = _apply(environment, job_id, decision_version=2)
        assert applied.status_code == 200, applied.text
        assert applied.json()["created"] == 0

        ledger = asyncio.run(_ledger(database, environment.alice_id))
        links = [row for row in ledger["origins"] if row.site_id == existing.json()["id"]]
        assert len(links) == 3
        assert {row.link_action for row in links} == {"matched_existing"}
        after = client.get(f"/api/library/sites/{existing.json()['id']}").json()
        assert after["name"] == "Manual home"
    finally:
        asyncio.run(database.dispose())


def test_ledger_rolls_back_with_the_library_when_apply_fails_late(
    preview_environment: PreviewEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = preview_environment
    database = Database(environment.database_url)
    real = bookmark_apply.record_source_ledger

    async def fail_after_ledger(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("injected failure after ledger write")

    monkeypatch.setattr(bookmark_apply, "record_source_ledger", fail_after_ledger)

    async def scenario() -> None:
        async with database.sessions() as session:
            sites_before = await session.scalar(
                select(func.count()).select_from(Site).where(Site.user_id == environment.alice_id)
            )
        async with database.sessions() as session:
            with pytest.raises(RuntimeError, match="injected failure"):
                await bookmark_queries.apply_import(
                    session,
                    environment.alice_id,
                    environment.alice_job_id,
                    expected_job_version=3,
                    expected_decision_version=1,
                )
        ledger = await _ledger(database, environment.alice_id)
        assert ledger == {"folders": [], "occurrences": [], "origins": []}
        async with database.sessions() as session:
            sites_after = await session.scalar(
                select(func.count()).select_from(Site).where(Site.user_id == environment.alice_id)
            )
            job = await session.get(BookmarkImportJob, environment.alice_job_id)
        assert sites_after == sites_before
        assert job is not None and job.state == "parse_preview_ready"

    try:
        asyncio.run(scenario())
        # Undo the injection: the very same job then applies cleanly, once.
        monkeypatch.setattr(bookmark_apply, "record_source_ledger", real)
        _use_token(environment.client, environment.alice_token)
        assert _apply(environment, environment.alice_job_id).status_code == 200
        assert len(asyncio.run(_ledger(database, environment.alice_id))["occurrences"]) == 6
    finally:
        asyncio.run(database.dispose())


def test_replay_neither_duplicates_nor_tops_up_partial_ledger(
    preview_environment: PreviewEnvironment,
) -> None:
    environment = preview_environment
    database = Database(environment.database_url)
    _use_token(environment.client, environment.alice_token)
    try:
        assert _apply(environment, environment.alice_job_id).status_code == 200
        first = asyncio.run(_ledger(database, environment.alice_id))
        replay = environment.client.post(
            f"/api/bookmark-imports/{environment.alice_job_id}/apply",
            json={"expected_job_version": 4, "expected_decision_version": 1},
            headers=ORIGIN,
        )
        assert replay.status_code == 409

        async def rerun_ledger() -> None:
            async with database.sessions() as session:
                outcome = await record_source_ledger(
                    session,
                    environment.alice_id,
                    environment.alice_run_id,
                    merge_targets={},
                    created_site_ids=set(),
                    skipped_actions=frozenset(),
                    batch_size=2,
                )
                await session.commit()
            assert outcome.occurrence_count == 6

        asyncio.run(rerun_ledger())
        second = asyncio.run(_ledger(database, environment.alice_id))
        assert sorted(second["occurrences"]) == sorted(first["occurrences"])
        assert sorted(second["folders"]) == sorted(first["folders"])
        assert sorted(second["origins"]) == sorted(first["origins"])

        async def damage_and_rerun() -> None:
            async with database.sessions() as session:
                victim = first["occurrences"][-1].id
                await session.execute(
                    delete(BookmarkSourceOccurrence).where(
                        BookmarkSourceOccurrence.user_id == environment.alice_id,
                        BookmarkSourceOccurrence.id == victim,
                    )
                )
                await session.commit()
            async with database.sessions() as session:
                with pytest.raises(SourceLedgerError):
                    await record_source_ledger(
                        session,
                        environment.alice_id,
                        environment.alice_run_id,
                        merge_targets={},
                        created_site_ids=set(),
                        skipped_actions=frozenset(),
                        batch_size=200,
                    )
                await session.rollback()

        asyncio.run(damage_and_rerun())
    finally:
        asyncio.run(database.dispose())


def test_ledger_and_origin_view_are_account_isolated(
    preview_environment: PreviewEnvironment,
) -> None:
    environment = preview_environment
    client = environment.client
    database = Database(environment.database_url)
    try:
        _use_token(client, environment.alice_token)
        assert _apply(environment, environment.alice_job_id).status_code == 200
        alice_site = asyncio.run(_site_id(database, environment.alice_id, SHARED_URL))

        # Bob imports the very same URLs: his ledger links only his own Sites.
        _use_token(client, environment.bob_token)
        assert _apply(environment, environment.bob_job_id).status_code == 200
        assert client.get(f"/api/library/sites/{alice_site}/import-origins").status_code == 404
        bob_ledger = asyncio.run(_ledger(database, environment.bob_id))
        assert len(bob_ledger["occurrences"]) == 6
        bob_site = asyncio.run(_site_id(database, environment.bob_id, SHARED_URL))
        assert {row.site_id for row in bob_ledger["origins"]} >= {bob_site}
        assert alice_site not in {row.site_id for row in bob_ledger["origins"]}
        bob_shared = [row for row in bob_ledger["origins"] if row.site_id == bob_site]
        assert {row.link_action for row in bob_shared} == {"created_site"}

        alice_ledger = asyncio.run(_ledger(database, environment.alice_id))
        alice_occurrence_ids = {row.id for row in alice_ledger["occurrences"]}
        assert not alice_occurrence_ids & {row.id for row in bob_ledger["occurrences"]}

        _use_token(client, environment.alice_token)
        view = client.get(f"/api/library/sites/{alice_site}/import-origins").json()
        assert view["occurrence_count"] == 2
        assert client.get("/api/library/sites/missing/import-origins").status_code == 404
    finally:
        asyncio.run(database.dispose())


def test_manual_site_has_an_empty_origin_view(preview_environment: PreviewEnvironment) -> None:
    environment = preview_environment
    database = Database(environment.database_url)
    _use_token(environment.client, environment.alice_token)
    try:
        site_id = asyncio.run(_site_id(database, environment.alice_id, SHARED_URL))
        view = environment.client.get(f"/api/library/sites/{site_id}/import-origins")
        assert view.status_code == 200
        assert view.json() == {
            "site_id": site_id,
            "occurrence_count": 0,
            "folder_paths": [],
            "truncated": False,
        }
    finally:
        asyncio.run(database.dispose())

