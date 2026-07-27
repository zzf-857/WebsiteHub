import json

import pytest
from pydantic import ValidationError

from webhub.bookmarks.classification_contract import (
    CLASSIFICATION_SCHEMA_VERSION,
    CLASSIFICATION_VALIDATOR_VERSION,
    LOW_CONFIDENCE_THRESHOLD,
    MAX_CLASSIFICATION_PAYLOAD_BYTES,
    ClassificationOutputError,
    validate_classification_output,
)


def _mapping(
    subject_id: str,
    *,
    action: str = "existing",
    category_id: str | None = "category-development",
    category_name: str = "开发与技术",
    tags: list[str] | None = None,
    confidence: float = 0.91,
    needs_review: bool = False,
    reason_code: str = "mixed_evidence",
) -> dict[str, object]:
    return {
        "subject_id": subject_id,
        "category_action": action,
        "category_id": category_id,
        "category_name": category_name,
        "tags": tags if tags is not None else ["Python", "文档"],
        "confidence": confidence,
        "needs_review": needs_review,
        "reason_code": reason_code,
    }


def _payload(*mappings: dict[str, object], batch_id: str = "batch-001") -> dict[str, object]:
    return {
        "schema_version": CLASSIFICATION_SCHEMA_VERSION,
        "batch_id": batch_id,
        "mappings": list(mappings),
    }


def _validate(
    payload: object,
    *,
    max_new_categories: int = 2,
    expected_batch_id: str = "batch-001",
    expected_subject_ids: tuple[str, ...] = ("cluster-1", "cluster-2", "cluster-3"),
    allowed_categories: dict[str, str] | None = None,
):
    return validate_classification_output(
        payload,  # type: ignore[arg-type]
        expected_batch_id=expected_batch_id,
        expected_subject_ids=expected_subject_ids,
        allowed_categories=allowed_categories
        or {
            "category-development": "开发与技术",
            "category-learning": "学习与文档",
        },
        max_new_categories=max_new_categories,
    )


def test_validates_and_canonicalizes_mixed_category_actions() -> None:
    payload = _payload(
        _mapping("cluster-1"),
        _mapping(
            "cluster-2",
            action="propose",
            category_id=None,
            category_name="  研究  资料  ",
            tags=["Paper", "论文"],
        ),
        _mapping(
            "cluster-3",
            action="uncategorized",
            category_id=None,
            category_name="未分类",
            tags=[],
            needs_review=True,
        ),
    )

    result = _validate(json.dumps(payload, ensure_ascii=False))

    assert result.missing_subject_ids == ()
    assert result.response.mappings[1].category_name == "研究 资料"
    assert len(result.binding_sha256) == 64
    assert json.loads(result.response_canonical_json)["mappings"][1]["category_name"] == "研究 资料"
    assert json.loads(result.binding_canonical_json)["missing_subject_ids"] == []


def test_validated_response_is_deeply_immutable_but_dumps_json_arrays() -> None:
    result = _validate(_payload(_mapping("cluster-1")))
    mapping = result.response.mappings[0]

    assert isinstance(result.response.mappings, tuple)
    assert isinstance(mapping.tags, tuple)
    dumped = result.response.model_dump(mode="json")
    assert isinstance(dumped["mappings"], list)
    assert isinstance(dumped["mappings"][0]["tags"], list)

    with pytest.raises(AttributeError):
        result.response.mappings.append(mapping)  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        mapping.tags[0] = "changed"  # type: ignore[index]
    with pytest.raises(ValidationError, match="frozen"):
        mapping.tags = ()  # type: ignore[misc]


def test_returns_missing_subjects_for_deterministic_fallback() -> None:
    result = _validate(_payload(_mapping("cluster-2")))

    assert result.missing_subject_ids == ("cluster-1", "cluster-3")


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        b"\xff",
        {"schema_version": CLASSIFICATION_SCHEMA_VERSION, "batch_id": "batch-001"},
        {
            "schema_version": CLASSIFICATION_SCHEMA_VERSION,
            "batch_id": "batch-001",
            "mappings": [],
            "unexpected": True,
        },
    ],
)
def test_rejects_non_json_or_invalid_structure(payload: object) -> None:
    with pytest.raises(ClassificationOutputError):
        _validate(payload)


def test_rejects_payload_over_size_limit() -> None:
    payload = "{" + (" " * MAX_CLASSIFICATION_PAYLOAD_BYTES) + "}"

    with pytest.raises(ClassificationOutputError, match="大小限制"):
        _validate(payload)


def test_wraps_json_integer_digit_limit_as_classification_error() -> None:
    payload = json.dumps(_payload(_mapping("cluster-1")), separators=(",", ":"))
    payload = payload.replace('"confidence":0.91', f'"confidence":{"9" * 5_000}', 1)

    with pytest.raises(ClassificationOutputError, match="有效 JSON"):
        _validate(payload)


