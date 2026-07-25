from __future__ import annotations

import argparse
from collections.abc import Sequence

from alembic import command
from webhub.config import get_settings
from webhub.db.migrations import create_alembic_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the WebHub database schema.")
    parser.add_argument("command", choices=("upgrade", "current", "check"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    config = create_alembic_config(get_settings().database_url)
    if arguments.command == "upgrade":
        command.upgrade(config, "head")
    elif arguments.command == "current":
        command.current(config, check_heads=True)
    else:
        command.check(config)
    return 0
