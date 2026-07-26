from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest

from webhub.agent.provider_binding import ProviderBinding
from webhub.bookmarks.classification_batches import (
    ClassificationBatchBudget,
    ClassificationCandidateSource,
    FolderClusterSource,
    build_folder_classification_batches,
)
from webhub.bookmarks.classifier import (
    ClassificationUnavailableError,
    estimated_request_count,
    run_plan,
)

# Vendor error bodies echo the request and can carry a key prefix.
VENDOR_LEAK = "invalid api key sk-live-abcd1234 for https://internal.vendor/v1"

_REAL_ASYNC_CLIENT = httpx.AsyncClient

BINDING = ProviderBinding(
    kind="model",
    provider="deepseek",
    config_id="cfg-1",
    display_name="DeepSeek",
    base_url="https://api.deepseek.com/v1",
    model_name="deepseek-v4-flash",
    timeout_seconds=30,
    api_key="sk-account-secret",
)


def _clusters(count: int) -> list[FolderClusterSource]:
    return [
        FolderClusterSource(
            source_id=f"folder-{index}",
            folder_labels=("书签栏", f"目录{index}"),
            candidates=(
                ClassificationCandidateSource(
                    source_id=f"cand-{index}",
                    title=f"标题{index}A",
                    hostname=f"host{index}.example.com",
                    folder_labels=("书签栏", f"目录{index}"),
                ),
            ),
        )
        for index in range(count)
    ]


def _plan(count: int, *, max_batches: int = 4):
    return build_folder_classification_batches(
        _clusters(count),
        allowed_categories={"c-dev": "开发与技术", "c-life": "生活与服务"},
        max_new_categories=2,
        requested_language="zh-CN",
        budget=ClassificationBatchBudget(
            max_batches=max_batches,
            max_total_payload_bytes=256 * 1024,
        ),
    )


def _mock_model(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(record)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


def _answer_all(request: httpx.Request) -> httpx.Response:
    """Echo back a well-formed mapping for every subject in the request."""

    body = json.loads(request.content)
    payload = json.loads(body["messages"][1]["content"])
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "schema_version": "webhub.bookmark-classification.v1",
                                "batch_id": payload["batch_id"],
                                "mappings": [
                                    {
                                        "subject_id": subject["subject_id"],
                                        "category_action": "existing",
                                        "category_id": "c-dev",
                                        "category_name": "开发与技术",
                                        "tags": ["前端", "框架"],
                                        "confidence": 0.9,
                                        "needs_review": False,
                                        "reason_code": "folder_match",
                                    }
                                    for subject in payload["subjects"]
                                ],
                            }
                        )
                    }
                }
            ]
        },
    )


def test_a_hundred_folders_cost_a_handful_of_calls_not_a_hundred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the design: cost scales with clusters, not sites."""

    plan = _plan(100)
    seen = _mock_model(monkeypatch, _answer_all)
    results = asyncio.run(run_plan(BINDING, plan))

    # 100 folder clusters — covering far more than 100 individual bookmarks —
    # batch into a couple of requests at 50 subjects each.
    assert estimated_request_count(plan) == len(seen) <= 3
    assert sum(len(result.mappings) for result in results) == 100


def test_the_request_carries_no_urls_and_no_page_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bookmark URLs routinely carry tokens; only hostnames may be sent."""

    plan = _plan(3)
    seen = _mock_model(monkeypatch, _answer_all)
    asyncio.run(run_plan(BINDING, plan))

    sent = seen[0].content.decode()
    assert "http://" not in sent
    assert "https://api.deepseek.com" not in sent
    # Hostnames are allowed; full URLs are not.
    assert "host0.example.com" in sent
    # The account key travels in the header, never in the body.
    assert "sk-account-secret" not in sent
    assert seen[0].headers["authorization"] == "Bearer sk-account-secret"


