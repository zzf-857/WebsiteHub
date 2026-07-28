from contextlib import asynccontextmanager

from fastapi import FastAPI

from webhub.agent.access import DatabaseConversationAccess
from webhub.agent.langgraph_runner import build_agent_runner
from webhub.agent.site_enrichment import AgentSiteEnricher
from webhub.auth.rate_limit import LoginRateLimiter
from webhub.bookmarks.admission import BookmarkUploadAdmissionManager
from webhub.config import Settings, get_settings
from webhub.db.database import Database
from webhub.ingestion import worker as ingestion_worker
from webhub.routes import router


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
) -> FastAPI:
    selected_settings = settings or get_settings()
    selected_database = database or Database(selected_settings.database_url)
    owns_database = database is None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await selected_database.assert_schema_current()
        ingestion_worker.start(
            selected_database,
            site_enricher=AgentSiteEnricher(selected_database, selected_settings),
        )
        # The homepage batch task is durable rather than an in-memory queue.
        # Resume its active leases before serving requests so a browser tab is
        # not required to recover work after an API process restart.
        await ingestion_worker.resume_metadata_backfills(selected_database)
        try:
            yield
        finally:
            try:
                await ingestion_worker.shutdown(selected_database)
            finally:
                if owns_database:
                    await selected_database.dispose()

    application = FastAPI(
        title="WebHub API",
        summary="Business API for the WebHub website",
        version=selected_settings.service_version,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )
    application.state.settings = selected_settings
    application.state.database = selected_database
    application.state.agent_conversation_access = DatabaseConversationAccess(selected_database)
    # The runner resolves each account's own Provider credentials per turn, so
    # registering it here does not imply any configured model.
    application.state.agent_runner = build_agent_runner(selected_database, selected_settings)
    application.state.login_rate_limiter = LoginRateLimiter()
    application.state.bookmark_upload_admission = BookmarkUploadAdmissionManager(
        data_directory=selected_settings.data_directory,
        global_concurrency=selected_settings.bookmark_upload_global_concurrency,
        rate_limit_attempts=selected_settings.bookmark_upload_rate_limit_attempts,
        rate_limit_window_seconds=(selected_settings.bookmark_upload_rate_limit_window_seconds),
        max_tracked_accounts=selected_settings.bookmark_upload_max_tracked_accounts,
        account_quota_bytes=selected_settings.bookmark_upload_account_quota_bytes,
        minimum_free_bytes=selected_settings.bookmark_upload_minimum_free_bytes,
        disk_check_interval_bytes=(selected_settings.bookmark_upload_disk_check_interval_bytes),
    )
    application.include_router(router)
    return application


app = create_app()
