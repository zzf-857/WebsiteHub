"""Run bounded classification batches against the account's own model Provider.

Everything here exists to answer one question cheaply: *what category does this
group of sites belong to?*  The naive shape — one model call per site — would
cost roughly 2000 requests for a typical bookmark import.  Four things keep it
to a couple of calls instead.

**Cluster, do not enumerate.**  Sites are grouped by the signal that actually
predicts a category (their source folder, or their current category plus host),
and one decision covers the whole group.  A 40-bookmark "前端" folder is one
subject, not forty.

**Only ask about what the rules could not answer.**  ``suggest_category``
already resolves most sites from folder/host/title keywords at zero cost.  Only
the leftovers reach a model.

**Close the taxonomy.**  The prompt ships the allowed category list and a small
number of worked examples, and the response schema restricts ``category_action``
to ``existing`` / ``propose`` / ``uncategorized``.  A model that cannot invent
categories cannot wander, and a bounded answer is a short answer.

**Send the minimum that supports the decision.**  ``classification_batches``
already caps each subject at 8 sample titles and 8 hostnames, and never sends a
full URL — bookmark URLs routinely carry tokens and session ids.

Vendor failures collapse into ``ClassificationUnavailableError``; like
``web_search``, vendor error text never reaches the caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from webhub.bookmarks.classification_batches import (
    ClassificationBatch,
    ClassificationBatchPlan,
    validate_classification_batch_output,
)
from webhub.bookmarks.classification_contract import ClassificationOutputError

# The response is a bounded JSON object; anything much larger than this is a
# model that has stopped following the schema.
MAX_RESPONSE_CHARS = 60_000
CLASSIFICATION_TIMEOUT_SECONDS = 90
MAX_CLASSIFICATION_CONCURRENCY = 4
CLASSIFICATION_MAX_ATTEMPTS = 2
CLASSIFICATION_RETRY_DELAY_SECONDS = 0.25
logger = logging.getLogger(__name__)


class ModelEndpoint(Protocol):
    """The three things this module needs from a resolved model Provider.

    Declared here rather than importing ``agent.provider_binding.ProviderBinding``:
    ``agent`` already imports ``bookmarks``, so depending on it in this direction
    would close an import cycle between two top-level packages.  A low-level
    module should state what it needs and let the caller satisfy it —
    ``ProviderBinding`` does, structurally, with no changes on its side.
    """

    @property
    def base_url(self) -> str: ...

    @property
    def model_name(self) -> str | None: ...

    @property
    def client_api_key(self) -> str: ...


class ClassificationUnavailableError(RuntimeError):
    """The account's model Provider could not produce a usable batch answer."""

    safe_message = "自动分类暂时不可用，请稍后重试或检查模型 Provider 配置。"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class BatchRunResult:
    batch_id: str
    mappings: tuple[object, ...]
    unresolved_source_ids: tuple[str, ...]


# Worked examples, not prose rules.  Two of them, chosen to demonstrate the two
# decisions that actually go wrong: reusing an existing category instead of
# minting a near-duplicate, and admitting "uncategorized" instead of guessing.
_TAGGED_FEW_SHOT = """\
示例输入（节选）：
{"subject_id":"subject_a1","folder_labels":["开发","前端"],\
"sample_titles":["React 文档","Vite"],\
"sample_hostnames":["react.dev","vitejs.dev"],"link_count":12}
{"subject_id":"subject_b2","folder_labels":["临时"],\
"sample_titles":["未命名","新建标签页"],\
"sample_hostnames":["example.com"],"link_count":3}

示例输出：
{"schema_version":"webhub.bookmark-classification.v1","batch_id":"<原样回传>","mappings":[
{"subject_id":"subject_a1","category_action":"existing","category_id":"c-dev",\
"category_name":"开发与技术","tags":["前端","框架"],"confidence":0.9,"needs_review":false,\
"reason_code":"folder_match"},
{"subject_id":"subject_b2","category_action":"uncategorized","category_id":null,\
"category_name":"未分类","tags":[],"confidence":0.2,"needs_review":true,\
"reason_code":"insufficient_evidence"}]}

注意 subject_a1 给了 2 个标签（existing 至少要 2 个）。
注意 subject_b2：证据不足时选 uncategorized、tags 留空并标记 needs_review，不要猜。"""

