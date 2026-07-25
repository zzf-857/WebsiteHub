from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpaceCreateRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=120)


class SpaceUpdateRequest(StrictRequest):
    expected_version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)


class SpaceResponse(BaseModel):
    id: str
    name: str
    member_count: int
    version: int
    created_at: datetime
    updated_at: datetime


class SpaceListAggregate(BaseModel):
    total_count: int


class SpaceListResponse(BaseModel):
    items: list[SpaceResponse]
    next_cursor: str | None
    aggregate: SpaceListAggregate


class SpaceSiteReference(BaseModel):
    id: str
    name: str
    original_url: str
    identity_url: str
    description: str
    favicon_url: str | None
    pinned: bool
    version: int


class SpaceMemberResponse(BaseModel):
    site: SpaceSiteReference
    position: int
    added_at: datetime


class SpaceDetailResponse(SpaceResponse):
    members: list[SpaceMemberResponse]
    next_cursor: str | None


class SpaceMemberAddRequest(StrictRequest):
    expected_version: int = Field(ge=1)
    site_id: str = Field(min_length=1, max_length=100)


class SpaceMemberAddResponse(BaseModel):
    space: SpaceResponse
    member: SpaceMemberResponse


class SpaceMemberDeleteResponse(BaseModel):
    message: str
    space_id: str
    site_id: str
    member_count: int
    version: int


class SpaceReorderRequest(StrictRequest):
    expected_version: int = Field(ge=1)
    ordered_site_ids: list[str] = Field(min_length=1, max_length=100)
    before_site_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_member_ids(self) -> Self:
        if len(set(self.ordered_site_ids)) != len(self.ordered_site_ids):
            raise ValueError("排序成员不能重复")
        if self.before_site_id in self.ordered_site_ids:
            raise ValueError("定位成员不能同时出现在移动列表中")
        return self


class SpaceDeletePreviewResponse(BaseModel):
    space: SpaceResponse
    affected_site_count: int


class SpaceDeleteResponse(BaseModel):
    message: str
    space_id: str
    unlinked_site_count: int
