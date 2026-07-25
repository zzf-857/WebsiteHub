from fastapi import APIRouter, Request

from webhub.auth.routes import router as auth_router
from webhub.config import Settings
from webhub.schemas import HealthResponse, ReadinessResponse

router = APIRouter(prefix="/api")
router.include_router(auth_router, prefix="")


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
