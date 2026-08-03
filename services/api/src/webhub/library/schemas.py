from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)
from pydantic.functional_validators import BeforeValidator

# 定义在这里而不是 batch.py：schemas 不依赖任何 library 内部模块，
# 反过来让 batch 引用它才不会形成 schemas → batch → service → schemas 的环。
MAX_BATCH_URLS = 50
MAX_BULK_DELETE_SITES = 100

_HTTP_URL_ADAPTER = TypeAdapter(AnyHttpUrl)


def normalize_favicon_url(value: object) -> object:
    if value is None or not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return str(_HTTP_URL_ADAPTER.validate_python(stripped))
    except ValidationError as error:
        raise ValueError("favicon_url 必须是绝对 HTTP(S) URL") from error


FaviconUrl = Annotated[str | None, BeforeValidator(normalize_favicon_url)]
SiteCreateSource = Literal["manual", "agent"]


def normalize_site_summary_input(value: object) -> object:
    """Allow an intentional blank; otherwise enforce the shared 20-50 contract."""

    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if normalized and len(normalized) < 20:
        raise ValueError("摘要留空表示不设置；填写时长度必须为 20 到 50 个字符")
    return normalized


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CategoryCreateRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=80)
    icon: str | None = None


class CategoryUpdateRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=80)
    icon: str | None = None


class CategoryReference(BaseModel):
    id: str
    name: str
    is_default: bool
    icon: str


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
    summary: str = Field(default="", max_length=50)
    description: str = Field(default="", max_length=4_000)
    favicon_url: FaviconUrl = Field(default=None, max_length=4_096)
    category_id: str | None = None
    tag_ids: list[str] = Field(default_factory=list, max_length=50)
    pinned: bool = False
    source: SiteCreateSource = "manual"

    @field_validator("summary", mode="before")
    @classmethod
    def normalize_summary(cls, value: object) -> object:
        return normalize_site_summary_input(value)


class SiteUpdateRequest(StrictRequest):
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    url: str | None = Field(default=None, min_length=1, max_length=16_384)
    summary: str | None = Field(default=None, max_length=50)
    description: str | None = Field(default=None, max_length=4_000)
    favicon_url: FaviconUrl = Field(default=None, max_length=4_096)
    category_id: str | None = None
    tag_ids: list[str] | None = Field(default=None, max_length=50)
    pinned: bool | None = None

    @field_validator("summary", mode="before")
    @classmethod
    def normalize_summary(cls, value: object) -> object:
        return normalize_site_summary_input(value)


class SiteResponse(BaseModel):
    id: str
    name: str
    original_url: str
    identity_url: str
    summary: str
    description: str
    favicon_url: FaviconUrl
    preview_url: str | None = None
    category: CategoryReference
    tags: list[TagReference]
    pinned: bool
    source: Literal["manual", "agent", "browser_import", "backup"]
    analysis_status: Literal["not_analyzed", "pending", "complete", "failed", "limited"]
    analysis_phase: Literal[
        "fetching_page",
        "preparing_evidence",
        "waiting_model",
        "calling_model",
        "saving_result",
    ] | None
    version: int
    created_at: datetime
    updated_at: datetime


class SiteAnalysisResponse(BaseModel):
    """Committed site state plus the real outcome of one requested analysis."""

    site: SiteResponse
    outcome: Literal["complete", "limited", "failed"]
    message: str
    llm_applied: bool


class SiteListAggregate(BaseModel):
    matched_count: int
    pinned_count: int


class SiteListResponse(BaseModel):
    items: list[SiteResponse]
    next_cursor: str | None
    aggregate: SiteListAggregate


class SiteSelectionItem(BaseModel):
    """Lightweight, versioned row used to freeze a bulk-selection snapshot."""

    id: str
    name: str
    original_url: str
    favicon_url: FaviconUrl
    version: int


class SiteSelectionResponse(BaseModel):
    items: list[SiteSelectionItem]


class SiteAnalysisBackfillResponse(BaseModel):
    queued_count: int
    active_count: int
    remaining_count: int


MetadataBackfillMode = Literal["metadata", "full"]
MetadataBackfillStopReason = Literal[
    "provider_rate_limited",
    "provider_temporary_failure",
    "provider_unavailable",
    "internal_error",
]


class MetadataBackfillStartRequest(StrictRequest):
    """Bound one user-triggered run before any outbound work starts."""

    mode: MetadataBackfillMode = "metadata"
    limit: int = Field(default=200, ge=1, le=500)


class MetadataBackfillPlanResponse(BaseModel):
    """Exact database selection preview for a proposed bounded run."""

    mode: MetadataBackfillMode
    requested_limit: int = Field(ge=1)
    max_limit: int = Field(ge=1)
    eligible_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    llm_count: int = Field(ge=0)


class MetadataBackfillProgressResponse(BaseModel):
    """A fixed-denominator snapshot for the homepage metadata command."""

    id: str
    mode: MetadataBackfillMode
    status: Literal[
        "queued",
        "running",
        "completed",
        "completed_with_errors",
        "failed",
    ]
    stopped_early: bool
    stop_reason: MetadataBackfillStopReason | None
    provider_retry_at: datetime | None
    total_count: int = Field(ge=0)
    queued_count: int = Field(ge=0)
    running_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    complete_count: int = Field(ge=0)
    limited_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)