_CATEGORY_ONLY_FEW_SHOT = """\
示例输入（节选）：
{"subject_id":"subject_a1","folder_labels":["开发","前端"],\
"sample_titles":["React 文档","Vite"],\
"sample_hostnames":["react.dev","vitejs.dev"],"link_count":12}
{"subject_id":"subject_b2","folder_labels":["临时"],\
"sample_titles":["未命名","新建标签页"],\
"sample_hostnames":["example.com"],"link_count":3}

示例输出：
{"schema_version":"webhub.bookmark-classification.v1","batch_id":"<原样回传>","mappings":[
{"subject_id":"subject_a1","category_action":"existing","category_id":"c-dev",\
"category_name":"开发与技术","tags":[],"confidence":0.9,"needs_review":false,\
"reason_code":"folder_match"},
{"subject_id":"subject_b2","category_action":"uncategorized","category_id":null,\
"category_name":"未分类","tags":[],"confidence":0.2,"needs_review":true,\
"reason_code":"insufficient_evidence"}]}

注意：本任务不修改标签，每一条 mapping 的 tags 都必须是空数组。
注意 subject_b2：证据不足时选 uncategorized、tags 留空并标记 needs_review，不要猜。"""


def _system_prompt(include_tags: bool) -> str:
    tag_rule = (
        "- **tags 必须给 2 到 8 个**（category_action 为 existing 或 propose 时）。\n"
        "  这是硬性约定，给少了整批答案都会被判为非法。uncategorized 时 tags 必须是空数组。"
        if include_tags
        else "- **本任务只修改分类，不修改标签**。每一条 mapping 的 tags 都必须是空数组 `[]`。"
    )
    few_shot = _TAGGED_FEW_SHOT if include_tags else _CATEGORY_ONLY_FEW_SHOT
    return f"""你是一个网址分类器。输入是若干「主题」，每个主题代表一组网站\
（一个书签文件夹，或一批同类网站）。为每个主题选一个分类。

## 规则
- 只输出 JSON，不要解释、不要 markdown 代码块、不要任何额外文字。
- `batch_id` 必须原样回传输入里的值。
- 每个输入主题**必须**在 mappings 里出现且只出现一次，`subject_id` 原样回传。
- 分类**优先从 allowed_categories 里选**（category_action="existing"，
  并回传该分类的 category_id）。
- 只有当所有已有分类都明显不合适时，才用 category_action="propose" 提一个新分类，
  且提出的新分类总数不得超过 max_new_categories。
- 证据不足时用 category_action="uncategorized"、category_name="未分类"、
  needs_review=true。**猜错比承认不知道更糟。**
{tag_rule}
- reason_code 只能是 folder_match / host_match / title_match / mixed_evidence /
  insufficient_evidence 五者之一。
- confidence 是 0 到 1 的小数。

## 你看不到的东西
输入里只有文件夹名、少量标题样本和主机名。**没有完整 URL、没有页面内容。**
不要假装读过网页，不要基于想象补充信息。

{few_shot}"""


def _messages(payload: Mapping[str, object]) -> list[dict[str, str]]:
    include_tags = payload.get("include_tags", True) is not False
    return [
        {"role": "system", "content": _system_prompt(include_tags)},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def _stripped_json(text: str) -> str:
    """Tolerate a fenced block without tolerating prose around it."""

    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1]
        if candidate.rstrip().endswith("```"):
            candidate = candidate.rstrip()[: -len("```")]
    return candidate.strip()


