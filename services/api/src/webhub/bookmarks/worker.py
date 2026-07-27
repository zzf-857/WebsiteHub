"""Drive an uploaded snapshot through parsing into a ready preview.

The staging layer, the parser and the preview queries all worked, but nothing
connected them inside the server: an upload left its job in ``queued_parse``
forever, so every preview endpoint answered "解析预览尚未完成" and the whole
feature was unreachable from the browser.

This is deliberately an **in-process** worker rather than the leased, heartbeat
driven one the schema anticipates.  WebHub is a single-user app running on the
user's own machine; a job queue with lease renewal would be ceremony around a
1.7-second CPU-bound parse.  The durable state machine is still respected step
by step, so replacing this with a real worker later needs no schema change:

    queued_parse → begin_parse_run → append_parse_chunk* → finalize_parse_run
                 → parse_preview_ready

Failures land on ``fail_parse_run`` so the job reports a cause instead of
sitting in ``parsing`` forever.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from webhub.bookmarks import persistence
from webhub.bookmarks.models import BookmarkEvent, ParsedFolder, ParserLimits, ParserStats
from webhub.bookmarks.parser import iter_netscape_events
from webhub.db.database import Database

_LOGGER = logging.getLogger(__name__)

# Events per staged chunk.  Each chunk is one transaction and one checkpoint,
# so this trades restart granularity against write amplification.
CHUNK_SIZE = 500


def _snapshot_path(data_directory: Path, account_id: str, snapshot_id: str) -> Path:
    return data_directory / "bookmark-imports" / account_id / snapshot_id / "source.html"


async def _stage_events(
    database: Database,
    user_id: str,
    job_id: str,
    run_id: str,
    source: Path,
    limits: ParserLimits,
) -> persistence.ParseCompletion:
    """Parse the snapshot and stage it chunk by chunk.

    Parsing is CPU-bound and synchronous; it runs in a thread so a large import
    cannot stall the event loop that is still serving the rest of the app.
    """

    chunk: list[BookmarkEvent] = []
    chunk_index = 0
    folder_count = 0
    occurrence_count = 0

    def _read() -> tuple[list[BookmarkEvent], ParserStats]:
        # iter_netscape_events mutates the stats object it is handed rather than
        # yielding it, so the source digest has to be collected this way.
        stats = ParserStats()
        return list(iter_netscape_events(source, limits=limits, stats=stats)), stats

    events, stats = await asyncio.to_thread(_read)

    for event in events:
        if isinstance(event, ParsedFolder):
            folder_count += 1
        else:
            occurrence_count += 1
        chunk.append(event)
        if len(chunk) >= CHUNK_SIZE:
            async with database.sessions() as session:
                await persistence.append_parse_chunk(
                    session,
                    user_id,
                    job_id,
                    run_id,
                    chunk_index=chunk_index,
                    events=chunk,
                )
            chunk_index += 1
            chunk = []

    if chunk:
        async with database.sessions() as session:
            await persistence.append_parse_chunk(
                session,
                user_id,
                job_id,
                run_id,
                chunk_index=chunk_index,
                events=chunk,
            )

    return persistence.ParseCompletion(
        source_sha256=stats.source_sha256,
        source_sequence_count=folder_count + occurrence_count,
        folder_count=folder_count,
        occurrence_count=occurrence_count,
    )


async def run_parse(
    database: Database,
    data_directory: Path,
    *,
    user_id: str,
    job_id: str,
    snapshot_id: str,
    expected_job_version: int,
    limits: ParserLimits | None = None,
) -> str:
    """Take one queued job all the way to ``parse_preview_ready``.

    Returns the run id.  Raises only on failures that could not be recorded on
    the job itself.
    """

    parser_limits = limits or ParserLimits()
    source = _snapshot_path(data_directory, user_id, snapshot_id)

    async with database.sessions() as session:
        run = await persistence.begin_parse_run(
            session,
            user_id,
            job_id,
            expected_job_version=expected_job_version,
            idempotency_key=f"inline-parse:{job_id}:{expected_job_version}",
        )
    run_id = run.run_id
    job_version = run.job_version

    try:
        completion = await _stage_events(
            database,
            user_id,
            job_id,
            run_id,
            source,
            parser_limits,
        )
        async with database.sessions() as session:
            preview = await persistence.finalize_parse_run(
                session,
                user_id,
                job_id,
                run_id,
                expected_job_version=job_version,
                completion=completion,
            )
        return preview.run_id
    except Exception as error:  # noqa: BLE001 - the job must not stay in "parsing"
        _LOGGER.warning("bookmark parse failed for job %s", job_id, exc_info=error)
        failure_code = (
            "invalid_bookmark_file"
            if isinstance(error, persistence.BookmarkPersistenceValidationError)
            else "internal_error"
        )
        try:
            async with database.sessions() as session:
                await persistence.fail_parse_run(
                    session,
                    user_id,
                    job_id,
                    run_id,
                    expected_job_version=job_version,
                    failure_code=failure_code,
                )
        except Exception:  # noqa: BLE001 - nothing left to do but surface the original
            _LOGGER.exception("could not record parse failure for job %s", job_id)
            # 只有「连失败都没记下来」才往上抛——这与 docstring 的契约一致。
            raise
        # 失败已经写在 job 上了，用户能从预览接口看到原因。这里唯一的调用方是
        # routes.py 里 asyncio.create_task 起的脱钩任务，它的 done_callback 只做
        # discard、从不取回结果：再抛一次没人接，只会变成
        # "Task exception was never retrieved" 噪声。
        return run_id


__all__ = ["CHUNK_SIZE", "run_parse"]
