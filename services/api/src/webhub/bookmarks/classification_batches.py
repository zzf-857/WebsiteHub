from __future__ import annotations

import ipaddress
import json
import re
import secrets
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from webhub.bookmarks.classification import meaningful_folder_path
from webhub.bookmarks.classification_contract import (
    CLASSIFICATION_SCHEMA_VERSION,
    MAX_CLASSIFICATION_BATCH_SIZE,
    ClassificationMapping,
    ClassificationValidationResult,
    validate_classification_output,
)
from webhub.bookmarks.models import FetchPolicy
from webhub.bookmarks.privacy import agent_safe_label

CLASSIFICATION_INPUT_SCHEMA_VERSION = "webhub.bookmark-classification-input.v1"
MAX_CLASSIFICATION_INPUT_BYTES = 256 * 1024
MAX_FOLDER_LABELS_PER_SUBJECT = 8
MAX_SAMPLE_TITLES_PER_CLUSTER = 8
MAX_SAMPLE_HOSTNAMES_PER_CLUSTER = 8
MAX_ALLOWED_TAGS = 512

ClassificationSubjectKind = Literal["folder_cluster", "candidate"]

_LANGUAGE_TAG_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LOCAL_HOST_SUFFIXES = (".internal", ".lan", ".local", ".localhost", ".home")


class ClassificationProjectionError(ValueError):
    """Raised when trusted backend inputs cannot form a safe classifier projection."""


@dataclass(frozen=True, slots=True)
class ClassificationCandidateSource:
    """Backend-owned candidate facts used to derive a sanitized model projection.

    ``source_id`` is an internal binding and is never serialized into Provider input.
    """

    source_id: str
    title: str
    hostname: str
    folder_labels: tuple[str, ...] = ()
    fetch_policy: FetchPolicy | str = FetchPolicy.PUBLIC_REVALIDATION_REQUIRED
    has_sensitive_url: bool = False
    occurrence_count: int = 1


@dataclass(frozen=True, slots=True)
class FolderClusterSource:
    """Backend-owned folder cluster and the candidates that contribute evidence."""

    source_id: str
    folder_labels: tuple[str, ...]
    candidates: tuple[ClassificationCandidateSource, ...]


@dataclass(frozen=True, slots=True)
class ClassificationBatchBudget:
    """Hard call and serialized-input limits for one classification planning pass."""

    max_batches: int
    max_total_payload_bytes: int
    max_payload_bytes_per_batch: int = 64 * 1024
    max_subjects_per_batch: int = MAX_CLASSIFICATION_BATCH_SIZE

    def __post_init__(self) -> None:
        _bounded_integer(self.max_batches, field="max_batches", minimum=0, maximum=10_000)
        _bounded_integer(
            self.max_total_payload_bytes,
            field="max_total_payload_bytes",
            minimum=1,
            maximum=MAX_CLASSIFICATION_INPUT_BYTES * 10_000,
        )
        _bounded_integer(
            self.max_payload_bytes_per_batch,
            field="max_payload_bytes_per_batch",
            minimum=1,
            maximum=MAX_CLASSIFICATION_INPUT_BYTES,
        )
        _bounded_integer(
            self.max_subjects_per_batch,
            field="max_subjects_per_batch",
            minimum=1,
            maximum=MAX_CLASSIFICATION_BATCH_SIZE,
        )


@dataclass(frozen=True, slots=True)
class _ProjectedFolderSubject:
    source_id: str
    folder_labels: tuple[str, ...]
    link_count: int
    sample_titles: tuple[str, ...]
    sample_hostnames: tuple[str, ...]

    def provider_value(self, subject_id: str) -> dict[str, object]:
        return {
            "subject_id": subject_id,
            "folder_labels": list(self.folder_labels),
            "link_count": self.link_count,
            "sample_titles": list(self.sample_titles),
            "sample_hostnames": list(self.sample_hostnames),
        }


