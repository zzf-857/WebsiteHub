import json
import sqlite3
from pathlib import Path

import pytest

from webhub.bookmarks.preview import build_import_preview


def _json_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _sample_export(path: Path) -> Path:
    path.write_text(
        """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<DL><p>
  <DT><H3>Bookmarks bar</H3><DL><p>
    <DT><H3>Same</H3><DL><p>
      <DT><A HREF="HTTPS://Example.COM:443/docs#one"
        ICON="data:image/png;base64,AAAA">Docs</A>
      <DT><A HREF="https://example.com/docs#one">Longer docs title</A>
    </DL><p>
    <DT><H3>Same</H3><DL><p>
      <DT><A HREF="https://example.com/docs#one">Docs copy</A>
      <DT><A HREF="https://example.com/docs#two">Docs section two</A>
    </DL><p>
    <DT><A HREF="http://localhost:8080/admin">Local admin</A>
    <DT><A HREF="file:///C:/private.txt">Private file</A>
  </DL><p>
</DL><p>
""",
        encoding="utf-8",
    )
    return path


def test_preview_is_disk_backed_and_preserves_occurrences(tmp_path: Path) -> None:
    source = _sample_export(tmp_path / "bookmarks.html")
    preview = build_import_preview(source, tmp_path / "preview")
    counts = preview.summary["counts"]

    assert counts == {
        "parsed_bookmarks": 6,
        "parsed_folders": 3,
        "max_folder_depth": 2,
        "accepted_occurrences": 5,
        "unique_candidates": 3,
        "duplicate_occurrences": 2,
        "candidate_source_relations": 4,
        "classification_clusters": 3,
        "rejected": 1,
        "invalid": 0,
        "unsupported": 1,
        "metadata_from_export_only": 1,
        "sensitive_url_candidates": 0,
        "public_fetch_requires_dns_revalidation": 2,
    }
    assert preview.summary["pipeline_versions"] == {
        "parser": "netscape-html.v2",
        "normalizer": "conservative-url.v1",
        "classification_rules": "bookmark-category-rules.v3",
        "sensitive_url_rules": "sensitive-url-keys.v2",
    }
    assert preview.summary["suspected_duplicates"] == {
        "fragment_variant_groups": 1,
        "fragment_variant_candidates": 2,
        "action": "review_only_never_auto_merge",
    }

    candidates = _json_lines(preview.candidates_path)
    occurrences = _json_lines(preview.occurrences_path)
    folders = _json_lines(preview.source_folders_path)
    rejected = _json_lines(preview.rejected_path)
    assert len(candidates) == 3
    assert len(occurrences) == 6
    assert len(folders) == 3
    assert [item["position"] for item in occurrences] == [1, 2, 3, 4, 5, 6]
    assert [item["source_sequence"] for item in occurrences] == [3, 4, 6, 7, 8, 9]
    assert [item["source_sequence"] for item in folders] == [1, 2, 5]
    assert rejected[0]["reason"] == "unsupported_scheme:file"
    assert {item["normalized_url"] for item in candidates} >= {
        "https://example.com/docs#one",
        "https://example.com/docs#two",
    }
    assert "data:image" not in preview.candidates_path.read_text(encoding="utf-8")
    assert preview.summary["ignored_payload"]["inline_favicon_count"] == 1

    with sqlite3.connect(preview.staging_database_path) as connection:
        folder_ids = connection.execute(
            "SELECT source_folder_id FROM source_folders WHERE title = 'Same'"
        ).fetchall()
        staged_occurrences = connection.execute(
            "SELECT position, source_sequence FROM occurrences ORDER BY position"
        ).fetchall()
        staged_folders = connection.execute(
            "SELECT source_order, source_sequence FROM source_folders ORDER BY source_order"
        ).fetchall()
    assert len(folder_ids) == 2
    assert folder_ids[0] != folder_ids[1]
    assert staged_occurrences == [(1, 3), (2, 4), (3, 6), (4, 7), (5, 8), (6, 9)]
    assert staged_folders == [(1, 1), (2, 2), (3, 5)]


def test_preview_refuses_to_overwrite_an_existing_directory(tmp_path: Path) -> None:
    source = _sample_export(tmp_path / "bookmarks.html")
    output = tmp_path / "existing"
    output.mkdir()

    with pytest.raises(FileExistsError):
        build_import_preview(source, output)


def test_classification_projection_excludes_urls_secrets_and_local_targets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "privacy.html"
    source.write_text(
        """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<DL><p>
  <DT><H3>Bookmarks bar</H3><DL><p>
    <DT><H3>Docs https://folder.example/private?token=folder-secret</H3><DL><p>
      <DT><A HREF="https://public.example/one?q=ordinary">https://title.example/a?q=title-secret</A>
      <DT><A HREF="https://public.example/two">Useful docs https://embedded.example/b?q=hidden</A>
      <DT><A HREF="https://public.example/three">Safe title</A>
      <DT><A HREF="https://secret.example/?token=url-secret">Secret candidate</A>
      <DT><A HREF="http://localhost:8080/admin">Local admin</A>
    </DL><p>
  </DL><p>
</DL><p>
""",
        encoding="utf-8",
    )

    preview = build_import_preview(source, tmp_path / "privacy-preview")
    clusters = _json_lines(preview.classification_clusters_path)
    assert len(clusters) == 1
    cluster = clusters[0]

    assert cluster["folder_path"] == ["Docs"]
    assert cluster["link_count"] == 5
    assert cluster["agent_eligible_link_count"] == 3
    assert cluster["sensitive_link_count"] == 1
    assert cluster["metadata_only_link_count"] == 1
    assert cluster["sample_titles"] == ["Useful docs", "Safe title"]
    assert cluster["sample_hosts"] == ["public.example"]

    model_projection = preview.classification_clusters_path.read_text(encoding="utf-8")
    for forbidden in (
        "http://",
        "https://",
        "title-secret",
        "folder-secret",
        "url-secret",
        "localhost",
    ):
        assert forbidden not in model_projection

    candidates = preview.candidates_path.read_text(encoding="utf-8")
    assert "https://title.example/a?q=title-secret" in candidates


def test_zero_eligible_cluster_is_not_sent_for_agent_review(tmp_path: Path) -> None:
    source = tmp_path / "private-only.html"
    source.write_text(
        """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<DL><p>
  <DT><H3>Bookmarks bar</H3><DL><p>
    <DT><H3>Private network</H3><DL><p>
      <DT><A HREF="http://localhost:9000/?token=secret">Local secret</A>
    </DL><p>
  </DL><p>
</DL><p>
""",
        encoding="utf-8",
    )

    preview = build_import_preview(source, tmp_path / "private-only-preview")
    cluster = _json_lines(preview.classification_clusters_path)[0]

    assert cluster["agent_eligible_link_count"] == 0
    assert cluster["folder_path"] == []
    assert cluster["sample_titles"] == []
    assert cluster["sample_hosts"] == []
    assert cluster["needs_agent_review"] is False
