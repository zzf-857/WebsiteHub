from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from webhub.bookmarks.similarity import (
    ExistingHomepage,
    SimilarityCandidate,
    detect_similarity_clusters,
    rebuild_similarity_projections,
    safe_display_url,
    site_key_for_url,
)
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database


def _candidate(
    identifier: str,
    url: str,
    sequence: int,
    *,
    title: str | None = None,
    fetch_policy: str = "public_revalidation_required",
    sensitive: bool = False,
    action: str = "create",
    occurrences: int = 1,
) -> SimilarityCandidate:
    host = urlsplit(url).hostname
    assert host is not None
    return SimilarityCandidate(
        id=identifier,
        identity_url=url,
        title=title or identifier,
        host=host,
        fetch_policy=fetch_policy,
        has_sensitive_url=sensitive,
        proposed_action=action,
        occurrence_count=occurrences,
        first_source_sequence=sequence,
    )


def test_site_key_folds_only_www_and_preserves_nondefault_ports() -> None:
    assert site_key_for_url("https://WWW.Example.COM./docs") == "example.com"
    assert site_key_for_url("http://example.com/docs") == "example.com"
    assert site_key_for_url("https://example.com:8443/docs") == "example.com:8443"
    assert site_key_for_url("https://docs.example.com/docs") == "docs.example.com"
    assert site_key_for_url("https://api.example.com/docs") == "api.example.com"


def test_query_and_fragment_variants_are_reviewed_without_losing_members() -> None:
    projections = detect_similarity_clusters(
        (
            _candidate("query", "https://example.com/docs?view=grid", 1),
            _candidate("fragment", "https://example.com/docs#api", 2),
        )
    )

    assert len(projections) == 1
    projection = projections[0]
    assert projection.site_key == "example.com"
    assert projection.canonical_url == "https://example.com/"
    assert projection.canonical_candidate_id is None
    assert projection.canonical_source == "derived_origin_root"
    assert projection.confidence == "low"
    assert projection.reason_codes == (
        "same_site_authority",
        "shared_path_variants",
        "derived_origin_root",
    )
    assert [member.id for member in projection.members] == ["query", "fragment"]


def test_http_https_and_www_variants_choose_an_imported_https_homepage() -> None:
    projections = detect_similarity_clusters(
        (
            _candidate("http-www", "http://www.example.com/", 1),
            _candidate("https-root", "https://example.com/", 2),
            _candidate("https-docs", "https://example.com/docs", 3),
        )
    )

    assert len(projections) == 1
    projection = projections[0]
    assert projection.site_key == "example.com"
    assert projection.canonical_candidate_id == "https-root"
    assert projection.canonical_url == "https://example.com/"
    assert projection.canonical_source == "imported_homepage"
    assert projection.confidence == "high"
    assert {"www_alias", "http_https_variants", "homepage_and_subpages"}.issubset(
        projection.reason_codes
    )


def test_nondefault_ports_form_separate_clusters() -> None:
    projections = detect_similarity_clusters(
        (
            _candidate("root", "https://example.com/", 1),
            _candidate("docs", "https://example.com/docs", 2),
            _candidate("admin-root", "https://example.com:8443/", 3),
            _candidate("admin-docs", "https://example.com:8443/docs", 4),
        )
    )

    assert [projection.site_key for projection in projections] == [
        "example.com",
        "example.com:8443",
    ]
    assert [projection.candidate_count for projection in projections] == [2, 2]


def test_shared_content_platforms_are_not_suggested_as_one_website() -> None:
    projections = detect_similarity_clusters(
        (
            _candidate("repo-one", "https://github.com/acme/one", 1),
            _candidate("repo-two", "https://www.github.com/other/two", 2),
            _candidate("site-root", "https://example.com/", 3),
            _candidate("site-docs", "https://example.com/docs", 4),
        )
    )

    assert len(projections) == 1
    assert projections[0].site_key == "example.com"
    assert {member.id for member in projections[0].members} == {"site-root", "site-docs"}


def test_clean_common_path_ancestor_is_preferred_over_a_derived_root() -> None:
    projections = detect_similarity_clusters(
        (
            _candidate("guide", "https://docs.example.com/guide", 1, title="Guide"),
            _candidate("start", "https://docs.example.com/guide/start", 2),
            _candidate("api", "https://docs.example.com/guide/api", 3),
        )
    )

    projection = projections[0]
    assert projection.canonical_candidate_id == "guide"
    assert projection.canonical_url == "https://docs.example.com/guide"
    assert projection.canonical_title == "Guide"
    assert projection.canonical_source == "imported_homepage"
    assert projection.confidence == "medium"
    assert "common_path_ancestor" in projection.reason_codes


def test_existing_library_homepage_wins_without_creating_another_site() -> None:
    projections = detect_similarity_clusters(
        (
            _candidate("docs", "https://example.com/docs", 1),
            _candidate("pricing", "https://example.com/pricing", 2),
        ),
        existing_homepages={
            "example.com": ExistingHomepage("https://example.com/", "Existing homepage")
        },
    )

    projection = projections[0]
    assert projection.canonical_candidate_id is None
    assert projection.canonical_url == "https://example.com/"
    assert projection.canonical_title == "Existing homepage"
    assert projection.canonical_source == "existing_library"
    assert projection.confidence == "high"
    assert projection.keep_original_create_count == 2
    assert projection.merge_create_count == 0


