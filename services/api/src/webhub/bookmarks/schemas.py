from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FetchPolicy = Literal["public_revalidation_required", "export_metadata_only"]
ProposedAction = Literal[
    "create",
    "skip_existing",
    "merge_missing_metadata",
    "reject",
    "needs_review",
]
ValidationStatus = Literal["accepted", "invalid", "unsupported"]
SimilarityDecision = Literal["merge_to_homepage", "keep_originals"]
SimilarityDecisionFilter = Literal["unresolved", "merge_to_homepage", "keep_originals"]
SimilarityConfidence = Literal["high", "medium", "low"]
SimilarityCanonicalSource = Literal[
    "imported_homepage",
    "derived_origin_root",
    "existing_library",
]
BookmarkImportFailureCode = Literal[
    "classification_budget_exhausted",
    "internal_error",
    "invalid_bookmark_file",
    "processing_limit_exceeded",
]


class BookmarkImportUploadResponse(BaseModel):
    job_id: str
    state: str
    job_version: int
    replayed: bool
    same_source_warning: bool


class BookmarkImportProgressResponse(BaseModel):
    completed: int
    total: int


class BookmarkImportStatusResponse(BaseModel):
    job_id: str
    state: str
    job_version: int
    preview_version: int
    progress: BookmarkImportProgressResponse
    failure_code: BookmarkImportFailureCode | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class BookmarkPreviewOccurrenceCounts(BaseModel):
    accepted: int
    invalid: int
    unsupported: int


class BookmarkPreviewCandidateActionCounts(BaseModel):
    create: int
    skip_existing: int
    merge_missing_metadata: int
    reject: int
    needs_review: int


class BookmarkSimilarityDecisionCounts(BaseModel):
    unresolved: int
    merge_to_homepage: int
    keep_originals: int


class BookmarkPreviewSummaryResponse(BaseModel):
    job_id: str
    run_id: str
    job_version: int
    preview_version: int
    source_sequence_count: int
    folder_count: int
    occurrence_count: int
    candidate_count: int
    occurrence_counts: BookmarkPreviewOccurrenceCounts
    duplicate_occurrence_count: int
    candidate_action_counts: BookmarkPreviewCandidateActionCounts
    metadata_only_candidate_count: int
    sensitive_candidate_count: int
    decision_version: int
    similarity_cluster_count: int
    similarity_candidate_count: int
    similarity_decision_counts: BookmarkSimilarityDecisionCounts
    selected_merge_reduction_count: int
    projected_create_count: int


class BookmarkSimilarityCanonicalResponse(BaseModel):
    candidate_id: str | None
    url: str
    title: str
    source: SimilarityCanonicalSource


class BookmarkSimilarityMemberResponse(BaseModel):
    candidate_id: str
    title: str
    display_url: str
    occurrence_count: int
    first_source_sequence: int
    is_canonical: bool


class BookmarkSimilarityClusterResponse(BaseModel):
    id: str
    display_host: str
    confidence: SimilarityConfidence
    reason_codes: list[str]
    candidate_count: int
    occurrence_count: int
    first_source_sequence: int
    decision: SimilarityDecision | None
    canonical: BookmarkSimilarityCanonicalResponse
    sample_members: list[BookmarkSimilarityMemberResponse]
    has_more_members: bool


class BookmarkSimilarityClusterPageResponse(BaseModel):
    items: list[BookmarkSimilarityClusterResponse]
    next_cursor: str | None
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=50)
    total_count: int = Field(ge=0)
    total_pages: int = Field(ge=0)
    decision_version: int


class BookmarkSimilarityMemberPageResponse(BaseModel):
    items: list[BookmarkSimilarityMemberResponse]
    next_cursor: str | None
    decision_version: int


class BookmarkSimilarityDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_job_version: int = Field(ge=1)
    expected_decision_version: int = Field(ge=1)
    decision: SimilarityDecision


class BookmarkSimilarityResolveAllRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_job_version: int = Field(ge=1)
    expected_decision_version: int = Field(ge=1)
    decision: Literal["keep_originals"]


class BookmarkSimilarityDecisionResponse(BaseModel):
    job_id: str
    run_id: str
    job_version: int
    decision_version: int
    similarity_decision_counts: BookmarkSimilarityDecisionCounts
    selected_merge_reduction_count: int
    projected_create_count: int


class BookmarkPreviewFolderResponse(BaseModel):
    id: str
    parent_id: str | None
    source_sequence: int
    source_order: int
    depth: int
    title: str
    display_path: list[str]


class BookmarkPreviewCandidateResponse(BaseModel):
    id: str
    identity_url: str
    title: str
    host: str
    fetch_policy: FetchPolicy
    has_sensitive_url: bool
    proposed_action: ProposedAction
    occurrence_count: int
    first_source_sequence: int


class BookmarkPreviewOccurrenceResponse(BaseModel):
    id: str
    folder_id: str | None
    source_sequence: int
    source_order: int
    title: str
    url: str
    add_date: int | None
    last_modified: int | None
    validation_status: ValidationStatus
    fetch_policy: FetchPolicy | None
    reason_code: str | None
    has_sensitive_url: bool


class BookmarkPreviewFolderPageResponse(BaseModel):
    items: list[BookmarkPreviewFolderResponse]
    next_cursor: str | None


class BookmarkPreviewCandidatePageResponse(BaseModel):
    items: list[BookmarkPreviewCandidateResponse]
    next_cursor: str | None


class BookmarkPreviewOccurrencePageResponse(BaseModel):
    items: list[BookmarkPreviewOccurrenceResponse]
    next_cursor: str | None


class BookmarkImportApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_job_version: int = Field(ge=1)
    expected_decision_version: int = Field(default=1, ge=1)


class BookmarkImportApplyResponse(BaseModel):
    job_id: str
    state: str
    job_version: int
    total_candidates: int
    created: int
    skipped_existing: int
    skipped_needs_review: int
    merged_candidates: int
    failed: int
