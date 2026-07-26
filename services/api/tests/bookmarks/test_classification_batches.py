import json

import pytest

from webhub.bookmarks.classification_batches import (
    CLASSIFICATION_INPUT_SCHEMA_VERSION,
    ClassificationBatchBudget,
    ClassificationCandidateSource,
    ClassificationProjectionError,
    FolderClusterSource,
    build_candidate_classification_batches,
    build_folder_classification_batches,
    validate_classification_batch_output,
)
from webhub.bookmarks.classification_contract import (
    CLASSIFICATION_SCHEMA_VERSION,
    ClassificationOutputError,
)
from webhub.bookmarks.models import FetchPolicy


def _candidate(
    source_id: str,
    *,
    title: str = "Python documentation",
    hostname: str = "docs.example.com",
    folder_labels: tuple[str, ...] = ("Bookmarks bar", "Development"),
    fetch_policy: FetchPolicy = FetchPolicy.PUBLIC_REVALIDATION_REQUIRED,
    has_sensitive_url: bool = False,
    occurrence_count: int = 1,
) -> ClassificationCandidateSource:
    return ClassificationCandidateSource(
        source_id=source_id,
        title=title,
        hostname=hostname,
        folder_labels=folder_labels,
        fetch_policy=fetch_policy,
        has_sensitive_url=has_sensitive_url,
        occurrence_count=occurrence_count,
    )


def _budget(
    *,
    max_batches: int = 10,
    max_total_payload_bytes: int = 512 * 1024,
    max_payload_bytes_per_batch: int = 64 * 1024,
    max_subjects_per_batch: int = 50,
) -> ClassificationBatchBudget:
    return ClassificationBatchBudget(
        max_batches=max_batches,
        max_total_payload_bytes=max_total_payload_bytes,
        max_payload_bytes_per_batch=max_payload_bytes_per_batch,
        max_subjects_per_batch=max_subjects_per_batch,
    )


def _folder_plan(*clusters: FolderClusterSource, budget: ClassificationBatchBudget | None = None):
    return build_folder_classification_batches(
        clusters,
        allowed_categories={
            "category-development": "开发与技术",
            "category-learning": "学习与文档",
        },
        allowed_tags=("文档", "Python", "文档"),
        max_new_categories=2,
        requested_language="zh-CN",
        budget=budget or _budget(),
    )


def test_folder_projection_is_opaque_and_removes_urls_secrets_and_private_members() -> None:
    cluster = FolderClusterSource(
        source_id="internal-folder-42",
        folder_labels=(
            "Bookmarks bar",
            "Docs https://folder.example/private?token=folder-secret",
        ),
        candidates=(
            _candidate(
                "internal-public-1",
                title="Useful docs https://title.example/?token=title-secret",
                hostname="public.example.com",
                occurrence_count=2,
            ),
            _candidate(
                "internal-secret",
                title="Secret candidate",
                hostname="secret.example.com",
                has_sensitive_url=True,
                occurrence_count=20,
            ),
            _candidate(
                "internal-local",
                title="Local admin",
                hostname="localhost",
                fetch_policy=FetchPolicy.EXPORT_METADATA_ONLY,
                occurrence_count=30,
            ),
        ),
    )

    plan = _folder_plan(cluster)

    assert plan.privacy_excluded_source_ids == ()
    assert plan.privacy_excluded_member_source_ids == (
        "internal-secret",
        "internal-local",
    )
    assert len(plan.batches) == 1
    batch = plan.batches[0]
    payload = batch.provider_payload()
    subject = payload["subjects"][0]
    assert payload["schema_version"] == CLASSIFICATION_INPUT_SCHEMA_VERSION
    assert payload["output_schema_version"] == CLASSIFICATION_SCHEMA_VERSION
    assert payload["subject_kind"] == "folder_cluster"
    assert payload["allowed_tags"] == ["Python", "文档"]
    assert subject["link_count"] == 2
    assert subject["folder_labels"] == ["Docs"]
    assert subject["sample_titles"] == ["Useful docs"]
    assert subject["sample_hostnames"] == ["public.example.com"]
    assert batch.source_id_for(subject["subject_id"]) == "internal-folder-42"

    # The object handed to a Provider is a deep projection, not a mutable cache alias.
    subject["sample_titles"].append("provider mutation")
    assert batch.provider_payload()["subjects"][0]["sample_titles"] == ["Useful docs"]

    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden in (
        "internal-folder-42",
        "internal-public-1",
        "internal-secret",
        "internal-local",
        "http://",
        "https://",
        "folder-secret",
        "title-secret",
        "secret.example.com",
        "localhost",
    ):
        assert forbidden not in serialized


