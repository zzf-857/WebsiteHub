from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CategoryCreateRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=80)


class CategoryUpdateRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=80)


class CategoryReference(BaseModel):
    id: str
    name: str
    is_default: bool


class CategoryResponse(CategoryReference):
    site_count: int
    created_at: datetime
    updated_at: datetime


class CategoryListResponse(BaseModel):
    items: list[CategoryResponse]


class CategoryDeletePreviewResponse(BaseModel):
    category: CategoryResponse
    affected_site_count: int
    replacement_category: CategoryResponse


class CategoryDeleteResponse(BaseModel):
    message: str
    reassigned_site_count: int


class TagCreateRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=40)


class TagUpdateRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=40)


class TagReference(BaseModel):
    id: str
    name: str


class TagResponse(TagReference):
    site_count: int
    created_at: datetime
    updated_at: datetime


class TagListResponse(BaseModel):
    items: list[TagResponse]


class TagDeleteResponse(BaseModel):
    message: str
    unlinked_site_count: int


class SiteCreateRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=160)
    url: str = Field(min_length=1, max_length=16_384)
    description: str = Field(default="", max_length=4_000)
    favicon_url: str | None = Field(default=None, max_length=4_096)
    category_id: str | None = None
    tag_ids: list[str] = Field(default_factory=list, max_length=50)
    pinned: bool = False


class SiteUpdateRequest(StrictRequest):
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    url: str | None = Field(default=None, min_length=1, max_length=16_384)
    description: str | None = Field(default=None, max_length=4_000)
    favicon_url: str | None = Field(default=None, max_length=4_096)
    category_id: str | None = None
    tag_ids: list[str] | None = Field(default=None, max_length=50)
    pinned: bool | None = None


class SiteResponse(BaseModel):
    id: str
    name: str
    original_url: str
    identity_url: str
    description: str
    favicon_url: str | None
    category: CategoryReference
    tags: list[TagReference]
    pinned: bool
    source: Literal["manual", "agent", "browser_import", "backup"]
    analysis_status: Literal["not_analyzed", "pending", "complete", "failed", "limited"]
    version: int
    created_at: datetime
    updated_at: datetime


class SiteListAggregate(BaseModel):
    matched_count: int
    pinned_count: int


class SiteListResponse(BaseModel):
    items: list[SiteResponse]
    next_cursor: str | None
    aggregate: SiteListAggregate


class SiteDeleteResponse(BaseModel):
    message: str
    site_id: str