@dataclass(frozen=True, slots=True)
class _ProjectedCandidateSubject:
    source_id: str
    title: str | None
    hostname: str
    folder_labels: tuple[str, ...]

    def provider_value(self, subject_id: str) -> dict[str, object]:
        value: dict[str, object] = {
            "subject_id": subject_id,
            "hostname": self.hostname,
            "folder_labels": list(self.folder_labels),
        }
        if self.title is not None:
            value["title"] = self.title
        return value


type _ProjectedSubject = _ProjectedFolderSubject | _ProjectedCandidateSubject


@dataclass(frozen=True, slots=True)
class ClassificationSubjectBinding:
    subject_id: str
    source_id: str


@dataclass(frozen=True, slots=True)
class ClassificationBatch:
    """One bounded Provider payload plus its backend-only subject bindings."""

    subject_kind: ClassificationSubjectKind
    batch_id: str
    subjects: tuple[Mapping[str, object], ...]
    bindings: tuple[ClassificationSubjectBinding, ...]
    allowed_categories: tuple[tuple[str, str], ...]
    allowed_tags: tuple[str, ...]
    include_tags: bool
    max_new_categories: int
    requested_language: str
    payload_bytes: int

    @property
    def expected_subject_ids(self) -> tuple[str, ...]:
        return tuple(binding.subject_id for binding in self.bindings)

    def provider_payload(self) -> dict[str, object]:
        """Return only the bounded projection that may cross the Provider boundary."""
        return _provider_payload(
            subject_kind=self.subject_kind,
            batch_id=self.batch_id,
            subjects=self.subjects,
            allowed_categories=self.allowed_categories,
            allowed_tags=self.allowed_tags,
            include_tags=self.include_tags,
            max_new_categories=self.max_new_categories,
            requested_language=self.requested_language,
        )

    def source_id_for(self, subject_id: str) -> str:
        for binding in self.bindings:
            if binding.subject_id == subject_id:
                return binding.source_id
        raise KeyError(subject_id)


@dataclass(frozen=True, slots=True)
class BoundClassificationMapping:
    source_id: str
    mapping: ClassificationMapping
    used_fallback: bool


@dataclass(frozen=True, slots=True)
class ValidatedClassificationBatch:
    validation: ClassificationValidationResult
    mappings: tuple[BoundClassificationMapping, ...]
    unresolved_source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClassificationBatchPlan:
    batches: tuple[ClassificationBatch, ...]
    privacy_excluded_source_ids: tuple[str, ...]
    privacy_excluded_member_source_ids: tuple[str, ...]
    budget_exhausted_source_ids: tuple[str, ...]
    total_payload_bytes: int


