"""Website REST models for /feeds. Requests forbid extra fields."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FeedKind = Literal["rss", "podcast"]


class FeedCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2_048)


class FeedUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class FeedItemStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    read: bool | None = None
    dismissed: bool | None = None


class FeedResponse(BaseModel):
    id: str
    url: str
    title: str
    kind: FeedKind
    site_url: str | None
    enabled: bool
    last_fetched_at: datetime | None
    next_fetch_at: datetime | None
    consecutive_failures: int
    last_error: str | None
    item_count: int


class FeedListResponse(BaseModel):
    items: list[FeedResponse]


class FeedItemResponse(BaseModel):
    id: str
    feed_id: str
    url: str | None
    title: str
    summary: str
    author: str | None
    published_at: datetime | None
    audio_url: str | None
    duration_seconds: int | None
    fetched_at: datetime
    read: bool
    dismissed: bool
    saved_site_id: str | None


class FeedItemListResponse(BaseModel):
    items: list[FeedItemResponse]


class FeedPickResponse(BaseModel):
    item: FeedItemResponse
    feed_title: str
    feed_kind: FeedKind
    reason: str
    matched: list[str]


class FeedPickListResponse(BaseModel):
    generated_at: datetime
    items: list[FeedPickResponse]


class FeedSaveResponse(BaseModel):
    item: FeedItemResponse
    site_id: str
    created: bool
