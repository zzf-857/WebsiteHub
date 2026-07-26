"""Local account maintenance for development machines.

This CLI exists so a developer (or an Agent working in the repo) can seed and
repair the local fixture account without hand-rolling password hashing or
poking at SQLite directly.  It is a **local** tool: it reads
``get_settings().database_url``, which points at ``.data/main.sqlite3`` unless
``WEBHUB_DATABASE_URL`` says otherwise.

Passwords never arrive as command-line arguments — those land in shell history
and in ``ps`` output.  Either the prompt or ``--password-stdin`` is used.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Sequence

from webhub.auth.service import (
    UsernameTakenError,
    UserNotFoundError,
    register_user,
    reset_password,
)
from webhub.config import get_settings
from webhub.db.database import Database

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
# A seeded fixture account never needs a long-lived session; register_user
# issues one regardless, so keep it short rather than leaving a 30-day token.
_SEED_SESSION_TTL_SECONDS = 60


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage local WebHub accounts.")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser(
        "create",
        help="Create one local account (fixture/admin seeding).",
    )
    create.add_argument("username")
    create.add_argument("--display-name", default=None)
    create.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the password from stdin instead of prompting.",
    )

    reset = commands.add_parser("reset-password", help="Reset one account password locally.")
    reset.add_argument("username")
    reset.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the password from stdin instead of prompting.",
    )
    return parser


def _validated_password(password: str) -> str:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(
            f"Password length must be between {MIN_PASSWORD_LENGTH} "
            f"and {MAX_PASSWORD_LENGTH} characters."
        )
    return password


def _read_new_password(*, from_stdin: bool = False) -> str:
    if from_stdin:
        # Only the first line, and only its trailing newline stripped: a
        # password may legitimately begin or end with spaces.
        return _validated_password(sys.stdin.readline().rstrip("\r\n"))
    password = getpass.getpass("New password: ")
    confirmation = getpass.getpass("Confirm new password: ")
    if password != confirmation:
        raise ValueError("Passwords do not match.")
    return _validated_password(password)


async def create_local_account(
    database_url: str,
    username: str,
    password: str,
    *,
    display_name: str | None = None,
) -> None:
    database = Database(database_url)
    try:
        await database.assert_schema_current()
        async with database.sessions() as session:
            await register_user(
                session,
                username=username,
                password=password,
                display_name=display_name,
                ttl_seconds=_SEED_SESSION_TTL_SECONDS,
            )
    finally:
        await database.dispose()


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
    database_url = get_settings().database_url
    try:
        password = _read_new_password(from_stdin=arguments.password_stdin)
        if arguments.command == "create":
            asyncio.run(
                create_local_account(
                    database_url,
                    arguments.username,
                    password,
                    display_name=arguments.display_name,
                )
            )
        else:
            asyncio.run(reset_local_password(database_url, arguments.username, password))
    except UsernameTakenError:
        print("Account already exists.", file=sys.stderr)
        return 1
    except UserNotFoundError:
        print("Account was not found.", file=sys.stderr)
        return 1
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    if arguments.command == "create":
        print(f"Account {arguments.username!r} created.")
    else:
        print("Password updated and all existing sessions revoked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
