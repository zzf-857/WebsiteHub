from datetime import date, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.functional_validators import AfterValidator, BeforeValidator

# icons 与 kinds 都是纯函数模块（不依赖 library 其他部分），从它们引用取值集合不会成环。
from webhub.library.icons import validate_category_icon
from webhub.library.kinds import KindSource, SiteKind

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


CategoryIcon = Annotated[str | None, AfterValidator(validate_category_icon)]


class CategoryCreateRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=80)
    icon: CategoryIcon = Field(default=None, max_length=40)


class CategoryUpdateRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=80)
    icon: CategoryIcon = Field(default=None, max_length=40)


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


class TagMergeRequest(StrictRequest):
    target_id: str = Field(min_length=1, max_length=36)


class TagMergePreviewResponse(BaseModel):
    source: TagResponse
    target: TagResponse
    # 带源标签的收藏数：合并后它们都带目标标签
    affected_site_count: int
    # 其中原本就同时带目标标签的收藏数（合并只去掉源标签）
    already_tagged_count: int


class TagMergeResponse(BaseModel):
    message: str
    target: TagResponse
    affected_site_count: int
    already_tagged_count: int


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
    # 显式指定资源类型：此后 kind_source=manual，改 URL 也不再自动重算。
    kind: SiteKind | None = None

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
    # Q42 收藏模型：资源类型与使用信号。使用信号不属于“编辑”，变化时
    # ``version`` / ``updated_at`` 保持不变。
    kind: SiteKind
    kind_source: KindSource
    open_count: int = Field(ge=0)
    last_opened_at: datetime | None
    seen_at: datetime | None
    starred: bool
    starred_at: datetime | None
    resurface_muted: bool
    # 由 ``meta_json`` 解析的按类型元数据（仓库为 host/owner/repo）；损坏时为 {}。
    meta: dict[str, Any]
    # Q46 详情页“使用记录”：累计推迟次数与当前推迟到期时间（未推迟为 null）。
    # 给默认值，让相似排查等只读快照不必伪造调度状态。
    resurface_count: int = Field(default=0, ge=0)
    resurface_due_at: datetime | None = None


MAX_SNOOZE_DAYS = 90


class SiteStateUpdateRequest(StrictRequest):
    """Reversible engagement state. Never bumps the site version.

    Strict JSON types: ``"true"`` or ``1`` is not a boolean here, and ``7.0``
    is not a day count. Absent fields are left unchanged; an explicit ``null``
    is rejected rather than read as "no change".
    """

    starred: StrictBool | None = None
    # false 清空 seen_at；true 在未看过时记为现在（已有时间保持不变）。
    seen: StrictBool | None = None
    # 0 清除推迟；1-90 表示 resurface_due_at = 现在 + N 天，并累计一次推迟次数。
    snooze_days: StrictInt | None = Field(default=None, ge=0, le=MAX_SNOOZE_DAYS)
    resurface_muted: StrictBool | None = None

    @model_validator(mode="after")
    def require_one_explicit_field(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("状态更新至少需要一个字段")
        empty = sorted(name for name in self.model_fields_set if getattr(self, name) is None)
        if empty:
            raise ValueError(f"{'、'.join(empty)} 不能为 null")
        return self


class SiteOpenResponse(BaseModel):
    """Usage counters after one recorded open; the site version is untouched."""

    site_id: str
    open_count: int = Field(ge=1)
    last_opened_at: datetime
    seen_at: datetime


MAX_REDISCOVER_LIMIT = 12


class RediscoverItemResponse(BaseModel):
    site: SiteResponse
    saved_days: int = Field(ge=0)
    # 规则生成的中文理由，可解释、不经模型。
    reason: str


class RediscoverResponse(BaseModel):
    # 本次轮转所依据的 UTC 日期：同一天、同一 shuffle 的结果稳定。
    generated_for: date
    items: list[RediscoverItemResponse] = Field(max_length=MAX_REDISCOVER_LIMIT)


class LibraryKindCounts(BaseModel):
    repo: int = Field(ge=0)
    article: int = Field(ge=0)
    video: int = Field(ge=0)
    website: int = Field(ge=0)


class LibraryOverviewResponse(BaseModel):
    total: int = Field(ge=0)
    by_kind: LibraryKindCounts
    # 从未在 WebHub 打开、也没有标记已看（与重温候选、state=unopened 同一口径）。
    unopened: int = Field(ge=0)
    starred: int = Field(ge=0)
    # 近 7 天新收藏数；近 7 天打开过的收藏数（按收藏去重，不是打开次数）。
    added_7d: int = Field(ge=0)
    opened_7d: int = Field(ge=0)


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
    # Q48: items of this page that matched the search only through their
    # content snapshot (page text / README), mapped to a short excerpt. Items
    # absent here matched on their own fields (title, URL, description, ...).
    content_matches: dict[str, str] = Field(default_factory=dict)


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
