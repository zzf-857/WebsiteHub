"""Contracts for optional LLM-backed website enrichment.

The ingestion package owns the workflow, but it must not depend on the Agent
implementation.  These immutable projections are the whole boundary: an
enricher receives page evidence plus account-scoped taxonomy options and
returns one validated draft.  It never receives a database session and never
writes storage itself.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from webhub.bookmarks.privacy import agent_safe_label

MIN_SITE_TAGS = 2
MAX_SITE_TAGS = 6
MAX_NEW_SITE_TAGS = 2
MIN_SITE_SUMMARY_CHARS = 20
MAX_SITE_SUMMARY_CHARS = 50
MIN_SITE_DESCRIPTION_CHARS = 100
MAX_SITE_DESCRIPTION_CHARS = 300
MIN_META_DESCRIPTION_EVIDENCE_CHARS = 24
MIN_PAGE_TEXT_EVIDENCE_CHARS = 80
MAX_BLOCK_PAGE_EVIDENCE_CHARS = 1_500
MAX_PROVIDER_RETRY_AFTER_SECONDS = 300

type SiteEnrichmentFailureReason = Literal[
    "provider_rate_limited",
    "provider_temporary_failure",
    "provider_unavailable",
    "internal_error",
]
_SITE_ENRICHMENT_FAILURE_REASONS = frozenset(
    {
        "provider_rate_limited",
        "provider_temporary_failure",
        "provider_unavailable",
        "internal_error",
    }
)

_BLOCK_PAGE_MARKERS = (
    "access denied",
    "checking your browser",
    "enable javascript and cookies",
    "just a moment",
    "log in to continue",
    "sign in to continue",
    "verify you are human",
    "访问被拒绝",
    "请启用javascript",
    "请启用 javascript",
    "请先登录",
    "人机验证",
    "安全验证",
    "登录后继续",
)

_MARKDOWN_PATTERN = re.compile(
    r"```|`[^`]+`|\[[^\]]+\]\([^)]+\)|"
    r"(?:^|\n)\s{0,3}(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s?|\|.*\|\s*$)|"
    r"(?:^|\n)(?: {4}|\t)\S|"
    r"(?:^|\n)\s*(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})\s*$|"
    r"(?:\*\*|__|~~)[^\n]+?(?:\*\*|__|~~)|"
    r"(?<![A-Za-z0-9])[*_][^\n*_]+[*_](?![A-Za-z0-9])",
    flags=re.MULTILINE,
)
_HTML_PATTERN = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")
_URL_PATTERN = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|www\.)|"
    r"(?<![A-Za-z0-9@._-])"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:com|net|org|io|ai|cn|com\.cn|dev|app|co|me|xyz|tech|cloud|site|info|biz|tv|cc|gov|edu)"
    r"(?::\d{1,5})?(?:/[^\s]*)?|"
    r"(?<![A-Za-z0-9@._-])"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}(?::\d{1,5})?/[^\s]+",
    flags=re.IGNORECASE,
)


def normalize_site_tag_name(value: str) -> tuple[str, str]:
    """Return the display and uniqueness forms of one model-proposed tag."""

    normalized = unicodedata.normalize("NFKC", value)
    if any(not character.isprintable() for character in normalized):
        raise ValueError("标签包含不可见字符")
    display = " ".join(normalized.split())
    if not display or len(display) > 40:
        raise ValueError("标签必须为 1 至 40 个字符")
    punctuation_trimmed = display.strip(" \t\r\n-:;,.()[]{}")
    if agent_safe_label(display, max_chars=40) != punctuation_trimmed:
        raise ValueError("标签不能包含 URL、路径、HTML 或敏感赋值")
    return display, display.casefold()


def normalize_site_description(value: str) -> str:
    """Normalize one focused 100-300 character detail and reject markup."""

    normalized = unicodedata.normalize("NFKC", value)
    if any(
        not character.isprintable() and character not in "\r\n\t"
        for character in normalized
    ):
        raise ValueError("介绍包含不可见字符")
    if _MARKDOWN_PATTERN.search(normalized):
        raise ValueError("介绍必须是纯文本，不能包含 Markdown")
    if _HTML_PATTERN.search(normalized):
        raise ValueError("介绍必须是纯文本，不能包含 HTML")
    if _URL_PATTERN.search(normalized):
        raise ValueError("介绍不能直接包含 URL")
    normalized = " ".join(normalized.split())
    if not MIN_SITE_DESCRIPTION_CHARS <= len(normalized) <= MAX_SITE_DESCRIPTION_CHARS:
        raise ValueError("介绍必须为 100 至 300 个字符")
    return normalized


def normalize_site_summary(value: str) -> str:
    """Normalize one 20-50 character sentence and reject rendered markup."""

    normalized = unicodedata.normalize("NFKC", value)
    if any(
        not character.isprintable() and character not in "\r\n\t"
        for character in normalized
    ):
        raise ValueError("摘要包含不可见字符")
    if _MARKDOWN_PATTERN.search(normalized):
        raise ValueError("摘要必须是纯文本，不能包含 Markdown")
    if _HTML_PATTERN.search(normalized):
        raise ValueError("摘要必须是纯文本，不能包含 HTML")
    if _URL_PATTERN.search(normalized):
        raise ValueError("摘要不能直接包含 URL")
    normalized = " ".join(normalized.split())
    sentence_body = normalized.rstrip("。！？!? .")
    if re.search(r"[。！？!?]", sentence_body) or re.search(
        r"(?<!\d)\.(?=\s+\S)",
        sentence_body,
    ):
        raise ValueError("摘要必须只包含一句话")
    if not MIN_SITE_SUMMARY_CHARS <= len(normalized) <= MAX_SITE_SUMMARY_CHARS:
        raise ValueError("摘要必须为 20 至 50 个字符")
    return normalized


class AnalysisIntent(StrEnum):
    METADATA_ONLY = "metadata_only"
    SITE_ENRICHMENT = "site_enrichment"


@dataclass(frozen=True, slots=True)
class SiteCategoryOption:
    id: str
    name: str
    is_default: bool = False


@dataclass(frozen=True, slots=True)
class SiteTagOption:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class SiteEnrichmentRequest:
    """A frozen site/taxonomy snapshot for one Provider call.

    The identity fields are backend guards.  Implementations must not include
    them in model messages or tool parameters.
    """

    user_id: str
    site_id: str
    expected_url: str
    expected_version: int
    hostname: str
    final_hostname: str | None
    site_name: str
    page_title: str
    meta_description: str
    page_text: str
    current_category_id: str
    current_tag_ids: tuple[str, ...]
    categories: tuple[SiteCategoryOption, ...]
    existing_tags: tuple[SiteTagOption, ...]
    bulk: bool = False

    @property
    def has_substantial_page_evidence(self) -> bool:
        """Require enough real page copy to support a 100+ character description."""

        meta_description = " ".join(self.meta_description.split())
        page_text = " ".join(self.page_text.split())
        combined = unicodedata.normalize(
            "NFKC",
            " ".join((self.page_title, meta_description, page_text)),
        ).casefold()
        if len(combined) <= MAX_BLOCK_PAGE_EVIDENCE_CHARS and any(
            marker in combined for marker in _BLOCK_PAGE_MARKERS
        ):
            return False
        return (
            len(meta_description) >= MIN_META_DESCRIPTION_EVIDENCE_CHARS
            or len(page_text) >= MIN_PAGE_TEXT_EVIDENCE_CHARS
        )


@dataclass(frozen=True, slots=True)
class SiteEnrichmentResult:
    category_id: str
    existing_tag_ids: tuple[str, ...]
    new_tag_names: tuple[str, ...]
    summary: str
    description: str


class SiteEnrichmentUnavailableError(RuntimeError):
    """A safe, vendor-independent enrichment failure."""

    def __init__(
        self,
        safe_message: str,
        *,
        stop_batch: bool = False,
        provider_failure: bool = False,
        failure_reason: SiteEnrichmentFailureReason | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        if (
            failure_reason is not None
            and failure_reason not in _SITE_ENRICHMENT_FAILURE_REASONS
        ):
            raise ValueError("unsupported site enrichment failure reason")
        self.safe_message = safe_message
        self.stop_batch = stop_batch
        self.provider_failure = provider_failure
        self.failure_reason = failure_reason
        self.retry_after_seconds = (
            min(retry_after_seconds, MAX_PROVIDER_RETRY_AFTER_SECONDS)
            if isinstance(retry_after_seconds, int)
            and not isinstance(retry_after_seconds, bool)
            and retry_after_seconds > 0
            else None
        )
        super().__init__(safe_message)


class SiteEnricher(Protocol):
    async def enrich(self, request: SiteEnrichmentRequest) -> SiteEnrichmentResult:
        """Return a complete in-memory draft or raise a safe failure."""


__all__ = [
    "AnalysisIntent",
    "MAX_NEW_SITE_TAGS",
    "MAX_PROVIDER_RETRY_AFTER_SECONDS",
    "MAX_SITE_DESCRIPTION_CHARS",
    "MAX_SITE_SUMMARY_CHARS",
    "MAX_SITE_TAGS",
    "MIN_SITE_DESCRIPTION_CHARS",
    "MIN_SITE_SUMMARY_CHARS",
    "MIN_SITE_TAGS",
    "SiteCategoryOption",
    "SiteEnricher",
    "SiteEnrichmentRequest",
    "SiteEnrichmentResult",
    "SiteEnrichmentFailureReason",
    "SiteEnrichmentUnavailableError",
    "SiteTagOption",
    "normalize_site_description",
    "normalize_site_summary",
    "normalize_site_tag_name",
]
