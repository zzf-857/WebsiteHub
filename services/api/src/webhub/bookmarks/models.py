from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class BookmarkFormatError(ValueError):
    """Raised when an export is unsafe or is not a supported bookmark document."""


class NormalizationStatus(StrEnum):
    ACCEPTED = "accepted"
    INVALID = "invalid"
    UNSUPPORTED = "unsupported"


class FetchPolicy(StrEnum):
    PUBLIC_REVALIDATION_REQUIRED = "public_revalidation_required"
    EXPORT_METADATA_ONLY = "export_metadata_only"


@dataclass(frozen=True, slots=True)
class ParserLimits:
    max_file_bytes: int = 512 * 1024 * 1024
    max_bookmarks: int = 500_000
    max_folders: int = 100_000
    max_folder_depth: int = 64
    max_url_chars: int = 16_384
    max_title_chars: int = 1_024
    max_folder_name_chars: int = 256
    max_buffered_tag_chars: int = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ParsedBookmark:
    position: int
    raw_url: str
    title: str
    folder_path: tuple[str, ...]
    source_folder_id: int | None
    add_date: int | None
    last_modified: int | None
    issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ParsedFolder:
    source_folder_id: int
    parent_source_folder_id: int | None
    source_order: int
    title: str
    folder_path: tuple[str, ...]
    depth: int


type BookmarkEvent = ParsedBookmark | ParsedFolder


@dataclass(slots=True)
class ParserStats:
    source_size_bytes: int = 0
    source_sha256: str = ""
    encoding: str = ""
    bookmark_count: int = 0
    folder_count: int = 0
    max_folder_depth: int = 0
    inline_icon_count: int = 0
    inline_icon_characters_ignored: int = 0
    invalid_timestamp_count: int = 0
    warning_count: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class NormalizedUrl:
    status: NormalizationStatus
    normalized_url: str | None
    host: str | None
    fetch_policy: FetchPolicy | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class CategorySuggestion:
    category: str
    confidence: str
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ImportPreview:
    output_directory: Path
    summary_path: Path
    candidates_path: Path
    occurrences_path: Path
    candidate_sources_path: Path
    source_folders_path: Path
    classification_clusters_path: Path
    rejected_path: Path
    staging_database_path: Path
    summary: dict[str, object]
