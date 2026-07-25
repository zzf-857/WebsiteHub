from __future__ import annotations

import json
import sqlite3
import time
import tracemalloc
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from itertools import groupby
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urldefrag

from webhub.bookmarks.classification import (
    CLASSIFICATION_RULESET_VERSION,
    meaningful_folder_path,
    suggest_category,
)
from webhub.bookmarks.models import (
    FetchPolicy,
    ImportPreview,
    NormalizationStatus,
    ParsedBookmark,
    ParsedFolder,
    ParserLimits,
    ParserStats,
)
from webhub.bookmarks.normalization import NORMALIZER_VERSION, normalize_bookmark_url
from webhub.bookmarks.parser import PARSER_VERSION, iter_netscape_events
from webhub.bookmarks.privacy import (
    SENSITIVE_URL_RULESET_VERSION,
    agent_safe_label,
    sensitive_url_keys,
)

_SCHEMA_VERSION = "webhub.bookmark-import-preview.v2"


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE candidates (
            normalized_url TEXT PRIMARY KEY,
            fragmentless_url TEXT NOT NULL,
            original_url TEXT NOT NULL,
            title TEXT NOT NULL,
            host TEXT NOT NULL,
            add_date INTEGER,
            last_modified INTEGER,
            first_position INTEGER NOT NULL,
            occurrence_count INTEGER NOT NULL,
            fetch_policy TEXT NOT NULL,
            suggested_category TEXT NOT NULL,
            classification_confidence TEXT NOT NULL,
            classification_evidence TEXT NOT NULL,
            sensitive_url INTEGER NOT NULL,
            sensitive_keys TEXT NOT NULL,
            primary_folder_path TEXT NOT NULL
        );

        CREATE TABLE source_folders (
            source_folder_id INTEGER PRIMARY KEY,
            parent_source_folder_id INTEGER,
            source_order INTEGER NOT NULL,
            source_sequence INTEGER NOT NULL UNIQUE,
            title TEXT NOT NULL,
            folder_path TEXT NOT NULL,
            depth INTEGER NOT NULL,
            FOREIGN KEY (parent_source_folder_id)
                REFERENCES source_folders(source_folder_id) ON DELETE CASCADE
        );

        CREATE TABLE candidate_sources (
            normalized_url TEXT NOT NULL,
            source_folder_key TEXT NOT NULL,
            source_folder_id INTEGER,
            folder_path TEXT NOT NULL,
            PRIMARY KEY (normalized_url, source_folder_key),
            FOREIGN KEY (normalized_url) REFERENCES candidates(normalized_url) ON DELETE CASCADE,
            FOREIGN KEY (source_folder_id)
                REFERENCES source_folders(source_folder_id) ON DELETE CASCADE
        );

        CREATE TABLE occurrences (
            position INTEGER PRIMARY KEY,
            source_sequence INTEGER NOT NULL UNIQUE,
            original_url TEXT NOT NULL,
            title TEXT NOT NULL,
            normalized_url TEXT,
            source_folder_id INTEGER,
            folder_path TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            add_date INTEGER,
            last_modified INTEGER,
            FOREIGN KEY (normalized_url) REFERENCES candidates(normalized_url) ON DELETE CASCADE,
            FOREIGN KEY (source_folder_id)
                REFERENCES source_folders(source_folder_id) ON DELETE SET NULL
        );

        CREATE INDEX candidate_sources_folder_idx ON candidate_sources(source_folder_key);
        CREATE INDEX candidates_first_position_idx ON candidates(first_position);
        CREATE INDEX candidates_host_idx ON candidates(host);
        CREATE INDEX occurrences_status_idx ON occurrences(status);
        CREATE INDEX occurrences_normalized_url_idx ON occurrences(normalized_url);
        """
    )
    return connection


def _json_line(stream: TextIO, value: dict[str, Any]) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    stream.write("\n")


def _stage_source(
    connection: sqlite3.Connection,
    source_path: Path,
    limits: ParserLimits,
    parser_stats: ParserStats,
) -> tuple[int, Counter[str]]:
    accepted_occurrences = 0
    rejection_reasons: Counter[str] = Counter()
    cursor = connection.cursor()

    staged_event_count = 0
    for event in iter_netscape_events(
        source_path,
        limits=limits,
        stats=parser_stats,
    ):
        staged_event_count += 1
        if isinstance(event, ParsedFolder):
            cursor.execute(
                """
                INSERT INTO source_folders (
                    source_folder_id,
                    parent_source_folder_id,
                    source_order,
                    source_sequence,
                    title,
                    folder_path,
                    depth
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.source_folder_id,
                    event.parent_source_folder_id,
                    event.source_order,
                    event.source_sequence,
                    event.title,
                    json.dumps(event.folder_path, ensure_ascii=False, separators=(",", ":")),
                    event.depth,
                ),
            )
            if staged_event_count % 1_000 == 0:
                connection.commit()
            continue

        assert isinstance(event, ParsedBookmark)
        bookmark = event
        normalized = normalize_bookmark_url(bookmark.raw_url)
        folder_json = json.dumps(bookmark.folder_path, ensure_ascii=False, separators=(",", ":"))
        if bookmark.issues or normalized.status is not NormalizationStatus.ACCEPTED:
            status = NormalizationStatus.INVALID if bookmark.issues else normalized.status
            reason = (
                f"parser:{bookmark.issues[0]}"
                if bookmark.issues
                else normalized.reason or "unknown"
            )
            rejection_reasons[reason] += 1
            cursor.execute(
                """
                INSERT INTO occurrences (
                    position,
                    source_sequence,
                    original_url,
                    title,
                    normalized_url,
                    source_folder_id,
                    folder_path,
                    status,
                    reason,
                    add_date,
                    last_modified
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bookmark.position,
                    bookmark.source_sequence,
                    bookmark.raw_url,
                    bookmark.title,
                    bookmark.source_folder_id,
                    folder_json,
                    status.value,
                    reason,
                    bookmark.add_date,
                    bookmark.last_modified,
                ),
            )
            if staged_event_count % 1_000 == 0:
                connection.commit()
            continue

        assert normalized.normalized_url is not None
        assert normalized.host is not None
        assert normalized.fetch_policy is not None
        accepted_occurrences += 1
        title = bookmark.title or normalized.host
        suggestion = suggest_category(bookmark.folder_path, title, normalized.host)
        sensitive_keys = sensitive_url_keys(normalized.normalized_url)
        cursor.execute(
            """
            INSERT INTO candidates (
                normalized_url,
                fragmentless_url,
                original_url,
                title,
                host,
                add_date,
                last_modified,
                first_position,
                occurrence_count,
                fetch_policy,
                suggested_category,
                classification_confidence,
                classification_evidence,
                sensitive_url,
                sensitive_keys,
                primary_folder_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(normalized_url) DO UPDATE SET
                occurrence_count = candidates.occurrence_count + 1,
                title = CASE
                    WHEN length(excluded.title) > length(candidates.title) THEN excluded.title
                    ELSE candidates.title
                END,
                add_date = CASE
                    WHEN candidates.add_date IS NULL THEN excluded.add_date
                    WHEN excluded.add_date IS NULL THEN candidates.add_date
                    ELSE min(candidates.add_date, excluded.add_date)
                END,
                last_modified = CASE
                    WHEN candidates.last_modified IS NULL THEN excluded.last_modified
                    WHEN excluded.last_modified IS NULL THEN candidates.last_modified
                    ELSE max(candidates.last_modified, excluded.last_modified)
                END
            """,
            (
                normalized.normalized_url,
                urldefrag(normalized.normalized_url).url,
                bookmark.raw_url,
                title,
                normalized.host,
                bookmark.add_date,
                bookmark.last_modified,
                bookmark.position,
                normalized.fetch_policy.value,
                suggestion.category,
                suggestion.confidence,
                json.dumps(suggestion.evidence, ensure_ascii=False, separators=(",", ":")),
                bool(sensitive_keys),
                json.dumps(sensitive_keys, separators=(",", ":")),
                folder_json,
            ),
        )
        cursor.execute(
            """
            INSERT OR IGNORE INTO candidate_sources (
                normalized_url, source_folder_key, source_folder_id, folder_path
            ) VALUES (?, ?, ?, ?)
            """,
            (
                normalized.normalized_url,
                str(bookmark.source_folder_id) if bookmark.source_folder_id is not None else "root",
                bookmark.source_folder_id,
                folder_json,
            ),
        )
        cursor.execute(
            """
            INSERT INTO occurrences (
                position,
                source_sequence,
                original_url,
                title,
                normalized_url,
                source_folder_id,
                folder_path,
                status,
                reason,
                add_date,
                last_modified
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                bookmark.position,
                bookmark.source_sequence,
                bookmark.raw_url,
                bookmark.title,
                normalized.normalized_url,
                bookmark.source_folder_id,
                folder_json,
                NormalizationStatus.ACCEPTED.value,
                bookmark.add_date,
                bookmark.last_modified,
            ),
        )
        if staged_event_count % 1_000 == 0:
            connection.commit()

    connection.commit()
    return accepted_occurrences, rejection_reasons


def _write_candidates(connection: sqlite3.Connection, target: Path) -> None:
    query = """
        SELECT
            normalized_url,
            original_url,
            title,
            host,
            add_date,
            last_modified,
            first_position,
            occurrence_count,
            fetch_policy,
            suggested_category,
            classification_confidence,
            classification_evidence,
            sensitive_url,
            sensitive_keys,
            primary_folder_path
        FROM candidates
        ORDER BY first_position
    """
    with target.open("w", encoding="utf-8", newline="\n") as stream:
        for row in connection.execute(query):
            value = dict(row)
            value["classification_evidence"] = json.loads(value["classification_evidence"])
            value["sensitive_url"] = bool(value["sensitive_url"])
            value["sensitive_keys"] = json.loads(value["sensitive_keys"])
            value["primary_folder_path"] = json.loads(value["primary_folder_path"])
            _json_line(stream, value)


def _write_candidate_sources(connection: sqlite3.Connection, target: Path) -> None:
    query = """
        SELECT
            source.normalized_url,
            source.source_folder_id,
            source.folder_path
        FROM candidate_sources AS source
        JOIN candidates AS candidate USING (normalized_url)
        ORDER BY
            candidate.first_position,
            source.source_folder_id IS NOT NULL,
            source.source_folder_id
    """
    with target.open("w", encoding="utf-8", newline="\n") as stream:
        for row in connection.execute(query):
            _json_line(
                stream,
                {
                    "normalized_url": row["normalized_url"],
                    "source_folder_id": row["source_folder_id"],
                    "folder_path": json.loads(row["folder_path"]),
                },
            )


def _write_occurrences(connection: sqlite3.Connection, target: Path) -> None:
    with target.open("w", encoding="utf-8", newline="\n") as stream:
        for row in connection.execute("SELECT * FROM occurrences ORDER BY source_sequence"):
            value = dict(row)
            value["folder_path"] = json.loads(value["folder_path"])
            _json_line(stream, value)


def _write_source_folders(connection: sqlite3.Connection, target: Path) -> None:
    with target.open("w", encoding="utf-8", newline="\n") as stream:
        for row in connection.execute("SELECT * FROM source_folders ORDER BY source_sequence"):
            value = dict(row)
            value["folder_path"] = json.loads(value["folder_path"])
            _json_line(stream, value)


def _write_rejected(connection: sqlite3.Connection, target: Path) -> None:
    with target.open("w", encoding="utf-8", newline="\n") as stream:
        for row in connection.execute(
            "SELECT * FROM occurrences WHERE status != ? ORDER BY source_sequence",
            (NormalizationStatus.ACCEPTED.value,),
        ):
            value = dict(row)
            value["folder_path"] = json.loads(value["folder_path"])
            _json_line(stream, value)


def _write_classification_clusters(connection: sqlite3.Connection, target: Path) -> int:
    query = """
        SELECT
            source.source_folder_key,
            source.source_folder_id,
            source.folder_path,
            candidate.normalized_url,
            candidate.title,
            candidate.host,
            candidate.sensitive_url,
            candidate.fetch_policy
        FROM candidate_sources AS source
        JOIN candidates AS candidate USING (normalized_url)
        ORDER BY
            source.source_folder_id IS NOT NULL,
            source.source_folder_id,
            candidate.first_position
    """
    cluster_count = 0
    rows = connection.execute(query)
    with target.open("w", encoding="utf-8", newline="\n") as stream:
        for source_folder_key, group in groupby(rows, key=lambda row: row["source_folder_key"]):
            cluster_count += 1
            link_count = 0
            category_votes: Counter[str] = Counter()
            sample_titles: list[str] = []
            sample_hosts: list[str] = []
            sensitive_link_count = 0
            metadata_only_link_count = 0
            agent_eligible_link_count = 0
            source_folder_id: int | None = None
            folder_json = "[]"
            for row in group:
                source_folder_id = row["source_folder_id"]
                folder_json = row["folder_path"]
                link_count += 1
                is_sensitive = bool(row["sensitive_url"])
                is_metadata_only = row["fetch_policy"] == FetchPolicy.EXPORT_METADATA_ONLY.value
                if is_sensitive:
                    sensitive_link_count += 1
                if is_metadata_only:
                    metadata_only_link_count += 1
                folder_path = tuple(json.loads(folder_json))
                cluster_suggestion = suggest_category(
                    folder_path,
                    row["title"],
                    row["host"],
                )
                category_votes[cluster_suggestion.category] += 1
                if is_sensitive or is_metadata_only:
                    continue

                agent_eligible_link_count += 1
                safe_title = agent_safe_label(row["title"])
                if safe_title and safe_title not in sample_titles and len(sample_titles) < 8:
                    sample_titles.append(safe_title)
                if row["host"] not in sample_hosts and len(sample_hosts) < 8:
                    sample_hosts.append(row["host"])

            if category_votes:
                recommendation, votes = category_votes.most_common(1)[0]
            else:
                recommendation, votes = "未分类", 0
            share = votes / max(link_count, 1)
            if recommendation == "未分类":
                confidence = "none"
            elif share >= 0.7:
                confidence = "high"
            elif share >= 0.4:
                confidence = "medium"
            else:
                confidence = "ambiguous"
            safe_folder_path = [
                safe_label
                for label in meaningful_folder_path(tuple(json.loads(folder_json)))
                if (safe_label := agent_safe_label(label)) is not None
            ]
            if agent_eligible_link_count == 0:
                safe_folder_path = []
            _json_line(
                stream,
                {
                    "source_folder_key": source_folder_key,
                    "source_folder_id": source_folder_id,
                    "folder_path": safe_folder_path,
                    "link_count": link_count,
                    "agent_eligible_link_count": agent_eligible_link_count,
                    "sensitive_link_count": sensitive_link_count,
                    "metadata_only_link_count": metadata_only_link_count,
                    "sample_titles": sample_titles,
                    "sample_hosts": sample_hosts,
                    "deterministic_suggestion": recommendation,
                    "suggestion_confidence": confidence,
                    "needs_agent_review": agent_eligible_link_count > 0
                    and confidence in {"none", "ambiguous"},
                },
            )
    return cluster_count


def _scalar(connection: sqlite3.Connection, query: str, parameters: tuple[object, ...] = ()) -> int:
    row = connection.execute(query, parameters).fetchone()
    return int(row[0]) if row else 0


def _summary(
    connection: sqlite3.Connection,
    *,
    source_path: Path,
    limits: ParserLimits,
    parser_stats: ParserStats,
    accepted_occurrences: int,
    rejection_reasons: Counter[str],
    cluster_count: int,
    elapsed_seconds: float,
    peak_python_memory_bytes: int | None,
) -> dict[str, object]:
    unique_candidates = _scalar(connection, "SELECT count(*) FROM candidates")
    rejected_count = _scalar(
        connection,
        "SELECT count(*) FROM occurrences WHERE status != ?",
        (NormalizationStatus.ACCEPTED.value,),
    )
    invalid_count = _scalar(
        connection,
        "SELECT count(*) FROM occurrences WHERE status = ?",
        (NormalizationStatus.INVALID.value,),
    )
    unsupported_count = _scalar(
        connection,
        "SELECT count(*) FROM occurrences WHERE status = ?",
        (NormalizationStatus.UNSUPPORTED.value,),
    )
    metadata_only_count = _scalar(
        connection,
        "SELECT count(*) FROM candidates WHERE fetch_policy = ?",
        (FetchPolicy.EXPORT_METADATA_ONLY.value,),
    )
    source_relation_count = _scalar(connection, "SELECT count(*) FROM candidate_sources")
    sensitive_candidate_count = _scalar(
        connection,
        "SELECT count(*) FROM candidates WHERE sensitive_url = 1",
    )
    fragment_variant_groups = _scalar(
        connection,
        """
        SELECT count(*) FROM (
            SELECT fragmentless_url
            FROM candidates
            GROUP BY fragmentless_url
            HAVING count(*) > 1
        )
        """,
    )
    fragment_variant_candidates = _scalar(
        connection,
        """
        SELECT coalesce(sum(item_count), 0) FROM (
            SELECT count(*) AS item_count
            FROM candidates
            GROUP BY fragmentless_url
            HAVING item_count > 1
        )
        """,
    )
    now_plus_one_day = int(time.time()) + 86_400
    suspicious_timestamp_count = _scalar(
        connection,
        """
        SELECT count(*) FROM candidates
        WHERE add_date > ? OR last_modified > ?
        """,
        (now_plus_one_day, now_plus_one_day),
    )

    top_hosts = [
        {"host": row["host"], "count": row["item_count"]}
        for row in connection.execute(
            """
            SELECT host, count(*) AS item_count
            FROM candidates
            GROUP BY host
            ORDER BY item_count DESC, host
            LIMIT 20
            """
        )
    ]
    suggested_categories = [
        {"category": row["suggested_category"], "count": row["item_count"]}
        for row in connection.execute(
            """
            SELECT suggested_category, count(*) AS item_count
            FROM candidates
            GROUP BY suggested_category
            ORDER BY item_count DESC, suggested_category
            """
        )
    ]

    return {
        "schema_version": _SCHEMA_VERSION,
        "pipeline_versions": {
            "parser": PARSER_VERSION,
            "normalizer": NORMALIZER_VERSION,
            "classification_rules": CLASSIFICATION_RULESET_VERSION,
            "sensitive_url_rules": SENSITIVE_URL_RULESET_VERSION,
        },
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "filename": source_path.name,
            "format": "netscape-bookmark-html",
            "encoding": parser_stats.encoding,
            "size_bytes": parser_stats.source_size_bytes,
            "sha256": parser_stats.source_sha256,
        },
        "counts": {
            "parsed_bookmarks": parser_stats.bookmark_count,
            "parsed_folders": parser_stats.folder_count,
            "max_folder_depth": parser_stats.max_folder_depth,
            "accepted_occurrences": accepted_occurrences,
            "unique_candidates": unique_candidates,
            "duplicate_occurrences": accepted_occurrences - unique_candidates,
            "candidate_source_relations": source_relation_count,
            "classification_clusters": cluster_count,
            "rejected": rejected_count,
            "invalid": invalid_count,
            "unsupported": unsupported_count,
            "metadata_from_export_only": metadata_only_count,
            "sensitive_url_candidates": sensitive_candidate_count,
            "public_fetch_requires_dns_revalidation": unique_candidates - metadata_only_count,
        },
        "ignored_payload": {
            "inline_favicon_count": parser_stats.inline_icon_count,
            "inline_favicon_characters": parser_stats.inline_icon_characters_ignored,
            "inline_favicon_share_of_source": round(
                parser_stats.inline_icon_characters_ignored
                / max(parser_stats.source_size_bytes, 1),
                4,
            ),
        },
        "suspected_duplicates": {
            "fragment_variant_groups": fragment_variant_groups,
            "fragment_variant_candidates": fragment_variant_candidates,
            "action": "review_only_never_auto_merge",
        },
        "quality": {
            "rejection_reasons": dict(sorted(rejection_reasons.items())),
            "invalid_timestamp_count": parser_stats.invalid_timestamp_count,
            "future_timestamp_candidates": suspicious_timestamp_count,
            "parser_warnings": parser_stats.warnings,
            "parser_warning_count": parser_stats.warning_count,
        },
        "suggested_categories": suggested_categories,
        "top_hosts": top_hosts,
        "limits": asdict(limits),
        "performance": {
            "elapsed_seconds": round(elapsed_seconds, 4),
            "peak_python_memory_bytes": peak_python_memory_bytes,
        },
        "output_files": {
            "candidates": "candidates.jsonl",
            "occurrences": "occurrences.jsonl",
            "candidate_sources": "candidate_sources.jsonl",
            "source_folders": "source_folders.jsonl",
            "classification_clusters": "classification_clusters.jsonl",
            "rejected": "rejected.jsonl",
            "staging_database": "staging.sqlite3",
        },
    }