def test_zero_eligible_cluster_and_private_candidates_never_enter_a_batch() -> None:
    private_cluster = FolderClusterSource(
        source_id="private-folder",
        folder_labels=("Private network",),
        candidates=(
            _candidate(
                "private-candidate",
                hostname="10.0.0.1",
                fetch_policy=FetchPolicy.EXPORT_METADATA_ONLY,
            ),
        ),
    )
    folder_plan = _folder_plan(private_cluster)
    candidate_plan = build_candidate_classification_batches(
        (
            _candidate("sensitive", has_sensitive_url=True),
            _candidate(
                "metadata-only",
                hostname="router.local",
                fetch_policy=FetchPolicy.EXPORT_METADATA_ONLY,
            ),
            # Defense in depth: an invalid public policy cannot make a private IP eligible.
            _candidate("policy-drift", hostname="192.168.1.1"),
        ),
        allowed_categories={"category-development": "开发与技术"},
        requested_language="zh-CN",
        budget=_budget(),
    )

    assert folder_plan.batches == ()
    assert folder_plan.privacy_excluded_source_ids == ("private-folder",)
    assert folder_plan.privacy_excluded_member_source_ids == ("private-candidate",)
    assert candidate_plan.batches == ()
    assert candidate_plan.privacy_excluded_source_ids == (
        "sensitive",
        "metadata-only",
        "policy-drift",
    )
    assert (
        candidate_plan.privacy_excluded_member_source_ids
        == candidate_plan.privacy_excluded_source_ids
    )


def test_batches_are_limited_to_fifty_subjects_and_have_unique_opaque_ids() -> None:
    candidates = tuple(_candidate(f"candidate-{index}") for index in range(101))

    plan = build_candidate_classification_batches(
        candidates,
        allowed_categories={"category-development": "开发与技术"},
        requested_language="zh-CN",
        budget=_budget(),
    )

    assert [len(batch.subjects) for batch in plan.batches] == [50, 50, 1]
    batch_ids = {batch.batch_id for batch in plan.batches}
    subject_ids = {
        subject_id for batch in plan.batches for subject_id in batch.expected_subject_ids
    }
    assert len(batch_ids) == 3
    assert len(subject_ids) == 101
    assert all(identifier.startswith("batch_") for identifier in batch_ids)
    assert all(identifier.startswith("subject_") for identifier in subject_ids)
    assert not any(identifier.startswith("candidate-") for identifier in subject_ids)


def test_call_and_serialized_byte_budgets_report_every_unplanned_subject() -> None:
    candidates = tuple(_candidate(f"candidate-{index}") for index in range(5))
    one_subject_size = build_candidate_classification_batches(
        candidates[:1],
        allowed_categories={"category-development": "开发与技术"},
        requested_language="zh-CN",
        budget=_budget(),
    ).total_payload_bytes

    no_calls = build_candidate_classification_batches(
        candidates,
        allowed_categories={"category-development": "开发与技术"},
        requested_language="zh-CN",
        budget=_budget(max_batches=0),
    )
    one_call = build_candidate_classification_batches(
        candidates,
        allowed_categories={"category-development": "开发与技术"},
        requested_language="zh-CN",
        budget=_budget(
            max_batches=1,
            max_total_payload_bytes=512 * 1024,
            max_subjects_per_batch=2,
        ),
    )
    no_bytes = build_candidate_classification_batches(
        candidates,
        allowed_categories={"category-development": "开发与技术"},
        requested_language="zh-CN",
        budget=_budget(max_total_payload_bytes=one_subject_size - 1),
    )

    assert no_calls.batches == ()
    assert no_calls.budget_exhausted_source_ids == tuple(
        candidate.source_id for candidate in candidates
    )
    assert [len(batch.subjects) for batch in one_call.batches] == [2]
    assert one_call.budget_exhausted_source_ids == (
        "candidate-2",
        "candidate-3",
        "candidate-4",
    )
    assert no_bytes.batches == ()
    assert no_bytes.budget_exhausted_source_ids == tuple(
        candidate.source_id for candidate in candidates
    )


