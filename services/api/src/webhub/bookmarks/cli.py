from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from webhub.bookmarks.models import BookmarkFormatError, ParserLimits
from webhub.bookmarks.preview import build_import_preview


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a disk-backed WebHub import preview from browser bookmarks."
    )
    parser.add_argument("source", type=Path, help="Netscape Bookmark HTML export")
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="A new directory for preview artifacts; existing directories are refused",
    )
    parser.add_argument("--max-file-mib", type=int, default=512)
    parser.add_argument("--max-bookmarks", type=int, default=500_000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    limits = ParserLimits(
        max_file_bytes=args.max_file_mib * 1024 * 1024,
        max_bookmarks=args.max_bookmarks,
    )
    try:
        preview = build_import_preview(
            args.source.resolve(strict=True),
            args.output_dir.resolve(),
            limits=limits,
        )
    except (BookmarkFormatError, FileExistsError, FileNotFoundError, OSError) as exc:
        _parser().error(str(exc))
    print(json.dumps(preview.summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