def test_the_prompt_closes_the_taxonomy_and_shows_worked_examples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(2)
    seen = _mock_model(monkeypatch, _answer_all)
    asyncio.run(run_plan(BINDING, plan))

    body = json.loads(seen[0].content)
    system = body["messages"][0]["content"]
    # Few-shot rather than prose-only rules, and an explicit "don't guess" case.
    assert "示例输出" in system
    assert "uncategorized" in system
    assert "不要猜" in system
    # 契约要求 existing/propose 必须 2-8 个标签，提示词必须说清楚
    assert "2 到 8 个" in system
    # Labelling task: no sampling noise.
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    # The closed taxonomy travels with the payload.
    payload = json.loads(body["messages"][1]["content"])
    assert {item["category_name"] for item in payload["allowed_categories"]} == {
        "开发与技术",
        "生活与服务",
    }


def test_a_model_answering_about_subjects_it_was_not_given_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def smuggle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "schema_version": "webhub.bookmark-classification.v1",
                                    "batch_id": payload["batch_id"],
                                    "mappings": [
                                        {
                                            "subject_id": "not-in-this-batch",
                                            "category_action": "existing",
                                            "category_id": "c-dev",
                                            "category_name": "开发与技术",
                                            "tags": ["前端", "框架"],
                                            "confidence": 1.0,
                                            "needs_review": False,
                                            "reason_code": "x",
                                        }
                                    ],
                                }
                            )
                        }
                    }
                ]
            },
        )

    plan = _plan(2)
    _mock_model(monkeypatch, smuggle)
    with pytest.raises(ClassificationUnavailableError):
        asyncio.run(run_plan(BINDING, plan))


def test_missing_subjects_fall_back_to_needs_review_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def answer_first_only(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        first = payload["subjects"][0]["subject_id"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "schema_version": "webhub.bookmark-classification.v1",
                                    "batch_id": payload["batch_id"],
                                    "mappings": [
                                        {
                                            "subject_id": first,
                                            "category_action": "existing",
                                            "category_id": "c-dev",
                                            "category_name": "开发与技术",
                                            "tags": ["前端", "框架"],
                                            "confidence": 0.8,
                                            "needs_review": False,
                                            "reason_code": "folder_match",
                                        }
                                    ],
                                }
                            )
                        }
                    }
                ]
            },
        )

    plan = _plan(3)
    _mock_model(monkeypatch, answer_first_only)
    results = asyncio.run(run_plan(BINDING, plan))

    # A lazy answer degrades to 未分类/待复核 for the rest rather than losing them.
    assert len(results[0].unresolved_source_ids) == 2
    assert len(results[0].mappings) == 3


@pytest.mark.parametrize("status", [401, 429, 500])
def test_vendor_failures_never_surface_vendor_text(
    status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(2)
    _mock_model(
        monkeypatch,
        lambda _request: httpx.Response(status, json={"error": {"message": VENDOR_LEAK}}),
    )
    with pytest.raises(ClassificationUnavailableError) as raised:
        asyncio.run(run_plan(BINDING, plan))
    assert VENDOR_LEAK not in str(raised.value)
    assert "sk-account-secret" not in str(raised.value)


def test_a_fenced_or_oversized_answer_is_handled(monkeypatch: pytest.MonkeyPatch) -> None:
    def fenced(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        inner = json.dumps(
            {
                "schema_version": "webhub.bookmark-classification.v1",
                "batch_id": payload["batch_id"],
                "mappings": [
                    {
                        "subject_id": subject["subject_id"],
                        "category_action": "uncategorized",
                        "category_id": None,
                        "category_name": "未分类",
                        "tags": [],
                        "confidence": 0.1,
                        "needs_review": True,
                        "reason_code": "insufficient_evidence",
                    }
                    for subject in payload["subjects"]
                ],
            }
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": f"```json\n{inner}\n```"}}]},
        )

    plan = _plan(2)
    _mock_model(monkeypatch, fenced)
    results = asyncio.run(run_plan(BINDING, plan))
    assert len(results[0].mappings) == 2

    oversized = _plan(2)
    _mock_model(
        monkeypatch,
        lambda _request: httpx.Response(
            200, json={"choices": [{"message": {"content": "x" * 70_000}}]}
        ),
    )
    with pytest.raises(ClassificationUnavailableError):
        asyncio.run(run_plan(BINDING, oversized))
