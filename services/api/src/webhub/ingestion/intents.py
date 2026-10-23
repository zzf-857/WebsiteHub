"""Durable new-site analysis intents; the worker queue remains an acceleration layer."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert

from webhub.db.database import Database
from webhub.db.models import ProviderConfig, Site, SiteAnalysisIntent, SiteMetadataPreference
from webhub.ingestion import worker as ingestion_worker
from webhub.ingestion.enrichment import AnalysisIntent

MAX_INTENT_ATTEMPTS = 6
TICK_SECONDS = 60
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IntentTickResult:
    scheduled: int = 0
    completed: int = 0
    dropped: int = 0
    deferred: int = 0


async def record_site_intent(database: Database, *, user_id: str, site_id: str) -> None:
    """Commit independently so queue rejection or restart cannot lose the request."""
    now = datetime.now(UTC)
    async with database.sessions() as session:
        await session.execute(
            insert(SiteAnalysisIntent)
            .values(
                user_id=user_id,
                site_id=site_id,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "site_id"],
                set_={"next_attempt_at": now, "updated_at": now},
            )
        )
        await session.commit()


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(60 * 2 ** (attempts - 1), 6 * 3600))


async def tick(
    database: Database,
    *,
    now: datetime | None = None,
    limit: int = 64,
) -> IntentTickResult:
    now = now or datetime.now(UTC)
    scheduled = completed = dropped = deferred = 0
    # This bounded system scan intentionally spans accounts. All dependent
    # lookups and dispatches use the row's account, never a caller's identity.
    async with database.sessions() as session:
        keys = (
            await session.execute(
                select(SiteAnalysisIntent.user_id, SiteAnalysisIntent.site_id)
                .where(SiteAnalysisIntent.next_attempt_at <= now)
                .order_by(SiteAnalysisIntent.next_attempt_at, SiteAnalysisIntent.site_id)
                .limit(limit)
            )
        ).all()
    for user_id, site_id in keys:
        async with database.sessions() as session:
            # Reserve before reading completion state: a URL edit can clear that
            # state and recreate this intent. An old tick must not delete the
            # newer request, nor overwrite its next_attempt_at after a retry.
            reserved = await session.execute(
                update(SiteAnalysisIntent)
                .where(
                    SiteAnalysisIntent.user_id == user_id,
                    SiteAnalysisIntent.site_id == site_id,
                    SiteAnalysisIntent.next_attempt_at <= now,
                )
                .values(updated_at=SiteAnalysisIntent.updated_at)
                .execution_options(synchronize_session=False)
            )
            if reserved.rowcount != 1:  # type: ignore[attr-defined]
                continue
            row = await session.get(SiteAnalysisIntent, (user_id, site_id))
            if row is None:
                continue
            site = await session.scalar(
                select(Site.id).where(
                    Site.user_id == user_id,
                    Site.id == site_id,
                )
            )
            preference = await session.get(SiteMetadataPreference, (user_id, site_id))
            provider = await session.scalar(
                select(ProviderConfig.id)
                .where(
                    ProviderConfig.user_id == user_id,
                    ProviderConfig.kind == "model",
                    ProviderConfig.enabled.is_(True),
                )
                .limit(1)
            )
            if site is None:
                await session.delete(row)
                dropped += 1
            elif preference is not None and preference.llm_analyzed_at is not None:
                await session.delete(row)
                completed += 1
            elif provider is None or row.attempts >= MAX_INTENT_ATTEMPTS:
                if provider is not None:
                    _LOGGER.warning("site analysis intent exhausted site_id=%s", site_id)
                await session.delete(row)
                dropped += 1
            else:
                result = ingestion_worker.schedule_analysis(
                    database,
                    user_id=user_id,
                    site_ids=(site_id,),
                    intent=AnalysisIntent.SITE_ENRICHMENT,
                    priority=False,
                    interactive=False,
                    bulk=False,
                )
                if result.rejected:
                    row.next_attempt_at = now + timedelta(seconds=60)
                    deferred += 1
                else:
                    row.attempts += 1
                    row.next_attempt_at = now + _backoff(row.attempts)
                    scheduled += 1
                row.updated_at = now
            await session.commit()
    return IntentTickResult(scheduled, completed, dropped, deferred)


async def _loop(database: Database) -> None:
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            await tick(database)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Exception text can contain SQL parameters; never log it here.
            _LOGGER.error("site analysis intent polling failed")


class IntentPoller:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(_loop(self._database), name="webhub-intent-poller")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