def _bounded_integer(value: int, *, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ClassificationProjectionError(
            f"{field} must be an integer between {minimum} and {maximum}"
        )
    return value


def _source_identifier(value: str, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 128
        or any(character.isspace() for character in value)
    ):
        raise ClassificationProjectionError(f"{field} must be an opaque backend identifier")
    return value


def _opaque_identifier(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def _normalized_taxonomy(
    categories: Mapping[str, str],
    *,
    max_new_categories: int,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(categories, Mapping):
        raise ClassificationProjectionError("allowed_categories must be a mapping")
    normalized: list[tuple[str, str]] = []
    seen_names: set[str] = set()
    for category_id, raw_name in categories.items():
        identifier = _source_identifier(category_id, field="category_id")
        if not isinstance(raw_name, str):
            raise ClassificationProjectionError("category_name must be text")
        name = unicodedata.normalize("NFKC", raw_name)
        if any(not character.isprintable() for character in name):
            raise ClassificationProjectionError("category_name contains invisible characters")
        name = " ".join(name.split())
        if not name or len(name) > 80:
            raise ClassificationProjectionError("category_name must contain 1 to 80 characters")
        punctuation_trimmed = name.strip(" \t\r\n-:;,.()[]{}")
        if agent_safe_label(name, max_chars=80) != punctuation_trimmed:
            raise ClassificationProjectionError("category_name contains unsafe text")
        name_key = name.casefold()
        if name_key in seen_names:
            raise ClassificationProjectionError("category names must be unique after normalization")
        seen_names.add(name_key)
        normalized.append((identifier, name))

    _bounded_integer(
        max_new_categories,
        field="max_new_categories",
        minimum=0,
        maximum=20,
    )
    return tuple(sorted(normalized))


def _normalized_tags(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ClassificationProjectionError("allowed_tags must be a sequence of labels")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        if not isinstance(raw_value, str):
            raise ClassificationProjectionError("allowed tag must be text")
        value = unicodedata.normalize("NFKC", raw_value)
        if any(not character.isprintable() for character in value):
            raise ClassificationProjectionError("allowed tag contains invisible characters")
        value = " ".join(value.split())
        if not value or len(value) > 40:
            raise ClassificationProjectionError("allowed tags must contain 1 to 40 characters")
        punctuation_trimmed = value.strip(" \t\r\n-:;,.()[]{}")
        if agent_safe_label(value, max_chars=40) != punctuation_trimmed:
            raise ClassificationProjectionError("allowed tag contains unsafe text")
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(value)
        if len(normalized) > MAX_ALLOWED_TAGS:
            raise ClassificationProjectionError(
                f"allowed_tags cannot contain more than {MAX_ALLOWED_TAGS} labels"
            )
    return tuple(sorted(normalized, key=str.casefold))


def _requested_language(value: str) -> str:
    if not isinstance(value, str) or not _LANGUAGE_TAG_RE.fullmatch(value):
        raise ClassificationProjectionError("requested_language must be a BCP 47 language tag")
    return value


def _validated_title(value: str) -> str:
    if not isinstance(value, str):
        raise ClassificationProjectionError("candidate title must be text")
    return value


def _safe_hostname(value: str) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 255:
        return None
    comparable = value.casefold().rstrip(".")
    if not comparable or any(character.isspace() for character in comparable):
        return None
    if any(marker in comparable for marker in ("/", "\\", "?", "#", "@")):
        return None

    address_text = comparable.partition("%")[0]
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError:
        try:
            hostname = comparable.encode("idna").decode("ascii")
        except UnicodeError:
            return None
        if len(hostname) > 253 or "." not in hostname:
            return None
        labels = hostname.split(".")
        if any(not _DNS_LABEL_RE.fullmatch(label) for label in labels):
            return None
        if hostname == "localhost" or hostname.endswith(_LOCAL_HOST_SUFFIXES):
            return None
        return hostname
    return address.compressed if address.is_global else None


def _validated_candidate(candidate: ClassificationCandidateSource) -> tuple[str, str] | None:
    _source_identifier(candidate.source_id, field="candidate source_id")
    _validated_title(candidate.title)
    if not isinstance(candidate.has_sensitive_url, bool):
        raise ClassificationProjectionError("has_sensitive_url must be boolean")
    _bounded_integer(
        candidate.occurrence_count,
        field="occurrence_count",
        minimum=1,
        maximum=500_000,
    )
    try:
        fetch_policy = FetchPolicy(candidate.fetch_policy)
    except (TypeError, ValueError) as error:
        raise ClassificationProjectionError("candidate fetch_policy is invalid") from error
    if candidate.has_sensitive_url or fetch_policy is FetchPolicy.EXPORT_METADATA_ONLY:
        return None
    hostname = _safe_hostname(candidate.hostname)
    if hostname is None:
        return None
    return candidate.source_id, hostname


def _safe_folder_labels(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ClassificationProjectionError("folder labels must be a sequence")
    safe: list[str] = []
    raw_values = tuple(values)
    if any(not isinstance(value, str) for value in raw_values):
        raise ClassificationProjectionError("folder labels must be text")
    for value in meaningful_folder_path(raw_values):
        label = agent_safe_label(value, max_chars=128)
        if label is not None:
            safe.append(label)
        if len(safe) == MAX_FOLDER_LABELS_PER_SUBJECT:
            break
    return tuple(safe)


def _append_unique(values: list[str], value: str | None, *, maximum: int) -> None:
    if value is None or len(values) >= maximum:
        return
    key = value.casefold()
    if any(existing.casefold() == key for existing in values):
        return
    values.append(value)


def _project_folder_clusters(
    clusters: Sequence[FolderClusterSource],
) -> tuple[tuple[_ProjectedFolderSubject, ...], tuple[str, ...], tuple[str, ...]]:
    if isinstance(clusters, (str, bytes)) or not isinstance(clusters, Sequence):
        raise ClassificationProjectionError("folder clusters must be a sequence")
    projected: list[_ProjectedFolderSubject] = []
    excluded: list[str] = []
    excluded_members: list[str] = []
    seen_source_ids: set[str] = set()
    for cluster in clusters:
        source_id = _source_identifier(cluster.source_id, field="folder cluster source_id")
        if source_id in seen_source_ids:
            raise ClassificationProjectionError("folder cluster source_id values must be unique")
        seen_source_ids.add(source_id)
        if isinstance(cluster.candidates, (str, bytes)) or not isinstance(
            cluster.candidates, Sequence
        ):
            raise ClassificationProjectionError("folder cluster candidates must be a sequence")
        link_count = 0
        sample_titles: list[str] = []
        sample_hostnames: list[str] = []
        seen_candidate_ids: set[str] = set()
        for candidate in cluster.candidates:
            candidate_source_id = _source_identifier(
                candidate.source_id,
                field="candidate source_id",
            )
            if candidate_source_id in seen_candidate_ids:
                raise ClassificationProjectionError(
                    "candidate source_id values must be unique within a folder cluster"
                )
            seen_candidate_ids.add(candidate_source_id)
            validated = _validated_candidate(candidate)
            if validated is None:
                excluded_members.append(candidate_source_id)
                continue
            _, hostname = validated
            link_count += candidate.occurrence_count
            _append_unique(
                sample_titles,
                agent_safe_label(candidate.title, max_chars=256),
                maximum=MAX_SAMPLE_TITLES_PER_CLUSTER,
            )
            _append_unique(
                sample_hostnames,
                hostname,
                maximum=MAX_SAMPLE_HOSTNAMES_PER_CLUSTER,
            )

        if link_count == 0:
            excluded.append(source_id)
            continue
        projected.append(
            _ProjectedFolderSubject(
                source_id=source_id,
                folder_labels=_safe_folder_labels(cluster.folder_labels),
                link_count=link_count,
                sample_titles=tuple(sample_titles),
                sample_hostnames=tuple(sample_hostnames),
            )
        )
    return tuple(projected), tuple(excluded), tuple(excluded_members)


def _project_candidates(
    candidates: Sequence[ClassificationCandidateSource],
) -> tuple[tuple[_ProjectedCandidateSubject, ...], tuple[str, ...], tuple[str, ...]]:
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        raise ClassificationProjectionError("candidates must be a sequence")
    projected: list[_ProjectedCandidateSubject] = []
    excluded: list[str] = []
    seen_source_ids: set[str] = set()
    for candidate in candidates:
        source_id = _source_identifier(candidate.source_id, field="candidate source_id")
        if source_id in seen_source_ids:
            raise ClassificationProjectionError("candidate source_id values must be unique")
        seen_source_ids.add(source_id)
        validated = _validated_candidate(candidate)
        if validated is None:
            excluded.append(source_id)
            continue
        _, hostname = validated
        projected.append(
            _ProjectedCandidateSubject(
                source_id=source_id,
                title=agent_safe_label(candidate.title, max_chars=256),
                hostname=hostname,
                folder_labels=_safe_folder_labels(candidate.folder_labels),
            )
        )
    return tuple(projected), tuple(excluded), tuple(excluded)


def _provider_payload(
    *,
    subject_kind: ClassificationSubjectKind,
    batch_id: str,
    subjects: Sequence[Mapping[str, object]],
    allowed_categories: Sequence[tuple[str, str]],
    allowed_tags: Sequence[str],
    include_tags: bool,
    max_new_categories: int,
    requested_language: str,
) -> dict[str, object]:
    return {
        "schema_version": CLASSIFICATION_INPUT_SCHEMA_VERSION,
        "output_schema_version": CLASSIFICATION_SCHEMA_VERSION,
        "batch_id": batch_id,
        "subject_kind": subject_kind,
        "subjects": [_clone_json_value(subject) for subject in subjects],
        "allowed_categories": [
            {"category_id": category_id, "category_name": category_name}
            for category_id, category_name in allowed_categories
        ],
        "allowed_tags": list(allowed_tags),
        "include_tags": include_tags,
        "max_new_categories": max_new_categories,
        "requested_language": requested_language,
    }


def _clone_json_value(value: object) -> object:
    """Deep-copy projection values so a Provider cannot mutate a cached batch payload."""
    if isinstance(value, Mapping):
        return {str(key): _clone_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clone_json_value(item) for item in value]
    return value


def _payload_size(value: Mapping[str, object]) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _plan_batches(
    subjects: Sequence[_ProjectedSubject],
    *,
    privacy_excluded_source_ids: tuple[str, ...],
    privacy_excluded_member_source_ids: tuple[str, ...],
    subject_kind: ClassificationSubjectKind,
    allowed_categories: tuple[tuple[str, str], ...],
    allowed_tags: tuple[str, ...],
    include_tags: bool,
    max_new_categories: int,
    requested_language: str,
    budget: ClassificationBatchBudget,
) -> ClassificationBatchPlan:
    if not isinstance(include_tags, bool):
        raise ClassificationProjectionError("include_tags must be boolean")
    batches: list[ClassificationBatch] = []
    budget_exhausted: list[str] = []
    total_payload_bytes = 0
    subject_index = 0

    while subject_index < len(subjects):
        if len(batches) >= budget.max_batches:
            budget_exhausted.extend(item.source_id for item in subjects[subject_index:])
            break

        batch_id = _opaque_identifier("batch")
        provider_subjects: list[Mapping[str, object]] = []
        bindings: list[ClassificationSubjectBinding] = []

        while (
            subject_index < len(subjects) and len(provider_subjects) < budget.max_subjects_per_batch
        ):
            projected = subjects[subject_index]
            subject_id = _opaque_identifier("subject")
            provider_subject = projected.provider_value(subject_id)
            candidate_subjects = (*provider_subjects, provider_subject)
            candidate_payload = _provider_payload(
                subject_kind=subject_kind,
                batch_id=batch_id,
                subjects=candidate_subjects,
                allowed_categories=allowed_categories,
                allowed_tags=allowed_tags,
                include_tags=include_tags,
                max_new_categories=max_new_categories,
                requested_language=requested_language,
            )
            candidate_size = _payload_size(candidate_payload)
            fits_batch = candidate_size <= budget.max_payload_bytes_per_batch
            fits_total = total_payload_bytes + candidate_size <= budget.max_total_payload_bytes
            if fits_batch and fits_total:
                provider_subjects.append(provider_subject)
                bindings.append(
                    ClassificationSubjectBinding(
                        subject_id=subject_id,
                        source_id=projected.source_id,
                    )
                )
                subject_index += 1
                continue

            if provider_subjects:
                break
            budget_exhausted.append(projected.source_id)
            subject_index += 1

        if not provider_subjects:
            continue

        payload = _provider_payload(
            subject_kind=subject_kind,
            batch_id=batch_id,
            subjects=provider_subjects,
            allowed_categories=allowed_categories,
            allowed_tags=allowed_tags,
            include_tags=include_tags,
            max_new_categories=max_new_categories,
            requested_language=requested_language,
        )
        payload_bytes = _payload_size(payload)
        batch = ClassificationBatch(
            subject_kind=subject_kind,
            batch_id=batch_id,
            subjects=tuple(provider_subjects),
            bindings=tuple(bindings),
            allowed_categories=allowed_categories,
            allowed_tags=allowed_tags,
            include_tags=include_tags,
            max_new_categories=max_new_categories,
            requested_language=requested_language,
            payload_bytes=payload_bytes,
        )
        # Validate the exact scope now, before any Provider call can consume the batch.
        validate_classification_output(
            {
                "schema_version": CLASSIFICATION_SCHEMA_VERSION,
                "batch_id": batch.batch_id,
                "mappings": [],
            },
            expected_batch_id=batch.batch_id,
            expected_subject_ids=batch.expected_subject_ids,
            allowed_categories=dict(batch.allowed_categories),
            max_new_categories=batch.max_new_categories,
            tags_disabled=not batch.include_tags,
        )
        batches.append(batch)
        total_payload_bytes += payload_bytes

    return ClassificationBatchPlan(
        batches=tuple(batches),
        privacy_excluded_source_ids=privacy_excluded_source_ids,
        privacy_excluded_member_source_ids=privacy_excluded_member_source_ids,
        budget_exhausted_source_ids=tuple(budget_exhausted),
        total_payload_bytes=total_payload_bytes,
    )


def build_folder_classification_batches(
    clusters: Sequence[FolderClusterSource],
    *,
    allowed_categories: Mapping[str, str],
    allowed_tags: Sequence[str] = (),
    include_tags: bool = True,
    max_new_categories: int,
    requested_language: str,
    budget: ClassificationBatchBudget,
) -> ClassificationBatchPlan:
    """Build bounded folder-first batches without exposing staging identifiers or URLs."""
    taxonomy = _normalized_taxonomy(
        allowed_categories,
        max_new_categories=max_new_categories,
    )
    tags = _normalized_tags(allowed_tags)
    language = _requested_language(requested_language)
    projected, excluded, excluded_members = _project_folder_clusters(clusters)
    return _plan_batches(
        projected,
        privacy_excluded_source_ids=excluded,
        privacy_excluded_member_source_ids=excluded_members,
        subject_kind="folder_cluster",
        allowed_categories=taxonomy,
        allowed_tags=tags,
        include_tags=include_tags,
        max_new_categories=max_new_categories,
        requested_language=language,
        budget=budget,
    )


def build_candidate_classification_batches(
    candidates: Sequence[ClassificationCandidateSource],
    *,
    allowed_categories: Mapping[str, str],
    allowed_tags: Sequence[str] = (),
    include_tags: bool = True,
    requested_language: str,
    budget: ClassificationBatchBudget,
) -> ClassificationBatchPlan:
    """Build the ambiguous-candidate pass; new category proposals are disabled here."""
    taxonomy = _normalized_taxonomy(allowed_categories, max_new_categories=0)
    tags = _normalized_tags(allowed_tags)
    language = _requested_language(requested_language)
    projected, excluded, excluded_members = _project_candidates(candidates)
    return _plan_batches(
        projected,
        privacy_excluded_source_ids=excluded,
        privacy_excluded_member_source_ids=excluded_members,
        subject_kind="candidate",
        allowed_categories=taxonomy,
        allowed_tags=tags,
        include_tags=include_tags,
        max_new_categories=0,
        requested_language=language,
        budget=budget,
    )


def validate_classification_batch_output(
    batch: ClassificationBatch,
    payload: str | bytes | Mapping[str, object],
) -> ValidatedClassificationBatch:
    """Validate untrusted output, bind it locally, and materialize every missing fallback."""
    validation = validate_classification_output(
        payload,
        expected_batch_id=batch.batch_id,
        expected_subject_ids=batch.expected_subject_ids,
        allowed_categories=dict(batch.allowed_categories),
        max_new_categories=batch.max_new_categories,
        tags_disabled=not batch.include_tags,
    )
    mappings_by_subject = {mapping.subject_id: mapping for mapping in validation.response.mappings}
    resolved: list[BoundClassificationMapping] = []
    unresolved: list[str] = []
    for binding in batch.bindings:
        mapping = mappings_by_subject.get(binding.subject_id)
        used_fallback = mapping is None
        if mapping is None:
            mapping = ClassificationMapping(
                subject_id=binding.subject_id,
                category_action="uncategorized",
                category_id=None,
                category_name="未分类",
                tags=(),
                confidence=0.0,
                needs_review=True,
                reason_code="insufficient_evidence",
            )
            unresolved.append(binding.source_id)
        resolved.append(
            BoundClassificationMapping(
                source_id=binding.source_id,
                mapping=mapping,
                used_fallback=used_fallback,
            )
        )

    return ValidatedClassificationBatch(
        validation=validation,
        mappings=tuple(resolved),
        unresolved_source_ids=tuple(unresolved),
    )
