import sqlite3
from pathlib import Path

import pytest

from webhub.bookmarks.preview import build_import_preview

_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_PRIVATE_MOCK = _REPOSITORY_ROOT / "MockData" / "bookmarks_2026_7_26.html"
_SOURCE_SHA256 = "c3dc4d28a504d2974a16a1aea7053fdecf83e1871245d68dc9a46583346c2785"
_EXPECTED_COUNTS = {
    "parsed_bookmarks": 2_541,
    "parsed_folders": 368,
    "accepted_occurrences": 2_535,
    "unique_candidates": 2_024,
    "duplicate_occurrences": 511,
    "candidate_source_relations": 2_509,
    "rejected": 6,
    "invalid": 0,
    "unsupported": 6,
}


def test_private_mock_matches_the_redacted_golden_contract(tmp_path: Path) -> None:
    if not _PRIVATE_MOCK.is_file():
        pytest.skip("本机未提供受 .gitignore 保护的书签 mock")

    preview = build_import_preview(_PRIVATE_MOCK, tmp_path / "mock-preview")
    assert preview.summary["schema_version"] == "webhub.bookmark-import-preview.v2"
    assert preview.summary["source"]["sha256"] == _SOURCE_SHA256  # type: ignore[index]
    counts = preview.summary["counts"]
    assert isinstance(counts, dict)
    assert {key: counts[key] for key in _EXPECTED_COUNTS} == _EXPECTED_COUNTS
    assert preview.summary["performance"]["elapsed_seconds"] < 5  # type: ignore[index]

    with sqlite3.connect(preview.staging_database_path) as connection:
        source_sequences = [
            row[0]
            for row in connection.execute(
                "SELECT source_sequence FROM source_folders "
                "UNION ALL SELECT source_sequence FROM occurrences "
                "ORDER BY source_sequence"
            )
        ]

    assert source_sequences == list(range(1, 2_541 + 368 + 1))
