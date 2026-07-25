import asyncio
from pathlib import Path

from sqlalchemy import text

from webhub.db.database import Database
from webhub.db.migrations import upgrade_database


def test_sqlite_connections_enable_required_pragmas(tmp_path: Path) -> None:
    database_path = tmp_path / "pragmas.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    upgrade_database(database_url)
    database = Database(database_url)

    async def read_pragmas() -> tuple[int, int, str]:
        async with database.engine.connect() as connection:
            foreign_keys = await connection.scalar(text("PRAGMA foreign_keys"))
            busy_timeout = await connection.scalar(text("PRAGMA busy_timeout"))
            journal_mode = await connection.scalar(text("PRAGMA journal_mode"))
        await database.dispose()
        return int(foreign_keys), int(busy_timeout), str(journal_mode)

    assert asyncio.run(read_pragmas()) == (1, 5000, "wal")