class MetadataBackfillStartResponse(MetadataBackfillProgressResponse):
    reused: bool


class SiteDeleteResponse(BaseModel):
    message: str
    site_id: str


class SiteBulkDeleteItem(StrictRequest):
    site_id: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)


class SiteBulkDeleteRequest(StrictRequest):
    items: list[SiteBulkDeleteItem] = Field(min_length=1, max_length=MAX_BULK_DELETE_SITES)

    @field_validator("items")
    @classmethod
    def reject_duplicate_site_ids(
        cls,
        items: list[SiteBulkDeleteItem],
    ) -> list[SiteBulkDeleteItem]:
        site_ids = [item.site_id for item in items]
        if len(site_ids) != len(set(site_ids)):
            raise ValueError("items 不能包含重复的网站")
        return items


class SiteBulkDeleteResponse(BaseModel):
    message: str
    deleted_site_ids: list[str]


class SiteBatchRequest(StrictRequest):
    """批量入库。urls 与 text 二选一：前端给已抽好的列表，Agent/命令行给原文。"""

    urls: list[str] | None = Field(default=None, max_length=MAX_BATCH_URLS)
    text: str | None = Field(default=None, max_length=20_000)
    confirm: bool = False
    source: SiteCreateSource = "manual"


class SiteBatchItemResponse(BaseModel):
    url: str
    status: str
    reason: str | None = None
    site_id: str | None = None


class SiteBatchResponse(BaseModel):
    confirmed: bool
    total: int
    ready: int
    duplicate: int
    invalid: int
    created: int
    failed: int
    items: list[SiteBatchItemResponse]


class SiteReorderRequest(StrictRequest):
    """一次可以移动多个网站，保持它们之间的相对顺序。"""

    ordered_site_ids: list[str] = Field(min_length=1, max_length=200)
    # 移动块落在这个网站之前；None 表示放到末尾。用锚点而不是绝对下标：
    # 下标在用户看着的那份列表之外一变就失效，「放在它前面」不会。
    before_site_id: str | None = None


SiteSimilarityKind = Literal["duplicate", "same_site"]
SiteSimilarityKindFilter = Literal["duplicate", "same_site", "all"]
SiteSimilarityRunStatus = Literal["ready", "applied", "superseded"]
SiteSimilarityKeepSiteId = Annotated[str, Field(min_length=1, max_length=64)]


class SiteSimilarityScanResponse(BaseModel):
    id: str
    status: SiteSimilarityRunStatus
    ruleset_version: str
    source_site_count: int = Field(ge=0)
    group_count: int = Field(ge=0)
    duplicate_group_count: int = Field(ge=0)
    same_site_group_count: int = Field(ge=0)
    candidate_site_count: int = Field(ge=0)
    selected_group_count: int = Field(ge=0)
    selected_delete_count: int = Field(ge=0)
    version: int = Field(ge=1)
    decision_version: int = Field(ge=1)
    created_at: datetime
    applied_at: datetime | None


class SiteSimilarityMemberResponse(SiteResponse):
    """A full site card frozen at scan time, not a live library row."""

    is_recommended: bool


class SiteSimilarityGroupResponse(BaseModel):
    id: str
    kind: SiteSimilarityKind
    site_key: str
    display_host: str
    member_count: int = Field(ge=2)
    recommended_site_id: str
    keep_site_ids: list[str]
    members: list[SiteSimilarityMemberResponse]


class SiteSimilarityGroupPageResponse(BaseModel):
    items: list[SiteSimilarityGroupResponse]
    next_cursor: str | None
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=24)
    total_count: int = Field(ge=0)
    total_pages: int = Field(ge=0)
    decision_version: int = Field(ge=1)


class SiteSimilarityDecisionRequest(StrictRequest):
    keep_site_ids: list[SiteSimilarityKeepSiteId] = Field(
        default_factory=list,
        max_length=10_000,
    )
    expected_version: int = Field(ge=1)

    @field_validator("keep_site_ids")
    @classmethod
    def unique_keep_site_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("keep_site_ids 不能包含重复网站")
        return value


class SiteSimilarityDecisionResponse(BaseModel):
    group_id: str
    keep_site_ids: list[str]
    decision_version: int = Field(ge=1)
    selected_group_count: int = Field(ge=0)
    selected_delete_count: int = Field(ge=0)


class SiteSimilarityRecommendedDecisionRequest(StrictRequest):
    kind: SiteSimilarityKindFilter
    expected_version: int = Field(ge=1)


class SiteSimilarityRecommendedDecisionResponse(BaseModel):
    kind: SiteSimilarityKindFilter
    matched_group_count: int = Field(ge=0)
    updated_group_count: int = Field(ge=0)
    decision_version: int = Field(ge=1)
    selected_group_count: int = Field(ge=0)
    selected_delete_count: int = Field(ge=0)


class SiteSimilarityApplyRequest(StrictRequest):
    expected_version: int = Field(ge=1)


class SiteSimilarityApplyResponse(BaseModel):
    id: str
    status: Literal["applied"]
    decision_version: int = Field(ge=1)
    merged_group_count: int = Field(ge=0)
    deleted_site_count: int = Field(ge=0)
    kept_site_count: int = Field(ge=0)
    deleted_site_ids: list[str]
    kept_site_ids: list[str]
    applied_at: datetime
