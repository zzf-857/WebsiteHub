from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from webhub.bookmarks.privacy import agent_safe_label

CLASSIFICATION_SCHEMA_VERSION = "webhub.bookmark-classification.v1"
CLASSIFICATION_VALIDATOR_VERSION = "bookmark-classification-validator.v1"
MAX_CLASSIFICATION_PAYLOAD_BYTES = 256 * 1024
MAX_CLASSIFICATION_BATCH_SIZE = 50
MAX_NEW_CATEGORIES_PER_BATCH = 20
LOW_CONFIDENCE_THRESHOLD = 0.5

CategoryAction = Literal["existing", "propose", "uncategorized"]
ReasonCode = Literal[
    "folder_match",
    "host_match",
    "title_match",
    "mixed_evidence",
    "insufficient_evidence",
]


class ClassificationOutputError(ValueError):
    """Raised when untrusted classifier output violates the import contract."""


class _DuplicateJsonKeyError(ValueError):
    pass


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKeyError(key)
        value[key] = item
    return value


def _normalized_label(value: str, *, field: str, max_length: int) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    if any(not character.isprintable() for character in normalized):
        raise ValueError(f"{field}包含不允许的不可见控制或格式字符")
    normalized = " ".join(normalized.split())
    if not normalized:
        raise ValueError(f"{field}不能为空")
    if len(normalized) > max_length:
        raise ValueError(f"{field}长度不能超过 {max_length}")
    punctuation_trimmed = normalized.strip(" \t\r\n-:;,.()[]{}")
    if agent_safe_label(normalized, max_chars=max_length) != punctuation_trimmed:
        raise ValueError(f"{field}包含不允许的 URL、路径、HTML 或敏感赋值")
    return normalized


def _opaque_identifier(value: str, *, field: str) -> str:
    has_whitespace = any(character.isspace() for character in value)
    if not value or value != value.strip() or len(value) > 128 or has_whitespace:
        raise ValueError(f"{field}不是有效的不透明标识")
    return value


class ClassificationMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    subject_id: str = Field(min_length=1, max_length=128)
    category_action: CategoryAction
    category_id: str | None = Field(max_length=128)
    category_name: str = Field(max_length=80)
    tags: tuple[str, ...] = Field(max_length=8)
    confidence: float = Field(ge=0, le=1)
    needs_review: bool
    reason_code: ReasonCode

    @field_validator("subject_id")
    @classmethod
    def validate_subject_id(cls, value: str) -> str:
        return _opaque_identifier(value, field="subject_id")

    @field_validator("category_id")
    @classmethod
    def validate_category_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _opaque_identifier(value, field="category_id")

    @field_validator("category_name")
    @classmethod
    def normalize_category_name(cls, value: str) -> str:
        return _normalized_label(value, field="category_name", max_length=80)

    @field_validator("tags", mode="before")
    @classmethod
    def accept_json_tag_array(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            label = _normalized_label(value, field="tag", max_length=40)
            key = label.casefold()
            if key in seen:
                raise ValueError("tags 规范化后不能重复")
            seen.add(key)
            normalized.append(label)
        return tuple(normalized)

    @model_validator(mode="after")
    def validate_category_shape(self) -> ClassificationMapping:
        if self.category_action == "existing":
            if self.category_id is None:
                raise ValueError("existing 分类必须提供 category_id")
        elif self.category_id is not None:
            raise ValueError("propose 或 uncategorized 分类不能提供 category_id")

        if self.category_action != "uncategorized" and len(self.tags) < 2:
            raise ValueError("existing 或 propose 分类必须提供 2 到 8 个标签")
        if self.category_action == "uncategorized":
            if self.category_name != "未分类":
                raise ValueError("uncategorized 分类名称必须是未分类")
            if not self.needs_review:
                raise ValueError("uncategorized 分类必须标记 needs_review")
        if self.category_action == "propose" and self.category_name == "未分类":
            raise ValueError("未分类不能作为新分类提案")
        if self.reason_code == "insufficient_evidence" and (
            self.category_action != "uncategorized" or not self.needs_review
        ):
            raise ValueError("insufficient_evidence 必须回退到未分类并标记复核")
        if self.confidence < LOW_CONFIDENCE_THRESHOLD and not self.needs_review:
            raise ValueError("低置信度分类必须标记 needs_review")
        return self


class ClassificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[CLASSIFICATION_SCHEMA_VERSION]
    batch_id: str = Field(min_length=1, max_length=128)
    mappings: tuple[ClassificationMapping, ...] = Field(max_length=MAX_CLASSIFICATION_BATCH_SIZE)

    @field_validator("batch_id")
    @classmethod
    def validate_batch_id(cls, value: str) -> str:
        return _opaque_identifier(value, field="batch_id")

    @field_validator("mappings", mode="before")
    @classmethod
    def accept_json_mapping_array(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


@dataclass(frozen=True, slots=True)
class ClassificationValidationResult:
    response: ClassificationResponse
    missing_subject_ids: tuple[str, ...]
    response_canonical_json: str
    binding_canonical_json: str
    binding_sha256: str


def _decode_payload(payload: str | bytes | Mapping[str, object]) -> Mapping[str, object]:
    if isinstance(payload, bytes):
        if len(payload) > MAX_CLASSIFICATION_PAYLOAD_BYTES:
            raise ClassificationOutputError("分类输出超过大小限制")
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ClassificationOutputError("分类输出必须是 UTF-8 JSON") from error

    if isinstance(payload, str):
        try:
            encoded_size = len(payload.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ClassificationOutputError("分类输出必须是 UTF-8 JSON") from error
        if encoded_size > MAX_CLASSIFICATION_PAYLOAD_BYTES:
            raise ClassificationOutputError("分类输出超过大小限制")
        try:
            decoded = json.loads(payload, object_pairs_hook=_object_without_duplicate_keys)
        except _DuplicateJsonKeyError as error:
            raise ClassificationOutputError("分类输出包含重复 JSON 键") from error
        except (ValueError, RecursionError) as error:
            raise ClassificationOutputError("分类输出不是有效 JSON") from error
    elif isinstance(payload, Mapping):
        decoded = dict(payload)
        try:
            encoded_size = len(
                json.dumps(decoded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
        except (TypeError, ValueError, RecursionError) as error:
            raise ClassificationOutputError("分类输出不是有效 JSON") from error
        if encoded_size > MAX_CLASSIFICATION_PAYLOAD_BYTES:
            raise ClassificationOutputError("分类输出超过大小限制")
    else:
        raise ClassificationOutputError("分类输出必须是 JSON 对象")

    if not isinstance(decoded, dict):
        raise ClassificationOutputError("分类输出顶层必须是 JSON 对象")
    return decoded


def _validated_subject_ids(subject_ids: Iterable[str]) -> tuple[str, ...]:
    values = tuple(_opaque_identifier(value, field="expected subject_id") for value in subject_ids)
    if not 1 <= len(values) <= MAX_CLASSIFICATION_BATCH_SIZE:
        raise ValueError("分类批次必须包含 1 到 50 个 subject_id")
    if len(set(values)) != len(values):
        raise ValueError("分类批次 subject_id 不能重复")
    return values


def _validated_categories(categories: Mapping[str, str]) -> dict[str, str]:
    validated: dict[str, str] = {}
    normalized_names: set[str] = set()
    for category_id, category_name in categories.items():
        identifier = _opaque_identifier(category_id, field="allowed category_id")
        name = _normalized_label(category_name, field="allowed category_name", max_length=80)
        name_key = name.casefold()
        if name_key in normalized_names:
            raise ValueError("允许分类名称规范化后不能重复")
        normalized_names.add(name_key)
        validated[identifier] = name
    return validated


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def validate_classification_output(
    payload: str | bytes | Mapping[str, object],
    *,
    expected_batch_id: str,
    expected_subject_ids: Iterable[str],
    allowed_categories: Mapping[str, str],
    max_new_categories: int,
) -> ClassificationValidationResult:
    """Validate and canonicalize one untrusted classifier response.

    Missing subjects are returned to the caller for deterministic ``未分类/待复核`` fallback.
    Unknown or duplicate subjects are rejected so a model cannot escape the bounded batch.
    """

    batch_id = _opaque_identifier(expected_batch_id, field="expected batch_id")
    subject_ids = _validated_subject_ids(expected_subject_ids)
    categories = _validated_categories(allowed_categories)
    if (
        isinstance(max_new_categories, bool)
        or not isinstance(max_new_categories, int)
        or not 0 <= max_new_categories <= MAX_NEW_CATEGORIES_PER_BATCH
    ):
        raise ValueError("max_new_categories 必须在 0 到 20 之间")

    decoded = _decode_payload(payload)
    try:
        response = ClassificationResponse.model_validate(decoded)
    except ValidationError as error:
        raise ClassificationOutputError("分类输出结构不符合约定") from error

    if response.batch_id != batch_id:
        raise ClassificationOutputError("分类输出 batch_id 与请求不一致")

    expected_set = set(subject_ids)
    seen_subjects: set[str] = set()
    proposed_names: set[str] = set()
    allowed_name_keys = {name.casefold() for name in categories.values()}
    for mapping in response.mappings:
        if mapping.subject_id not in expected_set:
            raise ClassificationOutputError("分类输出包含批次之外的 subject_id")
        if mapping.subject_id in seen_subjects:
            raise ClassificationOutputError("分类输出 subject_id 重复")
        seen_subjects.add(mapping.subject_id)

        if mapping.category_action == "existing":
            assert mapping.category_id is not None
            allowed_name = categories.get(mapping.category_id)
            if allowed_name is None:
                raise ClassificationOutputError("分类输出引用了未知 category_id")
            if mapping.category_name != allowed_name:
                raise ClassificationOutputError("分类输出的 category_id 与名称不匹配")
        elif mapping.category_action == "propose":
            name_key = mapping.category_name.casefold()
            if name_key in allowed_name_keys:
                raise ClassificationOutputError("已有分类必须通过 category_id 引用")
            proposed_names.add(name_key)

    if len(proposed_names) > max_new_categories:
        raise ClassificationOutputError("分类输出超过新分类预算")

    missing = tuple(subject_id for subject_id in subject_ids if subject_id not in seen_subjects)
    response_canonical_json = _canonical_json(response.model_dump(mode="json"))
    binding_canonical_json = _canonical_json(
        {
            "validator_version": CLASSIFICATION_VALIDATOR_VERSION,
            "schema_version": response.schema_version,
            "batch_id": response.batch_id,
            "expected_subject_ids": sorted(subject_ids),
            "missing_subject_ids": sorted(missing),
            "allowed_categories": [
                {"category_id": category_id, "category_name": category_name}
                for category_id, category_name in sorted(categories.items())
            ],
            "max_new_categories": max_new_categories,
            "mappings": [
                mapping.model_dump(mode="json")
                for mapping in sorted(response.mappings, key=lambda item: item.subject_id)
            ],
        }
    )
    return ClassificationValidationResult(
        response=response,
        missing_subject_ids=missing,
        response_canonical_json=response_canonical_json,
        binding_canonical_json=binding_canonical_json,
        binding_sha256=hashlib.sha256(binding_canonical_json.encode("utf-8")).hexdigest(),
    )
