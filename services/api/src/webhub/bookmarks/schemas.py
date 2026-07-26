from datetime import datetime
from typing import Literal

from pydantic import BaseModel

FetchPolicy = Literal["public_revalidation_required", "export_metadata_only"]
ProposedAction = Literal[
    "create",
    "skip_existing",
    "merge_missing_metadata",
    "reject",
    "needs_review",
]
ValidationStatus = Literal["accepted", "invalid", "unsupported"]
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