def build_import_preview(
    source_path: Path,
    output_directory: Path,
    *,
    limits: ParserLimits | None = None,
) -> ImportPreview:
    """Build a disk-backed preview without mutating WebHub business data."""
    selected_limits = limits or ParserLimits()
    output_directory.mkdir(parents=True, exist_ok=False)
    staging_path = output_directory / "staging.sqlite3"
    candidates_path = output_directory / "candidates.jsonl"
    occurrences_path = output_directory / "occurrences.jsonl"
    candidate_sources_path = output_directory / "candidate_sources.jsonl"
    source_folders_path = output_directory / "source_folders.jsonl"
    classification_clusters_path = output_directory / "classification_clusters.jsonl"
    rejected_path = output_directory / "rejected.jsonl"
    summary_path = output_directory / "summary.json"

    tracing_started_here = not tracemalloc.is_tracing()
    if tracing_started_here:
        tracemalloc.start()
    started_at = time.perf_counter()
    parser_stats = ParserStats()
    connection = _connect(staging_path)
    try:
        accepted_occurrences, rejection_reasons = _stage_source(
            connection,
            source_path,
            selected_limits,
            parser_stats,
        )
        _write_candidates(connection, candidates_path)
        _write_occurrences(connection, occurrences_path)
        _write_candidate_sources(connection, candidate_sources_path)
        _write_source_folders(connection, source_folders_path)
        _write_rejected(connection, rejected_path)
        cluster_count = _write_classification_clusters(
            connection,
            classification_clusters_path,
        )
        elapsed_seconds = time.perf_counter() - started_at
        peak_memory = tracemalloc.get_traced_memory()[1] if tracing_started_here else None
        summary = _summary(
            connection,
            source_path=source_path,
            limits=selected_limits,
            parser_stats=parser_stats,
            accepted_occurrences=accepted_occurrences,
            rejection_reasons=rejection_reasons,
            cluster_count=cluster_count,
            elapsed_seconds=elapsed_seconds,
            peak_python_memory_bytes=peak_memory,
        )
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    finally:
        connection.close()
        if tracing_started_here:
            tracemalloc.stop()

    return ImportPreview(
        output_directory=output_directory,
        summary_path=summary_path,
        candidates_path=candidates_path,
        occurrences_path=occurrences_path,
        candidate_sources_path=candidate_sources_path,
        source_folders_path=source_folders_path,
        classification_clusters_path=classification_clusters_path,
        rejected_path=rejected_path,
        staging_database_path=staging_path,
        summary=summary,
    )
