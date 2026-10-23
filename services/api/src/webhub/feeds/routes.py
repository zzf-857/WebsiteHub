"""Cookie-session REST behind the /feeds page."""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from webhub.auth.dependencies import (
    CurrentIdentityDependency,
    DatabaseSessionDependency,
    require_trusted_origin,
)
from webhub.db.models import Feed, FeedItem, utc_now
from webhub.library.analysis_queue import schedule_site_enrichment

from . import service
from .schemas import (
    FeedCreateRequest,
    FeedItemListResponse,
    FeedItemResponse,
    FeedItemStateRequest,
    FeedListResponse,
    FeedPickListResponse,
    FeedPickResponse,
    FeedResponse,
    FeedSaveResponse,
    FeedUpdateRequest,
)

router = APIRouter(prefix="/feeds", tags=["feeds"])
WriteOriginDependency = Annotated[None, Depends(require_trusted_origin)]


async def _call[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except service.FeedError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def feed_response(feed: Feed, item_count: int) -> FeedResponse:
    return FeedResponse(
        id=feed.id,
        url=feed.url,
        title=feed.title,
        kind=feed.kind,  # type: ignore[arg-type]
        site_url=feed.site_url,
        enabled=bool(feed.enabled),
        last_fetched_at=_utc(feed.last_fetched_at),
        next_fetch_at=_utc(feed.next_fetch_at),
        consecutive_failures=int(feed.consecutive_failures or 0),
        last_error=feed.last_error,
        item_count=item_count,
    )


def item_response(item: FeedItem) -> FeedItemResponse:
    return FeedItemResponse(
        id=item.id,
        feed_id=item.feed_id,
        url=item.url,
        title=item.title,
        summary=item.summary,
        author=item.author,
        published_at=_utc(item.published_at),
        audio_url=item.audio_url,
        duration_seconds=item.duration_seconds,
        fetched_at=_utc(item.fetched_at) or utc_now(),
        read=item.read_at is not None,
        dismissed=item.dismissed_at is not None,
        saved_site_id=item.saved_site_id,
    )


async def _feed_with_count(request: Request, user_id: str, feed_id: str) -> FeedResponse:
    async with request.app.state.database.sessions() as session:
        for feed, count in await service.list_feeds(session, user_id):
            if feed.id == feed_id:
                return feed_response(feed, count)
    raise HTTPException(status_code=404, detail={"code": "not_found", "message": "订阅源不存在"})


@router.get("", response_model=FeedListResponse)
async def list_feeds(
    identity: CurrentIdentityDependency, session: DatabaseSessionDependency
) -> FeedListResponse:
    rows = await service.list_feeds(session, identity.user.id)
    return FeedListResponse(items=[feed_response(feed, count) for feed, count in rows])


@router.post("", response_model=FeedResponse, status_code=status.HTTP_201_CREATED)
async def add_feed(
    payload: FeedCreateRequest,
    request: Request,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> FeedResponse:
    feed = await _call(service.create_feed(session, identity.user.id, payload.url))
    # The first fetch is part of adding: the user sees the real title, or the
    # error state, right away instead of an empty row for up to a tick.
    await service.refresh_feed(request.app.state.database, feed.id)
    return await _feed_with_count(request, identity.user.id, feed.id)


@router.patch("/{feed_id}", response_model=FeedResponse)
async def update_feed(
    feed_id: str,
    payload: FeedUpdateRequest,
    request: Request,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> FeedResponse:
    await _call(service.set_feed_enabled(session, identity.user.id, feed_id, payload.enabled))
    return await _feed_with_count(request, identity.user.id, feed_id)


@router.post("/{feed_id}/refresh", response_model=FeedResponse)
async def refresh_feed(
    feed_id: str,
    request: Request,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> FeedResponse:
    await _call(service.owned_feed(session, identity.user.id, feed_id))
    await session.commit()
    await service.refresh_feed(request.app.state.database, feed_id)
    return await _feed_with_count(request, identity.user.id, feed_id)


@router.delete("/{feed_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_feed(
    feed_id: str,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> Response:
    await _call(service.delete_feed(session, identity.user.id, feed_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/items", response_model=FeedItemListResponse)
async def list_items(
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    feed_id: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> FeedItemListResponse:
    items = await service.list_items(session, identity.user.id, feed_id=feed_id, limit=limit)
    return FeedItemListResponse(items=[item_response(item) for item in items])


@router.get("/picks", response_model=FeedPickListResponse)
async def daily_picks(
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    limit: Annotated[int, Query(ge=1, le=30)] = service.DIGEST_LIMIT,
) -> FeedPickListResponse:
    picks = await service.daily_picks(session, identity.user.id, limit=limit)
    return FeedPickListResponse(
        generated_at=utc_now(),
        items=[
            FeedPickResponse(
                item=item_response(pick.item),
                feed_title=pick.feed_title,
                feed_kind=pick.feed_kind,  # type: ignore[arg-type]
                reason=pick.reason,
                matched=pick.matched,
            )
            for pick in picks
        ],
    )


@router.patch("/items/{item_id}", response_model=FeedItemResponse)
async def update_item(
    item_id: str,
    payload: FeedItemStateRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> FeedItemResponse:
    item = await _call(
        service.update_item_state(
            session,
            identity.user.id,
            item_id,
            read=payload.read,
            dismissed=payload.dismissed,
        )
    )
    return item_response(item)


@router.post("/items/{item_id}/save", response_model=FeedSaveResponse)
async def save_item(
    item_id: str,
    request: Request,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> FeedSaveResponse:
    item, site_id, created = await _call(
        service.save_item_to_library(session, identity.user.id, item_id)
    )
    if created:
        # Same background enrichment as any other save from the website.
        await schedule_site_enrichment(
            request.app.state.database, user_id=identity.user.id, site_id=site_id
        )
    return FeedSaveResponse(item=item_response(item), site_id=site_id, created=created)
