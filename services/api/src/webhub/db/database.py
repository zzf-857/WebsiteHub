from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from webhub.db.urls import ensure_sqlite_database_parent

DATABASE_SCHEMA_HEADS = frozenset({"52c3f6173b38"})


class DatabaseSchemaError(RuntimeError):
    pass


class Database:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        ensure_sqlite_database_parent(database_url)
        engine_options: dict[str, object] = {"pool_pre_ping": True}
        if database_url.endswith(":memory:"):
            engine_options["poolclass"] = StaticPool
        self.engine: AsyncEngine = create_async_engine(database_url, **engine_options)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        if database_url.startswith("sqlite"):
            self._configure_sqlite()

    def _configure_sqlite(self) -> None:
        @event.listens_for(self.engine.sync_engine, "connect")
        def set_sqlite_pragmas(dbapi_connection: object, _: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA busy_timeout = 5000")
            if not self.database_url.endswith(":memory:"):
                cursor.execute("PRAGMA journal_mode = WAL")
                cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.close()

    async def check(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            await self._assert_schema_current(connection)

    async def assert_schema_current(self) -> None:
        async with self.engine.connect() as connection:
            await self._assert_schema_current(connection)

    @staticmethod
    async def _assert_schema_current(connection: AsyncConnection) -> None:
        try:
            versions = set(
                (await connection.execute(text("SELECT version_num FROM alembic_version")))
                .scalars()
                .all()
            )
        except SQLAlchemyError as error:
            raise DatabaseSchemaError(
                "Database schema is not initialized; run `webhub-db upgrade` first."
            ) from error
        if versions != DATABASE_SCHEMA_HEADS:
            expected = ", ".join(sorted(DATABASE_SCHEMA_HEADS))
            actual = ", ".join(sorted(versions)) or "none"
            raise DatabaseSchemaError(
                f"Database schema is not current (expected {expected}, found {actual}); "
                "run `webhub-db upgrade`."
            )

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
