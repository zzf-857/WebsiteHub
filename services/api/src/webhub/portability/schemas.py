"""Versioned, allow-listed backup data and browser API contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, StrictInt, StrictStr

from webhub.chat.models import MAX_CONVERSATION_TITLE_LENGTH, MAX_MESSAGE_CONTENT_LENGTH

MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
Key = Annotated[StrictStr, Field(min_length=1, max_length=200)]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BackupAccount(StrictRequest):
    username: StrictStr = Field(min_length=1, max_length=32)
    display_name: StrictStr = Field(max_length=80)


class BackupCategory(StrictRequest):
    key: Key
    name: StrictStr = Field(min_length=1, max_length=80)
    icon: StrictStr = Field(max_length=40)
    is_default: StrictBool


class BackupTag(StrictRequest):
    key: Key
    name: StrictStr = Field(min_length=1, max_length=40)


class BackupSite(StrictRequest):
    key: Key
    url: StrictStr = Field(max_length=16_384)
    name: StrictStr = Field(min_length=1, max_length=160)
    summary: StrictStr = Field(max_length=50)
    description: StrictStr = Field(max_length=4_000)
    kind: StrictStr = Field(max_length=32)
    kind_source: StrictStr = Field(max_length=32)
    category_key: Key
    tag_keys: list[Key] = Field(max_length=20_000)
    pinned: StrictBool
    favicon_url: StrictStr | None = Field(max_length=4_096)
    preview_url: StrictStr | None = Field(max_length=16_384)
    meta: dict[str, JsonValue]
    created_at: datetime
    open_count: StrictInt = Field(ge=0, le=9_223_372_036_854_775_807)
    last_opened_at: datetime | None
    seen_at: datetime | None
    starred_at: datetime | None
    resurface_muted: StrictBool
    # T4 compatibility: this legacy field is never used as a persisted identity.
    identity_url: StrictStr | None = Field(default=None, max_length=16_384, exclude=True)


class BackupSpace(StrictRequest):
    key: Key
    name: StrictStr = Field(min_length=1, max_length=120)
    site_keys: list[Key] = Field(max_length=200_000)


class BackupFeed(StrictRequest):
    url: StrictStr = Field(min_length=1, max_length=2_048)
    title: StrictStr = Field(max_length=200)
    kind: Literal["rss", "podcast"]
    enabled: StrictBool


class BackupMessage(StrictRequest):
    role: Literal["user", "assistant"]
    text: StrictStr = Field(max_length=MAX_MESSAGE_CONTENT_LENGTH)
    created_at: datetime


class BackupConversation(StrictRequest):
    title: StrictStr = Field(min_length=1, max_length=MAX_CONVERSATION_TITLE_LENGTH)
    created_at: datetime
    messages: list[BackupMessage]


class BackupV1(StrictRequest):
    format: Literal["webhub-backup"]
    version: Literal[1]
    exported_at: datetime
    account: BackupAccount
    categories: list[BackupCategory] = Field(max_length=20_000)
    tags: list[BackupTag] = Field(max_length=20_000)
    sites: list[BackupSite] = Field(max_length=200_000)
    spaces: list[BackupSpace] = Field(max_length=20_000)
    feeds: list[BackupFeed] = Field(max_length=20_000)
    conversations: list[BackupConversation]


class ApplyRequest(StrictRequest):
    expected_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class ReuseCounts(BaseModel):
    create: int = 0
    reuse: int = 0


class SiteCounts(BaseModel):
    create: int = 0
    merge: int = 0
    skip_invalid: int = 0


class SpaceCounts(BaseModel):
    create: int = 0
    merge: int = 0


class FeedCounts(BaseModel):
    create: int = 0
    skip_existing: int = 0


class ConversationCounts(BaseModel):
    skipped: int = 0


class PreviewCounts(BaseModel):
    categories: ReuseCounts = Field(default_factory=ReuseCounts)
    tags: ReuseCounts = Field(default_factory=ReuseCounts)
    sites: SiteCounts = Field(default_factory=SiteCounts)
    spaces: SpaceCounts = Field(default_factory=SpaceCounts)
    feeds: FeedCounts = Field(default_factory=FeedCounts)
    conversations: ConversationCounts = Field(default_factory=ConversationCounts)


class WarningResponse(BaseModel):
    code: str
    message: str


class SampleResponse(BaseModel):
    name: str
    url: str | None = None


class PreviewResponse(BaseModel):
    id: str
    status: Literal["previewed", "applied", "expired"]
    expires_at: datetime
    payload_sha256: str
    counts: PreviewCounts
    warnings: list[WarningResponse]
    samples: dict[str, list[SampleResponse]]


class ResultCounts(BaseModel):
    categories: int = 0
    tags: int = 0
    sites: int = 0
    spaces: int = 0
    feeds: int = 0


class ApplyResponse(BaseModel):
    id: str
    status: Literal["applied"] = "applied"
    applied_at: datetime
    created: ResultCounts
    merged: ResultCounts