def test_rejects_duplicate_keys_at_any_json_depth() -> None:
    payload = json.dumps(_payload(_mapping("cluster-1")), separators=(",", ":"))
    duplicate_top_level = payload.replace(
        '"batch_id":"batch-001"',
        '"batch_id":"batch-001","batch_id":"batch-001"',
        1,
    )
    duplicate_nested = payload.replace(
        '"subject_id":"cluster-1"',
        '"subject_id":"cluster-1","subject_id":"cluster-1"',
        1,
    )

    for untrusted in (duplicate_top_level, duplicate_nested.encode("utf-8")):
        with pytest.raises(ClassificationOutputError, match="重复 JSON 键"):
            _validate(untrusted)


def test_rejects_batch_escape_duplicate_subject_and_batch_mismatch() -> None:
    with pytest.raises(ClassificationOutputError, match="批次之外"):
        _validate(_payload(_mapping("other-cluster")))
    with pytest.raises(ClassificationOutputError, match="重复"):
        _validate(_payload(_mapping("cluster-1"), _mapping("cluster-1")))
    with pytest.raises(ClassificationOutputError, match="batch_id"):
        _validate(_payload(_mapping("cluster-1"), batch_id="batch-other"))


def test_rejects_unknown_or_mismatched_existing_category() -> None:
    with pytest.raises(ClassificationOutputError, match="未知 category_id"):
        _validate(
            _payload(
                _mapping(
                    "cluster-1",
                    category_id="category-other",
                    category_name="其他",
                )
            )
        )
    with pytest.raises(ClassificationOutputError, match="名称不匹配"):
        _validate(_payload(_mapping("cluster-1", category_name="学习与文档")))


def test_rejects_new_category_that_exists_or_exceeds_budget() -> None:
    with pytest.raises(ClassificationOutputError, match="已有分类"):
        _validate(
            _payload(
                _mapping(
                    "cluster-1",
                    action="propose",
                    category_id=None,
                    category_name="开发与技术",
                )
            )
        )

    payload = _payload(
        _mapping("cluster-1", action="propose", category_id=None, category_name="研究资料"),
        _mapping("cluster-2", action="propose", category_id=None, category_name="行业情报"),
    )
    with pytest.raises(ClassificationOutputError, match="新分类预算"):
        _validate(payload, max_new_categories=1)


@pytest.mark.parametrize(
    "mapping",
    [
        _mapping("cluster-1", action="existing", category_id=None),
        _mapping("cluster-1", action="propose", category_id="category-development"),
        _mapping(
            "cluster-1",
            action="uncategorized",
            category_id=None,
            category_name="其他",
        ),
    ],
)
def test_rejects_invalid_action_shape(mapping: dict[str, object]) -> None:
    with pytest.raises(ClassificationOutputError, match="结构"):
        _validate(_payload(mapping))


def test_rejects_duplicate_or_unsafe_output_labels() -> None:
    with pytest.raises(ClassificationOutputError, match="结构"):
        _validate(_payload(_mapping("cluster-1", tags=["Python", " python "])))
    with pytest.raises(ClassificationOutputError, match="结构"):
        _validate(
            _payload(
                _mapping(
                    "cluster-1",
                    action="propose",
                    category_id=None,
                    category_name="<script>研究</script>",
                )
            )
        )


@pytest.mark.parametrize("invisible", ["\x00", "\u202e", "\u200b"])
def test_rejects_invisible_control_and_format_characters(invisible: str) -> None:
    with pytest.raises(ClassificationOutputError, match="结构"):
        _validate(
            _payload(
                _mapping(
                    "cluster-1",
                    action="propose",
                    category_id=None,
                    category_name=f"开{invisible}发",
                )
            )
        )
    with pytest.raises(ClassificationOutputError, match="结构"):
        _validate(_payload(_mapping("cluster-1", tags=["Python", f"Py{invisible}thon"])))


@pytest.mark.parametrize(
    "mapping",
    [
        _mapping("cluster-1", tags=[]),
        _mapping("cluster-1", tags=["文档"]),
        _mapping(
            "cluster-1",
            action="propose",
            category_id=None,
            category_name="研究",
            tags=["论文"],
        ),
    ],
)
def test_existing_and_proposed_categories_require_two_tags(mapping: dict[str, object]) -> None:
    with pytest.raises(ClassificationOutputError, match="结构"):
        _validate(_payload(mapping))


def test_uncategorized_fallback_allows_zero_tags_when_reviewed() -> None:
    result = _validate(
        _payload(
            _mapping(
                "cluster-1",
                action="uncategorized",
                category_id=None,
                category_name="未分类",
                tags=[],
                needs_review=True,
            )
        )
    )

    assert result.response.mappings[0].tags == ()


