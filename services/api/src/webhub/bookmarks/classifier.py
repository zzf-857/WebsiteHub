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

import json
from collections.abc import Mapping
from dataclasses import dataclass

from webhub.agent.provider_binding import ProviderBinding
from webhub.bookmarks.classification_batches import (
    ClassificationBatch,
    ClassificationBatchPlan,
    validate_classification_batch_output,
)
from webhub.bookmarks.classification_contract import ClassificationOutputError

# The response is a bounded JSON object; anything much larger than this is a
# model that has stopped following the schema.
MAX_RESPONSE_CHARS = 60_000


class ClassificationUnavailableError(RuntimeError):
    """The account's model Provider could not produce a usable batch answer."""

    safe_message = "自动分类暂时不可用，请稍后重试或检查模型 Provider 配置。"


@dataclass(frozen=True, slots=True)
class BatchRunResult:
    batch_id: str
    mappings: tuple[object, ...]
    unresolved_source_ids: tuple[str, ...]


# Worked examples, not prose rules.  Two of them, chosen to demonstrate the two
# decisions that actually go wrong: reusing an existing category instead of
# minting a near-duplicate, and admitting "uncategorized" instead of guessing.
_FEW_SHOT = """\
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

_SYSTEM_PROMPT = f"""你是一个网址分类器。输入是若干「主题」，每个主题代表一组网站\
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
- **tags 必须给 2 到 8 个**（category_action 为 existing 或 propose 时）。
  这是硬性约定，给少了整批答案都会被判为非法。uncategorized 时 tags 必须是空数组。
- reason_code 只能是 folder_match / host_match / title_match / mixed_evidence /
  insufficient_evidence 五者之一。
- confidence 是 0 到 1 的小数。

## 你看不到的东西
输入里只有文件夹名、少量标题样本和主机名。**没有完整 URL、没有页面内容。**
不要假装读过网页，不要基于想象补充信息。

{_FEW_SHOT}"""


def _messages(payload: Mapping[str, object]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
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


async def _ask_model(binding: ProviderBinding, payload: Mapping[str, object]) -> str:
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
        async with httpx.AsyncClient(
            timeout=binding.timeout_seconds,
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
                    f"classification provider returned {response.status_code}"
                )
            data = response.json()
    except ClassificationUnavailableError:
        raise
    except Exception as error:  # noqa: BLE001 - vendor errors must not escape
        raise ClassificationUnavailableError("classification request failed") from error

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ClassificationUnavailableError("classification response was not usable") from error
    if not isinstance(content, str) or not content.strip():
        raise ClassificationUnavailableError("classification response was empty")
    if len(content) > MAX_RESPONSE_CHARS:
        raise ClassificationUnavailableError("classification response was too large")
    return _stripped_json(content)


async def run_batch(binding: ProviderBinding, batch: ClassificationBatch) -> BatchRunResult:
    """Classify one bounded batch and bind the answer back to local ids.

    The model only ever sees opaque subject ids, so a wrong or hostile answer
    cannot reach beyond the subjects in this batch: ``validate_classification_batch_output``
    rejects unknown ids and materialises ``未分类/待复核`` for missing ones.
    """

    answer = await _ask_model(binding, batch.provider_payload())
    try:
        validated = validate_classification_batch_output(batch, answer)
    except (ClassificationOutputError, ValueError) as error:
        raise ClassificationUnavailableError("classification output failed validation") from error
    return BatchRunResult(
        batch_id=batch.batch_id,
        mappings=tuple(validated.mappings),
        unresolved_source_ids=validated.unresolved_source_ids,
    )


async def run_plan(
    binding: ProviderBinding,
    plan: ClassificationBatchPlan,
) -> list[BatchRunResult]:
    """Run every batch in a plan sequentially.

    Sequential on purpose: these calls spend the user's own quota, and a burst
    of parallel requests is the fastest way to hit a vendor rate limit and lose
    the whole run.
    """

    results: list[BatchRunResult] = []
    for batch in plan.batches:
        results.append(await run_batch(binding, batch))
    return results


def estimated_request_count(plan: ClassificationBatchPlan) -> int:
    """How many model calls running this plan would cost, before spending any."""

    return len(plan.batches)


def estimated_input_characters(plan: ClassificationBatchPlan) -> int:
    """Rough size of what would be sent, so a caller can show a cost preview."""

    return sum(
        len(json.dumps(batch.provider_payload(), ensure_ascii=False, separators=(",", ":")))
        for batch in plan.batches
    ) + len(_SYSTEM_PROMPT) * max(1, len(plan.batches))


__all__ = [
    "BatchRunResult",
    "ClassificationUnavailableError",
    "estimated_input_characters",
    "estimated_request_count",
    "run_batch",
    "run_plan",
]