async def _ask_model(binding: ModelEndpoint, payload: Mapping[str, object]) -> str:
    import httpx

    if binding.model_name is None:
        raise ClassificationUnavailableError("model binding has no model name")
    body = {
        "model": binding.model_name,
        "messages": _messages(payload),
        # Deterministic-ish: this is a labelling task, not a creative one.
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    try:
        # httpx's timeout is per I/O phase/read chunk, not a wall-clock deadline.
        # The outer deadline is what makes the synchronous 50-batch budget provable.
        async with asyncio.timeout(CLASSIFICATION_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(
                timeout=CLASSIFICATION_TIMEOUT_SECONDS,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    f"{binding.base_url.rstrip('/')}/chat/completions",
                    headers={"authorization": f"Bearer {binding.client_api_key}"},
                    json=body,
                )
                if response.status_code != 200:
                    # Never read a vendor error body: it echoes the request and can
                    # carry a prefix of the API key.
                    raise ClassificationUnavailableError(
                        f"classification provider returned {response.status_code}",
                        retryable=(
                            response.status_code in {408, 429} or response.status_code >= 500
                        ),
                    )
                data = response.json()
    except ClassificationUnavailableError:
        raise
    except Exception as error:  # noqa: BLE001 - vendor errors must not escape
        raise ClassificationUnavailableError(
            "classification request failed",
            retryable=True,
        ) from error

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ClassificationUnavailableError(
            "classification response was not usable",
            retryable=True,
        ) from error
    if not isinstance(content, str) or not content.strip():
        raise ClassificationUnavailableError("classification response was empty", retryable=True)
    if len(content) > MAX_RESPONSE_CHARS:
        raise ClassificationUnavailableError(
            "classification response was too large",
            retryable=True,
        )
    return _stripped_json(content)


async def run_batch(binding: ModelEndpoint, batch: ClassificationBatch) -> BatchRunResult:
    """Classify one bounded batch and bind the answer back to local ids.

    The model only ever sees opaque subject ids, so a wrong or hostile answer
    cannot reach beyond the subjects in this batch: ``validate_classification_batch_output``
    rejects unknown ids and materialises ``未分类/待复核`` for missing ones.
    """

    answer = await _ask_model(binding, batch.provider_payload())
    try:
        validated = validate_classification_batch_output(batch, answer)
    except (ClassificationOutputError, ValueError) as error:
        raise ClassificationUnavailableError(
            "classification output failed validation",
            retryable=True,
        ) from error
    return BatchRunResult(
        batch_id=batch.batch_id,
        mappings=tuple(validated.mappings),
        unresolved_source_ids=validated.unresolved_source_ids,
    )


async def run_plan(
    binding: ModelEndpoint,
    plan: ClassificationBatchPlan,
    *,
    max_concurrency: int = 1,
    cancel_requested: Callable[[], Awaitable[bool]] | None = None,
) -> list[BatchRunResult]:
    """Run every batch in bounded groups while preserving plan order.

    The default stays sequential because these calls spend the user's own quota.
    Callers with a long, explicitly confirmed plan may opt into a small bounded
    concurrency; the hard cap prevents an accidental Provider burst.
    """

    if (
        isinstance(max_concurrency, bool)
        or not isinstance(max_concurrency, int)
        or not 1 <= max_concurrency <= MAX_CLASSIFICATION_CONCURRENCY
    ):
        raise ValueError(
            f"max_concurrency must be an integer between 1 and "
            f"{MAX_CLASSIFICATION_CONCURRENCY}"
        )

    total_batches = len(plan.batches)

    async def ensure_connected() -> None:
        if cancel_requested is not None and await cancel_requested():
            raise ClassificationUnavailableError("classification request disconnected")

    async def run_indexed(index: int, batch: ClassificationBatch) -> BatchRunResult:
        for attempt in range(1, CLASSIFICATION_MAX_ATTEMPTS + 1):
            # The group-level check covers each first attempt without racing several
            # request.is_disconnected() calls. Retries need their own fresh check.
            if attempt > 1:
                await ensure_connected()
            try:
                return await run_batch(binding, batch)
            except ClassificationUnavailableError as error:
                # Internal reason strings and exception class names are fixed locally;
                # vendor response bodies are never read or logged.
                cause_name = type(error.__cause__).__name__ if error.__cause__ else "none"
                contract_reason = (
                    str(error.__cause__)
                    if isinstance(error.__cause__, ClassificationOutputError)
                    else "none"
                )
                will_retry = error.retryable and attempt < CLASSIFICATION_MAX_ATTEMPTS
                logger.warning(
                    "classification batch %d/%d attempt %d/%d failed: %s "
                    "(cause=%s, contract=%s, retry=%s)",
                    index,
                    total_batches,
                    attempt,
                    CLASSIFICATION_MAX_ATTEMPTS,
                    error,
                    cause_name,
                    contract_reason,
                    will_retry,
                )
                if not will_retry:
                    raise
                await asyncio.sleep(CLASSIFICATION_RETRY_DELAY_SECONDS)
        raise AssertionError("classification retry loop exited unexpectedly")

    results: list[BatchRunResult] = []
    for offset in range(0, total_batches, max_concurrency):
        await ensure_connected()
        group = plan.batches[offset : offset + max_concurrency]
        tasks = [
            asyncio.create_task(run_indexed(offset + group_index, batch))
            for group_index, batch in enumerate(group, start=1)
        ]
        try:
            group_results = await asyncio.gather(*tasks)
        except BaseException:
            # asyncio.gather propagates the first failure but does not cancel its
            # siblings. Drain them explicitly so a failed group cannot keep using
            # Provider quota after run_plan has already returned an error.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        results.extend(group_results)
    await ensure_connected()
    return results


def estimated_request_count(plan: ClassificationBatchPlan) -> int:
    """How many model calls running this plan would cost, before spending any."""

    return len(plan.batches)


def estimated_input_characters(plan: ClassificationBatchPlan) -> int:
    """Rough size of what would be sent, so a caller can show a cost preview."""

    return sum(
        len(json.dumps(batch.provider_payload(), ensure_ascii=False, separators=(",", ":")))
        + len(_system_prompt(batch.include_tags))
        for batch in plan.batches
    )


__all__ = [
    "CLASSIFICATION_MAX_ATTEMPTS",
    "BatchRunResult",
    "ModelEndpoint",
    "ClassificationUnavailableError",
    "estimated_input_characters",
    "estimated_request_count",
    "run_batch",
    "run_plan",
]
