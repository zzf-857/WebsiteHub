"""Full-library LLM reclassification service.

This module aggregates the account's existing sites in Python first,
builds a cost-bounded classification plan with zero model calls,
validates that a valid model Provider exists before proposing,
and applies validated results under optimistic locking.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.agent.provider_binding import resolve_optional_binding
from webhub.bookmarks.classification_batches import (
    ClassificationBatchBudget,
    ClassificationBatchPlan,
    ClassificationCandidateSource,
    build_candidate_classification_batches,
)
from webhub.bookmarks.classifier import (
    CLASSIFICATION_MAX_ATTEMPTS,
    ClassificationUnavailableError,
    estimated_input_characters,
    estimated_request_count,
    run_plan,
)
from webhub.config import get_settings
from webhub.db.locking import reserve_account_taxonomy
from webhub.db.models import Category, Site, utc_now

RECLASSIFICATION_MAX_BATCHES = 50
RECLASSIFICATION_MAX_TOTAL_PAYLOAD_BYTES = 256 * 1024


class ReclassificationError(RuntimeError):
    """Raised when reclassification fails validation or DB state checks."""

    def __init__(
        self,
        message: str,
        safe_message: str | None = None,
        *,
        status_code: int = 400,
    ) -> None:
        super().__init__(message)
        self.safe_message = safe_message or message
        self.status_code = status_code


def _incomplete_plan_reason(plan: ClassificationBatchPlan) -> str | None:
    if plan.privacy_excluded_source_ids or plan.privacy_excluded_member_source_ids:
        return "网址库包含不能安全发送给模型的本地或私网网址，未发起任何模型请求。"
    if plan.budget_exhausted_source_ids:
        return "网址库规模超出当前全量重分类上限，未发起任何模型请求。"
    return None


def _extract_hostname(url: str) -> str:
    """Extract a safe hostname string from a site URL."""
    try:
        parsed = urlparse(url)
        host = parsed.netloc.split(":")[0].strip().lower()
        if host:
            return host
    except Exception:
        pass
    return "unknown.host"


def prepare_reclassification_sources(
    sites: Sequence[Site],
    cat_by_id: dict[str, str],
) -> tuple[tuple[ClassificationCandidateSource, ...], dict[str, list[str]]]:
    """Aggregate sites by hostname to ensure duplicate sites do not duplicate payload.

    Returns:
        (sources, source_id_to_site_ids_mapping)
    """
    sites_by_host: dict[str, list[Site]] = {}
    for site in sites:
        host = _extract_hostname(site.original_url)
        sites_by_host.setdefault(host, []).append(site)

    sources: list[ClassificationCandidateSource] = []
    source_to_sites: dict[str, list[str]] = {}

    for host, host_sites in sites_by_host.items():
        source_id = f"src_{host.replace('.', '_')}"
        representative_title = host_sites[0].name
        folder_label = cat_by_id.get(host_sites[0].category_id, "未分类")

        site_ids = [s.id for s in host_sites]
        source_to_sites[source_id] = site_ids

        sources.append(
            ClassificationCandidateSource(
                source_id=source_id,
                title=representative_title,
                hostname=host,
                folder_labels=(folder_label,),
                occurrence_count=len(host_sites),
            )
        )

    return tuple(sources), source_to_sites


async def propose_reclassification(
    session: AsyncSession,
    user_id: str,
) -> dict[str, Any]:
    """Build a proposal for reclassifying all sites in the account's library.

    First checks for an active model Provider binding. If missing, rejects immediately
    without generating a non-functional proposal. Costs ZERO tokens.
    """
    settings = get_settings()
    binding = await resolve_optional_binding(session, settings, user_id=user_id, kind="model")
    if binding is None:
        return {
            "status": "rejected",
            "reason": "当前账号尚未配置或启用模型 Provider，无法开展全库重分类。",
        }

    stmt = select(Site).where(Site.user_id == user_id)
    sites = list((await session.execute(stmt)).scalars().all())

    if not sites:
        return {
            "status": "noop",
            "message": "网址库中没有需要分类的网站。",
        }

    cat_stmt = select(Category).where(Category.user_id == user_id)
    categories = list((await session.execute(cat_stmt)).scalars().all())
    cat_mapping = {c.id: c.name for c in categories} if categories else {"cat_default": "通用"}

    sources, source_to_sites = prepare_reclassification_sources(sites, cat_mapping)
    budget = ClassificationBatchBudget(
        max_batches=RECLASSIFICATION_MAX_BATCHES,
        max_total_payload_bytes=RECLASSIFICATION_MAX_TOTAL_PAYLOAD_BYTES,
    )
    plan = build_candidate_classification_batches(
        candidates=sources,
        allowed_categories=cat_mapping,
        allowed_tags=(),
        include_tags=False,
        requested_language="zh-CN",
        budget=budget,
    )
    if incomplete_reason := _incomplete_plan_reason(plan):
        return {
            "status": "rejected",
            "reason": incomplete_reason,
        }

    req_count = estimated_request_count(plan)
    input_chars = estimated_input_characters(plan)

    return {
        "status": "awaiting_confirmation",
        "message": "全库重分类草稿已生成，请在界面确认预估 Token/请求消耗后开始分类。",
        "draft": {
            "kind": "reclassify",
            "site_count": len(sites),
            "estimated_request_count": req_count,
            "maximum_request_count": req_count * CLASSIFICATION_MAX_ATTEMPTS,
            "estimated_input_characters": input_chars,
            "allowed_categories": list(cat_mapping.values()),
            "expected_categories": cat_mapping,
            "expected_versions": {s.id: s.version for s in sites},
        },
    }


async def apply_reclassification(
    session: AsyncSession,
    user_id: str,
    expected_versions: dict[str, int],
    *,
    expected_categories: dict[str, str],
    cancel_requested: Callable[[], Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    """Execute classification via user's model Provider and update database.

    Enforces optimistic locking per site version.
    """
    settings = get_settings()
    binding = await resolve_optional_binding(session, settings, user_id=user_id, kind="model")
    if binding is None:
        raise ReclassificationError("当前账号未配置或启用模型 Provider")

    stmt = select(Site).where(Site.user_id == user_id)
    sites = list((await session.execute(stmt)).scalars().all())
    site_map = {s.id: s for s in sites}

    if set(expected_versions) != set(site_map):
        raise ReclassificationError(
            "reclassification draft does not cover the current library",
            safe_message="网址库状态已发生变化，请重新发起重分类草稿。",
            status_code=409,
        )

    for site_id, exp_ver in expected_versions.items():
        if site_id not in site_map:
            raise ReclassificationError(f"网站 {site_id} 不存在或已被删除")
        if site_map[site_id].version != exp_ver:
            raise ReclassificationError(
                f"网站“{site_map[site_id].name}”在生成方案后已被改动，请重新发起提案。",
                safe_message="网址库状态已发生变化，请重新发起重分类草稿。",
                status_code=409,
            )

    cat_stmt = select(Category).where(Category.user_id == user_id)
    categories = list((await session.execute(cat_stmt)).scalars().all())
    cat_mapping = {c.id: c.name for c in categories} if categories else {"cat_default": "通用"}
    if cat_mapping != expected_categories:
        raise ReclassificationError(
            "reclassification draft taxonomy does not match the current library",
            safe_message="分类结构已发生变化，请重新发起重分类草稿。",
            status_code=409,
        )
    allowed_category_ids = {c.id for c in categories}

    sources, source_to_sites = prepare_reclassification_sources(sites, cat_mapping)
    budget = ClassificationBatchBudget(
        max_batches=RECLASSIFICATION_MAX_BATCHES,
        max_total_payload_bytes=RECLASSIFICATION_MAX_TOTAL_PAYLOAD_BYTES,
    )
    plan = build_candidate_classification_batches(
        candidates=sources,
        allowed_categories=cat_mapping,
        allowed_tags=(),
        include_tags=False,
        requested_language="zh-CN",
        budget=budget,
    )
    if incomplete_reason := _incomplete_plan_reason(plan):
        raise ReclassificationError(
            "reclassification plan does not cover the complete library",
            safe_message=incomplete_reason,
            status_code=422,
        )

    # Do not hold a SQLite read transaction open across minutes of Provider I/O.
    # Everything needed to validate the answer is now held in immutable projections.
    await session.rollback()

    try:
        # Four concurrent calls bound even the disclosed retry ceiling
        # below the web request window without launching the whole plan at once.
        batch_results = await run_plan(
            binding,
            plan,
            max_concurrency=4,
            cancel_requested=cancel_requested,
        )
    except ClassificationUnavailableError as err:
        raise ReclassificationError(err.safe_message) from err

    # A reverse proxy can close its upstream socket while FastAPI keeps running.
    # Never turn that browser-visible failure into a surprise write minutes later.
    if cancel_requested is not None and await cancel_requested():
        raise ReclassificationError(
            "reclassification request disconnected before commit",
            safe_message="连接已中断，重分类结果未写入，请重新发起操作。",
        )

    target_category_by_site_id: dict[str, str] = {}
    for res in batch_results:
        # res.mappings 里是 BoundClassificationMapping：只有 source_id / mapping /
        # used_fallback 三个字段（slots=True）。模型给的分类结果在 .mapping 里，
        # 不在这一层——直接 getattr(bound, "subject_id") 恒为 None，会让整个循环
        # 每条都 continue、一个站点都不更新，却仍然返回 status="success"。
        for bound in res.mappings:
            site_ids = source_to_sites.get(bound.source_id)
            if not site_ids:
                continue

            mapping = bound.mapping
            # 只认 existing：本路径的 max_new_categories 恒为 0（见
            # build_candidate_classification_batches），propose 会在校验层就被拒，
            # uncategorized 表示模型自认证据不足，不该拿它去覆盖现有分类。
            if mapping.category_action != "existing" or not mapping.category_id:
                continue
            if mapping.category_id not in allowed_category_ids:
                continue

            for s_id in site_ids:
                if s_id in site_map:
                    target_category_by_site_id[s_id] = mapping.category_id

    # Reserve the account taxonomy before the fresh snapshot. This is the same
    # mutex used by ordinary category/tag writes and site moves.
    try:
        if not await reserve_account_taxonomy(session, user_id):
            await session.rollback()
            raise ReclassificationError(
                "account disappeared before reclassification commit",
                safe_message="账号状态已发生变化，请重新发起重分类草稿。",
                status_code=409,
            )
    except SQLAlchemyError as error:
        await session.rollback()
        raise ReclassificationError(
            "could not acquire the reclassification write lock",
            safe_message="网址库正忙，重分类结果未写入，请稍后重新发起操作。",
            status_code=503,
        ) from error
    current_rows = (
        await session.execute(
            select(Site.id, Site.version, Site.category_id, Site.position).where(
                Site.user_id == user_id
            )
        )
    ).all()
    current_states = {
        site_id: (version, category_id, position)
        for site_id, version, category_id, position in current_rows
    }
    if set(current_states) != set(expected_versions) or any(
        current_states[site_id][0] != expected_version
        for site_id, expected_version in expected_versions.items()
    ):
        await session.rollback()
        raise ReclassificationError(
            "library changed while reclassification was running",
            safe_message="网址库状态已发生变化，请重新发起重分类草稿。",
            status_code=409,
        )

    current_category_mapping = dict(
        (
            await session.execute(
                select(Category.id, Category.name).where(Category.user_id == user_id)
            )
        ).all()
    )
    if current_category_mapping != expected_categories:
        await session.rollback()
        raise ReclassificationError(
            "category taxonomy changed while reclassification was running",
            safe_message="分类结构已发生变化，请重新发起重分类草稿。",
            status_code=409,
        )

    moves = [
        (site_id, target_category_id)
        for site_id, target_category_id in target_category_by_site_id.items()
        if current_states[site_id][1] != target_category_id
    ]
    ordered_moves = sorted(
        moves,
        key=lambda move: (move[1], current_states[move[0]][2], move[0]),
    )
    target_category_ids = {target_category_id for _, target_category_id in moves}
    max_positions: dict[str, int] = {}
    if target_category_ids:
        position_rows = await session.execute(
            select(Site.category_id, func.max(Site.position))
            .where(
                Site.user_id == user_id,
                Site.category_id.in_(target_category_ids),
            )
            .group_by(Site.category_id)
        )
        max_positions = {
            category_id: int(max_position)
            for category_id, max_position in position_rows
            if max_position is not None
        }

    next_positions = {
        category_id: max_positions.get(category_id, -1) + 1
        for category_id in target_category_ids
    }

    # Claims and position lookups can take time on a large library. Recheck at
    # the last safe point before applying material changes and committing them.
    if cancel_requested is not None and await cancel_requested():
        await session.rollback()
        raise ReclassificationError(
            "reclassification request disconnected before commit",
            safe_message="连接已中断，重分类结果未写入，请重新发起操作。",
        )

    now = utc_now()
    for site_id, target_category_id in ordered_moves:
        applied = await session.execute(
            update(Site)
            .where(
                Site.user_id == user_id,
                Site.id == site_id,
                Site.version == expected_versions[site_id],
            )
            .values(
                category_id=target_category_id,
                position=next_positions[target_category_id],
                version=Site.version + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if applied.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            raise ReclassificationError(
                "site version claim was lost before reclassification commit",
                safe_message="网址库状态已发生变化，请重新发起重分类草稿。",
                status_code=409,
            )
        next_positions[target_category_id] += 1

    if cancel_requested is not None and await cancel_requested():
        await session.rollback()
        raise ReclassificationError(
            "reclassification request disconnected before commit",
            safe_message="连接已中断，重分类结果未写入，请重新发起操作。",
        )

    await session.commit()

    return {
        "status": "success",
        "updated_count": len(moves),
        "total_sites": len(sites),
    }


__all__ = [
    "ReclassificationError",
    "apply_reclassification",
    "prepare_reclassification_sources",
    "propose_reclassification",
]
