"""Q45: GitHub enrichment inside the site analysis flow."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.ingestion import service as ingestion_service
from webhub.ingestion.enrichment import SiteEnrichmentRequest, SiteEnrichmentUnavailableError
from webhub.ingestion.fetcher import FetchOutcome, SiteMetadata
from webhub.ingestion.github import (
    GitHubCredential,
    GitHubFetchResult,
    GitHubRepoRef,
    parse_repository,
)
from webhub.ingestion.worker import AnalysisSchedule
from webhub.main import create_app

ORIGIN = {"Origin": "http://testserver"}
REPO_URL = "https://github.com/langchain-ai/langgraph"
SNAPSHOT = parse_repository(
    {
        "stargazers_count": 52_310,
        "forks_count": 9_000,
        "language": "Python",
        "topics": ["agents"],
        "license": {"spdx_id": "MIT"},
        "pushed_at": "2026-10-20T00:00:00Z",
        "archived": False,
        "description": "Build resilient language agents as graphs.",
    }
)


@dataclass
class FakeRepositoryEnricher:
    results: list[GitHubFetchResult]
    readme: str = ""
    credential_value: GitHubCredential | None = None
    calls: list[dict[str, object]] = field(default_factory=list)
    on_fetch: object = None

    async def credential(self, _session: object, _user_id: str) -> GitHubCredential | None:
        return self.credential_value

    async def fetch(
        self,
        ref: GitHubRepoRef,
        *,
        credential: GitHubCredential | None,
        etag: str | None,
        include_stats: bool,
        include_readme: bool,
    ) -> GitHubFetchResult:
        self.calls.append(
            {
                "ref": ref,
                "credential": credential,
                "etag": etag,
                "include_stats": include_stats,
                "include_readme": include_readme,
            }
        )
        if callable(self.on_fetch):
            self.on_fetch()
        result = self.results.pop(0) if include_stats else GitHubFetchResult("skipped")
        return GitHubFetchResult(
            result.status,
            snapshot=result.snapshot,
            etag=result.etag,
            readme_excerpt=self.readme if include_readme else "",
        )


@contextmanager
def _environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'main.sqlite3').as_posix()}",
        data_directory=tmp_path,
        provider_master_key=b"provider-test-master-key-32bytes",
    )
    upgrade_database(settings.database_url)
    monkeypatch.setattr(
        "webhub.library.routes.ingestion_worker.schedule_analysis",
        lambda *_args, **_kwargs: AnalysisSchedule(queued=0, already_queued=0, rejected=0),
    )

    async def fetched_page(url: str, **_kwargs: object) -> FetchOutcome:
        return FetchOutcome(
            status="complete",
            reason="",
            metadata=SiteMetadata(title="GitHub - langgraph", description="仓库页面描述文字足够长"),
            final_url=url,
        )

    monkeypatch.setattr(ingestion_service, "fetch_site_metadata", fetched_page)
    with TestClient(create_app(settings=settings)) as client:
        registered = client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "a sufficiently secure password"},
            headers=ORIGIN,
        )
        assert registered.status_code == 201
        yield client


def _create(client: TestClient, url: str = REPO_URL) -> dict[str, object]:
    created = client.post(
        "/api/library/sites",
        json={"name": "LangGraph", "url": url},
        headers=ORIGIN,
    )
    assert created.status_code == 201, created.text
    return created.json()


def _row(tmp_path: Path, site_id: object) -> tuple[str, int, str, str]:
    with sqlite3.connect(tmp_path / "main.sqlite3") as connection:
        row = connection.execute(
            "SELECT user_id, version, updated_at, meta_json FROM sites WHERE id = ?",
            (site_id,),
        ).fetchone()
    assert row is not None
    return str(row[0]), int(row[1]), str(row[2]), str(row[3])


def _analyze(
    tmp_path: Path,
    site_id: object,
    repository: FakeRepositoryEnricher,
    **kwargs: object,
) -> FetchOutcome | None:
    user_id = _row(tmp_path, site_id)[0]

    async def scenario() -> FetchOutcome | None:
        database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'main.sqlite3').as_posix()}")
        try:
            async with database.sessions() as session:
                return await ingestion_service.analyze_site(
                    session,
                    user_id,
                    str(site_id),
                    repository_enricher=repository,
                    **kwargs,  # type: ignore[arg-type]
                )
        finally:
            await database.dispose()

    return asyncio.run(scenario())


def test_stats_are_stored_without_touching_version_or_updated_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enricher = FakeRepositoryEnricher([GitHubFetchResult("ok", SNAPSHOT, etag='"v1"')])
    with _environment(tmp_path, monkeypatch) as client:
        site = _create(client)
        _, version_before, updated_before, _ = _row(tmp_path, site["id"])

        outcome = _analyze(tmp_path, site["id"], enricher)

        assert outcome is not None
        assert enricher.calls == [
            {
                "ref": GitHubRepoRef("langchain-ai", "langgraph"),
                "credential": None,
                "etag": None,
                "include_stats": True,
                "include_readme": False,
            }
        ]
        _, version_after, updated_after, raw_meta = _row(tmp_path, site["id"])
        assert (version_after, updated_after) == (version_before, updated_before)
        meta = json.loads(raw_meta)
        # The URL-derived repository keys survive next to the new record.
        assert meta["host"] == "github.com"
        assert meta["owner"] == "langchain-ai"
        assert meta["repo"] == "langgraph"
        assert meta["github"]["stars"] == 52_310
        assert meta["github"]["language"] == "Python"
        assert meta["github"]["etag"] == '"v1"'

        # The API exposes it on the site for covers and the detail page.
        listed = client.get(f"/api/library/sites/{site['id']}")
        assert listed.status_code == 200
        assert listed.json()["meta"]["github"]["stars"] == 52_310

        # A background pass within seven days reuses the stats: no request.
        _analyze(tmp_path, site["id"], enricher, automatic=True)
        assert len(enricher.calls) == 1

        # A person asking again refreshes, conditionally on the stored ETag.
        enricher.results.append(GitHubFetchResult("not_modified", etag='"v1"'))
        _analyze(tmp_path, site["id"], enricher)
        assert enricher.calls[-1]["etag"] == '"v1"'
        assert json.loads(_row(tmp_path, site["id"])[3])["github"]["stars"] == 52_310


def test_rate_limit_marks_the_attempt_and_analysis_still_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enricher = FakeRepositoryEnricher([GitHubFetchResult("rate_limited")])
    with _environment(tmp_path, monkeypatch) as client:
        site = _create(client)

        outcome = _analyze(tmp_path, site["id"], enricher)

        assert outcome is not None
        assert outcome.status == "complete"
        meta = json.loads(_row(tmp_path, site["id"])[3])
        assert set(meta["github"]) == {"rate_limited_at"}
        with sqlite3.connect(tmp_path / "main.sqlite3") as connection:
            status = connection.execute(
                "SELECT analysis_status FROM sites WHERE id = ?", (site["id"],)
            ).fetchone()[0]
        assert status == "complete"


def test_only_github_repositories_are_enriched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enricher = FakeRepositoryEnricher([])
    with _environment(tmp_path, monkeypatch) as client:
        gitlab = _create(client, "https://gitlab.com/group/project")
        issue = _create(client, "https://github.com/langchain-ai/langgraph/issues/1")
        website = _create(client, "https://example.com/")

        for site in (gitlab, issue, website):
            _analyze(tmp_path, site["id"], enricher)

        assert enricher.calls == []


def test_a_url_change_during_the_request_discards_the_stats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _environment(tmp_path, monkeypatch) as client:
        site = _create(client)

        def move_site() -> None:
            with sqlite3.connect(tmp_path / "main.sqlite3") as connection:
                connection.execute(
                    "UPDATE sites SET original_url = ? WHERE id = ?",
                    ("https://github.com/other/repo", site["id"]),
                )

        enricher = FakeRepositoryEnricher(
            [GitHubFetchResult("ok", SNAPSHOT)],
            on_fetch=move_site,
        )
        _analyze(tmp_path, site["id"], enricher)

        assert "github" not in json.loads(_row(tmp_path, site["id"])[3])


def test_llm_analysis_receives_description_and_readme_as_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[SiteEnrichmentRequest] = []

    class CapturingEnricher:
        async def enrich(self, request: SiteEnrichmentRequest) -> object:
            captured.append(request)
            raise SiteEnrichmentUnavailableError("模型未返回可用结果")

    enricher = FakeRepositoryEnricher(
        [GitHubFetchResult("ok", SNAPSHOT)],
        readme="# LangGraph\nLow-level orchestration for long-running agents.",
        credential_value=GitHubCredential(config_id="config", token="secret-token"),
    )
    with _environment(tmp_path, monkeypatch) as client:
        site = _create(client)

        _analyze(
            tmp_path,
            site["id"],
            enricher,
            use_llm=True,
            enricher=CapturingEnricher(),
        )

        assert enricher.calls[0]["include_readme"] is True
        assert enricher.calls[0]["credential"] == GitHubCredential("config", "secret-token")
        assert len(captured) == 1
        evidence = captured[0].repository_evidence
        assert "GitHub 仓库：langchain-ai/langgraph" in evidence
        assert "仓库简介：Build resilient language agents as graphs." in evidence
        assert "Low-level orchestration for long-running agents." in evidence
        assert "secret-token" not in evidence
        # The README is model evidence only; it is never stored.
        assert "orchestration" not in _row(tmp_path, site["id"])[3]


def test_a_failing_repository_enricher_never_blocks_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenEnricher(FakeRepositoryEnricher):
        async def fetch(self, *_args: object, **_kwargs: object) -> GitHubFetchResult:
            raise RuntimeError("unexpected")

    with _environment(tmp_path, monkeypatch) as client:
        site = _create(client)

        outcome = _analyze(tmp_path, site["id"], BrokenEnricher([]))

        assert outcome is not None
        assert outcome.status == "complete"
        assert "github" not in json.loads(_row(tmp_path, site["id"])[3])