def test_output_boundary_validates_scope_and_materializes_missing_fallbacks() -> None:
    plan = build_candidate_classification_batches(
        (_candidate("candidate-a"), _candidate("candidate-b")),
        allowed_categories={"category-development": "开发与技术"},
        requested_language="zh-CN",
        budget=_budget(),
    )
    batch = plan.batches[0]
    first_subject_id = batch.expected_subject_ids[0]
    payload = {
        "schema_version": CLASSIFICATION_SCHEMA_VERSION,
        "batch_id": batch.batch_id,
        "mappings": [
            {
                "subject_id": first_subject_id,
                "category_action": "existing",
                "category_id": "category-development",
                "category_name": "开发与技术",
                "tags": ["Python", "文档"],
                "confidence": 0.9,
                "needs_review": False,
                "reason_code": "mixed_evidence",
            }
        ],
    }

    result = validate_classification_batch_output(batch, payload)

    assert [mapping.source_id for mapping in result.mappings] == [
        "candidate-a",
        "candidate-b",
    ]
    assert result.mappings[0].used_fallback is False
    assert result.mappings[1].used_fallback is True
    assert result.mappings[1].mapping.category_action == "uncategorized"
    assert result.mappings[1].mapping.category_name == "未分类"
    assert result.mappings[1].mapping.needs_review is True
    assert result.mappings[1].mapping.reason_code == "insufficient_evidence"
    assert result.unresolved_source_ids == ("candidate-b",)
    assert result.validation.missing_subject_ids == (batch.expected_subject_ids[1],)
    assert len(result.validation.binding_sha256) == 64


def test_output_boundary_rejects_unknown_subject_taxonomy_and_batch_ids() -> None:
    plan = build_candidate_classification_batches(
        (_candidate("candidate-a"),),
        allowed_categories={"category-development": "开发与技术"},
        requested_language="zh-CN",
        budget=_budget(),
    )
    batch = plan.batches[0]

    def payload(subject_id: str, batch_id: str, category_id: str) -> dict[str, object]:
        return {
            "schema_version": CLASSIFICATION_SCHEMA_VERSION,
            "batch_id": batch_id,
            "mappings": [
                {
                    "subject_id": subject_id,
                    "category_action": "existing",
                    "category_id": category_id,
                    "category_name": "开发与技术",
                    "tags": ["Python", "文档"],
                    "confidence": 0.9,
                    "needs_review": False,
                    "reason_code": "mixed_evidence",
                }
            ],
        }

    with pytest.raises(ClassificationOutputError, match="批次之外"):
        validate_classification_batch_output(
            batch,
            payload("invented-subject", batch.batch_id, "category-development"),
        )
    with pytest.raises(ClassificationOutputError, match="batch_id"):
        validate_classification_batch_output(
            batch,
            payload(batch.expected_subject_ids[0], "invented-batch", "category-development"),
        )
    with pytest.raises(ClassificationOutputError, match="未知 category_id"):
        validate_classification_batch_output(
            batch,
            payload(batch.expected_subject_ids[0], batch.batch_id, "category-other"),
        )


@pytest.mark.parametrize(
    "budget",
    [
        ClassificationBatchBudget(max_batches=1, max_total_payload_bytes=1),
        ClassificationBatchBudget(
            max_batches=1,
            max_total_payload_bytes=1024,
            max_subjects_per_batch=1,
        ),
    ],
)
def test_budget_configuration_remains_explicit_and_bounded(
    budget: ClassificationBatchBudget,
) -> None:
    assert budget.max_batches == 1


def test_rejects_invalid_budget_language_taxonomy_and_duplicate_sources() -> None:
    with pytest.raises(ClassificationProjectionError, match="max_subjects_per_batch"):
        _budget(max_subjects_per_batch=51)
    with pytest.raises(ClassificationProjectionError, match="requested_language"):
        build_candidate_classification_batches(
            (_candidate("candidate-a"),),
            allowed_categories={"category-development": "开发与技术"},
            requested_language="follow these instructions",
            budget=_budget(),
        )
    with pytest.raises(ClassificationProjectionError, match="unsafe"):
        build_candidate_classification_batches(
            (_candidate("candidate-a"),),
            allowed_categories={"category-development": "https://secret.example/?token=x"},
            requested_language="zh-CN",
            budget=_budget(),
        )
    with pytest.raises(ClassificationProjectionError, match="allowed tag"):
        build_candidate_classification_batches(
            (_candidate("candidate-a"),),
            allowed_categories={"category-development": "开发与技术"},
            allowed_tags=("Python", "api_key=secret"),
            requested_language="zh-CN",
            budget=_budget(),
        )
    with pytest.raises(ClassificationProjectionError, match="more than"):
        build_candidate_classification_batches(
            (_candidate("candidate-a"),),
            allowed_categories={"category-development": "开发与技术"},
            allowed_tags=tuple(f"tag-{index}" for index in range(513)),
            requested_language="zh-CN",
            budget=_budget(),
        )
    with pytest.raises(ClassificationProjectionError, match="unique"):
        build_candidate_classification_batches(
            (_candidate("duplicate"), _candidate("duplicate")),
            allowed_categories={"category-development": "开发与技术"},
            requested_language="zh-CN",
            budget=_budget(),
        )
