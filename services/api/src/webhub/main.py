from contextlib import asynccontextmanager

from fastapi import FastAPI

from webhub.auth.rate_limit import LoginRateLimiter
from webhub.config import Settings, get_settings
from webhub.db.database import Database
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
        yield
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
    application.state.login_rate_limiter = LoginRateLimiter()
    application.include_router(router)
    return application


app = create_app()
