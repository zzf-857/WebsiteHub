from pathlib import Path

import pytest

from webhub.bookmarks.models import (
    BookmarkFormatError,
    ParsedBookmark,
    ParsedFolder,
    ParserLimits,
    ParserStats,
)
from webhub.bookmarks.parser import iter_netscape_events


def _write_export(path: Path, body: str) -> Path:
    path.write_text(
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>\n"
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">\n'
        "<DL><p>\n"
        f"{body}\n"
        "</DL><p>\n",
        encoding="utf-8",
    )
    return path


def test_parser_streams_nested_folders_and_ignores_inline_icons(tmp_path: Path) -> None:
    source = _write_export(
        tmp_path / "bookmarks.html",
        """
        <DT><H3>书签栏</H3>
        <DL><p>
          <DT><H3>开发 &amp; 文档</H3>
          <DL><p>
            <DT><A HREF="https://example.com/docs?a=1&amp;b=2#part"
              ADD_DATE="123" ICON_URI="data:image/png;base64,AAAA">示例 &amp; API</A>
          </DL><p>
        </DL><p>
        """,
    )

    stats = ParserStats()
    events = list(iter_netscape_events(source, chunk_size=1, stats=stats))
    folders = [event for event in events if isinstance(event, ParsedFolder)]
    bookmarks = [event for event in events if isinstance(event, ParsedBookmark)]

    assert [folder.folder_path for folder in folders] == [
        ("书签栏",),
        ("书签栏", "开发 & 文档"),
    ]
    assert [folder.source_order for folder in folders] == [1, 2]
    assert [event.source_sequence for event in events] == [1, 2, 3]
    assert len(bookmarks) == 1
    assert bookmarks[0].folder_path == ("书签栏", "开发 & 文档")
    assert bookmarks[0].source_folder_id == folders[1].source_folder_id
    assert bookmarks[0].raw_url == "https://example.com/docs?a=1&b=2#part"
    assert bookmarks[0].title == "示例 & API"
    assert bookmarks[0].add_date == 123
    assert stats.inline_icon_count == 1


def test_source_sequence_is_global_and_independent_of_chunk_boundaries(tmp_path: Path) -> None:
    source = _write_export(
        tmp_path / "source-order.html",
        """
        <DT><H3>Root</H3><DL><p>
          <DT><A HREF="https://root.example/one">Root one</A>
          <DT><H3>Nested</H3><DL><p>
            <DT><A HREF="https://nested.example">Nested bookmark</A>
          </DL><p>
          <DT><A HREF="https://root.example/two">Root two</A>
        </DL><p>
        """,
    )

    expected = list(iter_netscape_events(source, chunk_size=source.stat().st_size + 1))

    assert [type(event) for event in expected] == [
        ParsedFolder,
        ParsedBookmark,
        ParsedFolder,
        ParsedBookmark,
        ParsedBookmark,
    ]
    assert [event.source_sequence for event in expected] == [1, 2, 3, 4, 5]
    assert [event.position for event in expected if isinstance(event, ParsedBookmark)] == [1, 2, 3]
    assert [event.source_order for event in expected if isinstance(event, ParsedFolder)] == [1, 2]

    for chunk_size in (1, 2, 7, 31):
        assert list(iter_netscape_events(source, chunk_size=chunk_size)) == expected


def test_parser_assigns_distinct_ids_to_duplicate_folder_paths(tmp_path: Path) -> None:
    source = _write_export(
        tmp_path / "duplicates.html",
        """
        <DT><H3>Root</H3><DL><p>
          <DT><H3>Same</H3><DL><p>
            <DT><A HREF="https://one.example">One</A>
          </DL><p>
          <DT><H3>Same</H3><DL><p>
            <DT><A HREF="https://two.example">Two</A>
          </DL><p>
        </DL><p>
        """,
    )

    bookmarks = [
        event
        for event in iter_netscape_events(source, chunk_size=7)
        if isinstance(event, ParsedBookmark)
    ]

    assert bookmarks[0].folder_path == bookmarks[1].folder_path == ("Root", "Same")
    assert bookmarks[0].source_folder_id != bookmarks[1].source_folder_id


def test_parser_rejects_wrong_format_and_resource_overflow(tmp_path: Path) -> None:
    wrong = tmp_path / "wrong.html"
    wrong.write_text("<html><a href='https://example.com'>x</a></html>", encoding="utf-8")
    with pytest.raises(BookmarkFormatError, match="Netscape"):
        list(iter_netscape_events(wrong))

    export = _write_export(
        tmp_path / "too-many.html",
        '<DT><A HREF="https://one.example">One</A>\n<DT><A HREF="https://two.example">Two</A>',
    )
    with pytest.raises(BookmarkFormatError, match="count exceeds 1"):
        list(iter_netscape_events(export, limits=ParserLimits(max_bookmarks=1)))

    oversized_tag = _write_export(
        tmp_path / "oversized-tag.html",
        '<DT><A HREF="https://example.com" ICON="data:image/png;base64,'
        + ("A" * 200)
        + '">Example</A>',
    )
    with pytest.raises(BookmarkFormatError, match="buffered-tag"):
        list(
            iter_netscape_events(
                oversized_tag,
                limits=ParserLimits(max_buffered_tag_chars=64),
                chunk_size=7,
            )
        )
