"""Queue the background analysis of one newly saved or retargeted site.

Shared by the website REST routes and the MCP ``save_resource`` tool, so an
Agent save is enriched exactly like a save from the website.
"""

from __future__ import annotations

import logging

from webhub.db.database import Database
from webhub.ingestion import intents
from webhub.ingestion import worker as ingestion_worker
from webhub.ingestion.enrichment import AnalysisIntent


async def schedule_site_enrichment(database: Database, *, user_id: str, site_id: str) -> None:
    try:
        await intents.record_site_intent(database, user_id=user_id, site_id=site_id)
    except Exception:
        # This best-effort side effect must never undo an already saved collection.
        logging.getLogger(__name__).error("site analysis intent persistence failed")
    scheduled = ingestion_worker.schedule_analysis(
        database,
        user_id=str(user_id),
        site_ids=(site_id,),
        priority=True,
        # A manual/Agent save is an explicit foreground action. Let it use the
        # queue's single bounded overflow slot just like the detail-page AI
        # action, instead of dropping its enrichment intent behind a large
        # historical batch.
        interactive=True,
        intent=AnalysisIntent.SITE_ENRICHMENT,
        bulk=False,
    )
    # When the foreground queue is full, leave the newly created/retargeted
    # site to the database-driven sweep instead of silently giving up until a
    # later list request happens to wake it.
    if scheduled.rejected:
        ingestion_worker.ensure_auto_backfill(
            database,
            user_id=str(user_id),
            rescan_if_running=True,
        )
