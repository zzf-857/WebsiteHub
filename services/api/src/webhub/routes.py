from fastapi import APIRouter, Request

from webhub.auth.routes import router as auth_router
from webhub.bookmarks.routes import router as bookmarks_router
from webhub.chat.routes import router as chat_router
from webhub.config import Settings
from webhub.library.routes import router as library_router
from webhub.providers.routes import router as providers_router
from webhub.schemas import HealthResponse, ReadinessResponse
from webhub.spaces.routes import router as spaces_router

router = APIRouter(prefix="/api")
router.include_router(auth_router, prefix="")
router.include_router(chat_router, prefix="")
router.include_router(bookmarks_router, prefix="")
router.include_router(library_router, prefix="")
router.include_router(providers_router, prefix="")
router.include_router(spaces_router, prefix="")


@router.get("/health", response_model=HealthResponse, tags=["system"])
async def health(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    return HealthResponse(
        status="ok",
        service=settings.service_name,
        version=settings.service_version,
        environment=settings.environment,
    )


@router.get("/ready", response_model=ReadinessResponse, tags=["system"])
async def readiness(request: Request) -> ReadinessResponse:
    await request.app.state.database.check()
    return ReadinessResponse(status="ready", checks={"api": "ok", "database": "ok"})
