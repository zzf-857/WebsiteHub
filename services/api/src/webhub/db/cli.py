from __future__ import annotations

import argparse
from collections.abc import Sequence

from alembic import command
from webhub.config import get_settings
from webhub.db.migrations import create_alembic_config, upgrade_database
from webhub.db.preflight import MigrationPreflightError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the WebHub database schema.")
    parser.add_argument("command", choices=("upgrade", "current", "check"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    database_url = get_settings().database_url
    if arguments.command == "upgrade":
        try:
            upgrade_database(database_url)
        except MigrationPreflightError as error:
            parser.exit(status=2, message=f"error: {error}\n")
    elif arguments.command == "current":
        command.current(create_alembic_config(database_url), check_heads=True)
    else:
        command.check(create_alembic_config(database_url))
    return 0