def test_ineligible_or_sensitive_candidates_never_enter_a_cluster() -> None:
    projections = detect_similarity_clusters(
        (
            _candidate("safe", "https://example.com/", 1),
            _candidate("secret", "https://example.com/private?token=secret", 2, sensitive=True),
            _candidate(
                "local",
                "http://localhost:3000/admin",
                3,
                fetch_policy="export_metadata_only",
            ),
            _candidate("review", "https://example.com/review", 4, action="needs_review"),
        )
    )

    assert projections == ()


def test_safe_display_url_redacts_sensitive_query_and_fragment_values() -> None:
    displayed = safe_display_url(
        "https://example.com/path?token=query-secret&view=grid#session_id=fragment-secret"
    )

    assert "query-secret" not in displayed
    assert "fragment-secret" not in displayed
    assert "view=grid" in displayed
    assert "token=%5Bhidden%5D" in displayed
    assert "session_id=%5Bhidden%5D" in displayed


def test_rebuild_similarity_projections_is_stable_and_persists_decision_state(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "similarity.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    upgrade_database(database_url)
    timestamp = "2026-07-31 00:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            f"""
            INSERT INTO users(
                id, username, display_name, password_hash, is_active, created_at, updated_at
            ) VALUES ('user', 'similarity-user', 'Similarity User', 'hash', 1,
                      '{timestamp}', '{timestamp}');
            INSERT INTO bookmark_import_snapshots(
                id, user_id, source_sha256, source_size_bytes, source_format, storage_key,
                request_idempotency_key_hash, created_at
            ) VALUES ('snapshot', 'user', printf('%064d', 1), 100, 'netscape_html',
                      'bookmark-imports/user/snapshot/source.html', printf('%064d', 2),
                      '{timestamp}');
            INSERT INTO bookmark_import_jobs(
                id, user_id, snapshot_id, state, parser_version, normalizer_version,
                skill_version, version, preview_version, progress_completed, progress_total,
                classification_budget, classification_used, created_at, updated_at
            ) VALUES ('job', 'user', 'snapshot', 'parsing', 'parser', 'normalizer', 'skill',
                      1, 0, 2, 2, 0, 0, '{timestamp}', '{timestamp}');
            INSERT INTO bookmark_import_runs(
                id, user_id, job_id, attempt_number, state, run_idempotency_key_hash,
                input_hash, completion_hash, parser_version, normalizer_version,
                source_sequence_count, folder_count, occurrence_count, candidate_count, created_at
            ) VALUES ('run', 'user', 'job', 1, 'running', printf('%064d', 3),
                      printf('%064d', 4), NULL, 'parser', 'normalizer',
                      2, 0, 2, 2, '{timestamp}');
            INSERT INTO bookmark_staging_candidates(
                id, user_id, run_id, identity_url, identity_hash, display_title, host,
                fetch_policy, has_sensitive_url, proposed_action, occurrence_count,
                first_source_sequence, created_at
            ) VALUES
                ('root', 'user', 'run', 'https://example.com/', printf('%064d', 6),
                 'Example', 'example.com', 'public_revalidation_required', 0, 'create', 1, 1,
                 '{timestamp}'),
                ('docs', 'user', 'run', 'https://example.com/docs', printf('%064d', 7),
                 'Example docs', 'example.com', 'public_revalidation_required', 0, 'create',
                 1, 2, '{timestamp}');
            UPDATE bookmark_import_runs
            SET state = 'finalizing', completion_hash = printf('%064d', 5)
            WHERE id = 'run';
            """
        )
        connection.commit()

    database = Database(database_url)

    async def rebuild() -> int:
        async with database.sessions() as session:
            count = await rebuild_similarity_projections(session, "user", "job", "run")
            await session.commit()
            return count

    try:
        assert asyncio.run(rebuild()) == 1
        with sqlite3.connect(database_path) as connection:
            first_cluster = connection.execute(
                "SELECT id, site_key, canonical_candidate_id, canonical_url, confidence, "
                "candidate_count, occurrence_count FROM bookmark_similarity_clusters"
            ).fetchone()
            members = connection.execute(
                "SELECT candidate_id, is_canonical FROM bookmark_similarity_cluster_members "
                "ORDER BY first_source_sequence"
            ).fetchall()
            decision_state = connection.execute(
                "SELECT job_id, version FROM bookmark_similarity_decision_states"
            ).fetchone()

        assert first_cluster is not None
        assert first_cluster[1:] == (
            "example.com",
            "root",
            "https://example.com/",
            "high",
            2,
            2,
        )
        assert members == [("root", 1), ("docs", 0)]
        assert decision_state == ("job", 1)

        assert asyncio.run(rebuild()) == 1
        with sqlite3.connect(database_path) as connection:
            rebuilt_cluster = connection.execute(
                "SELECT id, site_key, canonical_candidate_id, canonical_url, confidence, "
                "candidate_count, occurrence_count FROM bookmark_similarity_clusters"
            ).fetchone()
            assert connection.execute(
                "SELECT count(*) FROM bookmark_similarity_cluster_members"
            ).fetchone() == (2,)
            assert connection.execute(
                "SELECT count(*) FROM bookmark_similarity_decision_states"
            ).fetchone() == (1,)
        assert rebuilt_cluster == first_cluster
    finally:
        asyncio.run(database.dispose())
