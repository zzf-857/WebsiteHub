from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Sequence

from webhub.auth.service import UserNotFoundError, reset_password
from webhub.config import get_settings
from webhub.db.database import Database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage local WebHub accounts.")
    commands = parser.add_subparsers(dest="command", required=True)
    reset = commands.add_parser("reset-password", help="Reset one account password locally.")
    reset.add_argument("username")
    return parser


def _read_new_password() -> str:
    password = getpass.getpass("New password: ")
    confirmation = getpass.getpass("Confirm new password: ")
    if password != confirmation:
        raise ValueError("Passwords do not match.")
    if not 8 <= len(password) <= 128:
        raise ValueError("Password length must be between 8 and 128 characters.")
    return password


async def reset_local_password(database_url: str, username: str, new_password: str) -> None:
    database = Database(database_url)
    try:
        await database.assert_schema_current()
        async with database.sessions() as session:
            await reset_password(session, username=username, new_password=new_password)
    finally:
        await database.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        new_password = _read_new_password()
        asyncio.run(
            reset_local_password(
                get_settings().database_url,
                arguments.username,
                new_password,
            )
        )
    except (UserNotFoundError, ValueError) as error:
        message = "Account was not found." if isinstance(error, UserNotFoundError) else str(error)
        print(message, file=sys.stderr)
        return 1
    print("Password updated and all existing sessions revoked.")
    return 0
