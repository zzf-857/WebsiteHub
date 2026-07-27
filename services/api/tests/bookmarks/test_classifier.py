from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace

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
    CLASSIFICATION_TIMEOUT_SECONDS,
    BatchRunResult,
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


def _plan(count: int, *, max_batches: int = 4, include_tags: bool = True):
    return build_folder_classification_batches(
        _clusters(count),
        allowed_categories={"c-dev": "开发与技术", "c-life": "生活与服务"},
        include_tags=include_tags,
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
    *,
    timeouts: list[object] | None = None,
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        if timeouts is not None:
            timeouts.append(kwargs.get("timeout"))
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
                                        "tags": ["前端", "框架"]
                                        if payload.get("include_tags", True)
                                        else [],
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


def test_classification_uses_a_timeout_sized_for_structured_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeouts: list[object] = []
    plan = _plan(2)
    _mock_model(monkeypatch, _answer_all, timeouts=observed_timeouts)

    asyncio.run(run_plan(replace(BINDING, timeout_seconds=999), plan))

    assert observed_timeouts == [CLASSIFICATION_TIMEOUT_SECONDS]


def test_classification_enforces_a_total_wall_clock_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class HangingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            await asyncio.Event().wait()
            raise AssertionError("the wall-clock deadline must cancel this request")

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = HangingTransport()
        return _REAL_ASYNC_CLIENT(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr("webhub.bookmarks.classifier.CLASSIFICATION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("webhub.bookmarks.classifier.CLASSIFICATION_RETRY_DELAY_SECONDS", 0)

    with pytest.raises(ClassificationUnavailableError, match="request failed"):
        asyncio.run(run_plan(BINDING, _plan(1)))

    assert attempts == 2


def test_run_plan_supports_bounded_concurrency_without_reordering_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(100)
    active = 0
    peak = 0

    async def fake_run_batch(_binding, batch):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return BatchRunResult(
                batch_id=batch.batch_id,
                mappings=(),
                unresolved_source_ids=(),
            )
        finally:
            active -= 1

    monkeypatch.setattr("webhub.bookmarks.classifier.run_batch", fake_run_batch)

    results = asyncio.run(run_plan(BINDING, plan, max_concurrency=2))

    assert peak == 2
    assert [result.batch_id for result in results] == [batch.batch_id for batch in plan.batches]


def test_run_plan_stops_before_next_group_when_client_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(101)
    assert len(plan.batches) == 3
    started_batch_ids: list[str] = []
    disconnect_checks = 0

    async def fake_run_batch(_binding, batch):
        started_batch_ids.append(batch.batch_id)
        return BatchRunResult(
            batch_id=batch.batch_id,
            mappings=(),
            unresolved_source_ids=(),
        )

    async def cancel_requested() -> bool:
        nonlocal disconnect_checks
        disconnect_checks += 1
        return disconnect_checks >= 2

    monkeypatch.setattr("webhub.bookmarks.classifier.run_batch", fake_run_batch)

    with pytest.raises(ClassificationUnavailableError, match="request disconnected"):
        asyncio.run(
            run_plan(
                BINDING,
                plan,
                max_concurrency=2,
                cancel_requested=cancel_requested,
            )
        )

    assert disconnect_checks == 2
    assert started_batch_ids == [batch.batch_id for batch in plan.batches[:2]]


def test_run_plan_retries_one_invalid_model_answer_without_restarting_prior_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def invalid_then_valid(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
        return _answer_all(request)

    plan = _plan(2)
    seen = _mock_model(monkeypatch, invalid_then_valid)

    results = asyncio.run(run_plan(BINDING, plan))

    assert attempts == len(seen) == 2
    assert len(results) == 1


def test_run_plan_does_not_spend_a_retry_after_client_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(2)
    attempts = 0
    disconnect_checks = 0

    async def fail_retryably(_binding, _batch):
        nonlocal attempts
        attempts += 1
        raise ClassificationUnavailableError("transient failure", retryable=True)

    async def cancel_requested() -> bool:
        nonlocal disconnect_checks
        disconnect_checks += 1
        return disconnect_checks >= 2

    monkeypatch.setattr("webhub.bookmarks.classifier.run_batch", fail_retryably)
    monkeypatch.setattr("webhub.bookmarks.classifier.CLASSIFICATION_RETRY_DELAY_SECONDS", 0)

    with pytest.raises(ClassificationUnavailableError, match="request disconnected"):
        asyncio.run(run_plan(BINDING, plan, cancel_requested=cancel_requested))

    assert attempts == 1
    assert disconnect_checks == 2


def test_run_plan_cancels_and_drains_siblings_when_one_batch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(51)
    assert len(plan.batches) == 2
    ready_count = 0
    all_ready = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    attempts_by_batch: dict[str, int] = {}
    first_batch_id = plan.batches[0].batch_id

    async def one_fails_one_waits(_binding, batch):
        nonlocal ready_count
        attempts_by_batch[batch.batch_id] = attempts_by_batch.get(batch.batch_id, 0) + 1
        ready_count += 1
        if ready_count == 2:
            all_ready.set()
        await all_ready.wait()
        if batch.batch_id == first_batch_id:
            raise ClassificationUnavailableError("terminal failure")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise

    monkeypatch.setattr("webhub.bookmarks.classifier.run_batch", one_fails_one_waits)

    async def scenario() -> None:
        with pytest.raises(ClassificationUnavailableError, match="terminal failure"):
            await run_plan(BINDING, plan, max_concurrency=2)
        await asyncio.sleep(0)
        assert sibling_cancelled.is_set()

    asyncio.run(scenario())

    assert attempts_by_batch == {batch.batch_id: 1 for batch in plan.batches}


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


def test_category_only_plan_requires_empty_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _plan(2, include_tags=False)
    seen = _mock_model(monkeypatch, _answer_all)

    results = asyncio.run(run_plan(BINDING, plan))

    body = json.loads(seen[0].content)
    system = body["messages"][0]["content"]
    payload = json.loads(body["messages"][1]["content"])
    assert "本任务只修改分类，不修改标签" in system
    assert "tags 都必须是空数组" in system
    assert payload["include_tags"] is False
    assert all(not bound.mapping.tags for result in results for bound in result.mappings)


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
