"""HTTP surface for usage signals, resurfacing and the library overview.

Kept apart from ``library/routes.py`` (already close to the backend's
1000-line ceiling). Site-level writes stay under the existing
``/library/sites/{id}`` namespace next to PATCH/DELETE; the resurfacing read
gets its own ``/discover`` prefix for the discovery home page.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from webhub.auth.dependencies import CurrentIdentityDependency, DatabaseSessionDependency
from webhub.library import service
from webhub.library.routes import WriteOriginDependency, _call
from webhub.library.schemas import (
    MAX_REDISCOVER_LIMIT,
    LibraryOverviewResponse,
    RediscoverResponse,
    SiteOpenResponse,
    SiteResponse,
    SiteStateUpdateRequest,
)

router = APIRouter(tags=["library"])


@router.post("/library/sites/{site_id}/open", response_model=SiteOpenResponse)
async def record_site_open(
    site_id: str,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> SiteOpenResponse:
    """Record one open through WebHub without touching version/updated_at."""

    return await _call(service.record_site_open(session, identity.user.id, site_id))


@router.put("/library/sites/{site_id}/state", response_model=SiteResponse)
async def update_site_state(
    site_id: str,
    payload: SiteStateUpdateRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> SiteResponse:
    """Star / mark seen / snooze / mute; reversible and version-free."""

    return await _call(service.update_site_state(session, identity.user.id, site_id, payload))


@router.get("/library/overview", response_model=LibraryOverviewResponse)
async def library_overview(
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
) -> LibraryOverviewResponse:
    return await _call(service.library_overview(session, identity.user.id))


@router.get("/discover/rediscover", response_model=RediscoverResponse)
async def rediscover(
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    limit: Annotated[int, Query(ge=1, le=MAX_REDISCOVER_LIMIT)] = 4,
    shuffle: Annotated[int, Query(ge=0)] = 0,
) -> RediscoverResponse:
    """Today's resurfacing picks; a pure read, stable per UTC day and shuffle."""

    return await _call(
        service.rediscover(session, identity.user.id, limit=limit, shuffle=shuffle)
    )
