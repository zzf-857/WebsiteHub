"""Background polling: every few minutes, refresh the feeds that are due.

Each feed carries its own ``next_fetch_at`` (60 minutes after a success,
exponential backoff after failures), so the loop only asks the database what
is due.  Expired stream items are cleaned up on the same tick.  One loop per
application; tests drive ``tick`` directly instead of starting it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from webhub.db.database import Database

from . import service

TICK_SECONDS = 5 * 60
INITIAL_DELAY_SECONDS = 30
_LOGGER = logging.getLogger(__name__)


async def tick(database: Database) -> None:
    await service.poll_due_feeds(database)
    await service.cleanup_items(database)


async def _loop(database: Database) -> None:
    await asyncio.sleep(INITIAL_DELAY_SECONDS)
    while True:
        try:
            await tick(database)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad tick must not stop polling
            _LOGGER.exception("feed polling tick failed")
        await asyncio.sleep(TICK_SECONDS)


class FeedPoller:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(_loop(self._database), name="webhub-feed-poller")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
