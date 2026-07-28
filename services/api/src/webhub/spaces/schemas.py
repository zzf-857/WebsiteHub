from datetime import datetime
from typing import Literal, Self

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
    summary: str
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


class SpaceMemberBatchTarget(StrictRequest):
    mode: Literal["existing", "create"]
    space_name: str = Field(min_length=1, max_length=120)
    space_id: str | None = Field(default=None, min_length=1, max_length=36)
    expected_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_mode_fields(self) -> Self:
        if self.mode == "existing":
            if self.space_id is None or self.expected_version is None:
                raise ValueError("已有 Space 必须提供 space_id 和 expected_version")
        elif self.space_id is not None or self.expected_version is not None:
            raise ValueError("新建 Space 不能预先指定 space_id 或 expected_version")
        return self


class SpaceMemberBatchRequest(StrictRequest):
    target: SpaceMemberBatchTarget
    site_ids: list[str] = Field(default_factory=list, max_length=100)
    operation_id: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_site_ids(self) -> Self:
        if self.target.mode == "existing" and not self.site_ids:
            raise ValueError("已有 Space 的批量任务至少需要一个网站")
        if not self.operation_id.strip():
            raise ValueError("operation_id 不能为空")
        if any(not site_id.strip() or len(site_id) > 36 for site_id in self.site_ids):
            raise ValueError("网站 ID 无效")
        if len(set(self.site_ids)) != len(self.site_ids):
            raise ValueError("批量加入的网站不能重复")
        return self


class SpaceMemberBatchResponse(BaseModel):
    space: SpaceResponse
    added_count: int
    already_member_count: int
    site_ids: list[str]


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