@pytest.mark.parametrize(
    "mapping",
    [
        _mapping(
            "cluster-1",
            action="uncategorized",
            category_id=None,
            category_name="未分类",
            tags=[],
            needs_review=False,
        ),
        _mapping(
            "cluster-1",
            reason_code="insufficient_evidence",
            needs_review=True,
        ),
        _mapping("cluster-1", confidence=0.49, needs_review=False),
    ],
)
def test_rejects_review_semantic_bypasses(mapping: dict[str, object]) -> None:
    with pytest.raises(ClassificationOutputError, match="结构"):
        _validate(_payload(mapping))


def test_accepts_reviewed_low_confidence_and_insufficient_evidence_fallbacks() -> None:
    assert LOW_CONFIDENCE_THRESHOLD == 0.5
    boundary = _validate(_payload(_mapping("cluster-1", confidence=0.5)))
    reviewed = _validate(_payload(_mapping("cluster-1", confidence=0.49, needs_review=True)))
    fallback = _validate(
        _payload(
            _mapping(
                "cluster-1",
                action="uncategorized",
                category_id=None,
                category_name="未分类",
                tags=[],
                confidence=0.2,
                needs_review=True,
                reason_code="insufficient_evidence",
            )
        )
    )

    assert boundary.response.mappings[0].needs_review is False
    assert reviewed.response.mappings[0].needs_review is True
    assert fallback.response.mappings[0].category_action == "uncategorized"


def test_binding_hash_is_order_independent_and_covers_validation_scope() -> None:
    first = _mapping("cluster-1")
    second = _mapping(
        "cluster-2",
        category_id="category-learning",
        category_name="学习与文档",
    )
    ordered = _validate(
        _payload(first, second),
        expected_subject_ids=("cluster-1", "cluster-2"),
        max_new_categories=1,
    )
    reversed_result = _validate(
        _payload(second, first),
        expected_subject_ids=("cluster-2", "cluster-1"),
        max_new_categories=1,
    )
    different_subject = _validate(
        _payload(first),
        expected_subject_ids=("cluster-1", "cluster-3"),
        max_new_categories=1,
    )
    different_taxonomy = _validate(
        _payload(first),
        expected_subject_ids=("cluster-1", "cluster-2"),
        allowed_categories={
            "category-development": "开发与技术",
            "category-learning": "学习与文档",
            "category-research": "研究",
        },
        max_new_categories=1,
    )
    different_budget = _validate(
        _payload(first),
        expected_subject_ids=("cluster-1", "cluster-2"),
        max_new_categories=2,
    )
    different_batch = _validate(
        _payload(first, batch_id="batch-002"),
        expected_batch_id="batch-002",
        expected_subject_ids=("cluster-1", "cluster-2"),
        max_new_categories=1,
    )

    assert ordered.binding_sha256 == reversed_result.binding_sha256
    assert ordered.response_canonical_json != reversed_result.response_canonical_json
    assert (
        len(
            {
                _validate(
                    _payload(first),
                    expected_subject_ids=("cluster-1", "cluster-2"),
                    max_new_categories=1,
                ).binding_sha256,
                different_subject.binding_sha256,
                different_taxonomy.binding_sha256,
                different_budget.binding_sha256,
                different_batch.binding_sha256,
            }
        )
        == 5
    )
    binding = json.loads(ordered.binding_canonical_json)
    assert binding["validator_version"] == CLASSIFICATION_VALIDATOR_VERSION
    assert binding["schema_version"] == CLASSIFICATION_SCHEMA_VERSION
    assert binding["batch_id"] == "batch-001"
    assert binding["expected_subject_ids"] == ["cluster-1", "cluster-2"]
    assert [mapping["subject_id"] for mapping in binding["mappings"]] == [
        "cluster-1",
        "cluster-2",
    ]


def test_allows_meaningful_tags_with_leading_punctuation() -> None:
    result = _validate(_payload(_mapping("cluster-1", tags=[".NET", "C++"])))

    assert result.response.mappings[0].tags == (".NET", "C++")


def test_rejects_coerced_scalar_types_and_invalid_caller_scope() -> None:
    payload = _payload(_mapping("cluster-1"))
    payload["mappings"][0]["confidence"] = "0.9"  # type: ignore[index]
    with pytest.raises(ClassificationOutputError, match="结构"):
        _validate(payload)

    with pytest.raises(ValueError, match="subject_id 不能重复"):
        validate_classification_output(
            _payload(_mapping("cluster-1")),
            expected_batch_id="batch-001",
            expected_subject_ids=("cluster-1", "cluster-1"),
            allowed_categories={"category-development": "开发与技术"},
            max_new_categories=1,
        )
