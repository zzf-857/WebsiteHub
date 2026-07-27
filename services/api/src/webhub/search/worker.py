"""Run embedding backfill off the request thread.

Same posture as ``bookmarks/worker.py``: an in-process task, not the leased
worker a multi-node deployment would need.  WebHub runs on the user's own
machine, and a job queue with lease renewal would be ceremony around a job
whose cost is dominated by waiting on a vendor.

One thing this worker has that the bookmark one does not: **a per-account
guard**.  Parsing an upload is idempotent and cheap to repeat; embedding is
neither — two overlapping passes would read the same stale rows and pay for the
same vectors twice.  ``schedule_backfill`` therefore refuses to start a second
pass for an account that already has one running, and says so, rather than
silently spending the quota again.
"""

from __future__ import annotations

import asyncio
import logging

from webhub.db.database import Database

from .backfill import BackfillResult, backfill_embeddings
from .embeddings import EmbeddingEndpoint

_LOGGER = logging.getLogger(__name__)

# asyncio keeps only a weak reference to a detached task, so a strong one has to
# live somewhere or the pass can be collected halfway through.  Keying by
# account also gives the concurrency guard its answer for free.
_RUNNING: dict[str, asyncio.Task[BackfillResult]] = {}


def is_running(user_id: str) -> bool:
    return user_id in _RUNNING


async def run_backfill(
    database: Database,
    *,
    user_id: str,
    binding: EmbeddingEndpoint,
    limit: int,
) -> BackfillResult:
    """One backfill pass in its own session.

    Never raises: this task is detached, so an exception would surface only as
    an "unretrieved task exception" warning in the log.  Semantic recall is an
    enhancement — a failed pass leaves the digests unchanged and the next pass
    picks the same sites up.
    """

    try:
        async with database.sessions() as session:
            return await backfill_embeddings(
                session,
                user_id,
                binding=binding,
                limit=limit,
            )
    except Exception as error:  # noqa: BLE001 - a detached task must not escape
        _LOGGER.warning("embedding backfill failed for %s", user_id, exc_info=error)
        return BackfillResult(embedded=0, failed_batches=0, requests=0)


def schedule_backfill(
    database: Database,
    *,
    user_id: str,
    binding: EmbeddingEndpoint,
    limit: int,
) -> bool:
    """Start a pass unless this account already has one.

    Returns whether a new pass was started.  The caller reports that honestly —
    "已在进行中" is a different answer from "已排队", and conflating them would
    let a user click twice and believe they queued twice.
    """

    if user_id in _RUNNING:
        return False
    task = asyncio.create_task(
        run_backfill(database, user_id=user_id, binding=binding, limit=limit)
    )
    _RUNNING[user_id] = task
    task.add_done_callback(lambda _task: _RUNNING.pop(user_id, None))
    return True


__all__ = ["is_running", "run_backfill", "schedule_backfill"]
