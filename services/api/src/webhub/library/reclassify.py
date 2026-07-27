"""Full-library LLM reclassification service.

This module aggregates the account's existing sites in Python first,
builds a cost-bounded classification plan with zero model calls,
validates that a valid model Provider exists before proposing,
and applies validated results under optimistic locking.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.agent.provider_binding import resolve_optional_binding
from webhub.bookmarks.classification_batches import (
    ClassificationBatchBudget,
    ClassificationCandidateSource,
    build_candidate_classification_batches,
)
from webhub.bookmarks.classifier import (
    ClassificationUnavailableError,
    estimated_input_characters,
    estimated_request_count,
    run_plan,
)
from webhub.config import get_settings
from webhub.db.models import Category, Site, utc_now


class ReclassificationError(RuntimeError):
    """Raised when reclassification fails validation or DB state checks."""

    def __init__(self, message: str, safe_message: str | None = None) -> None:
        super().__init__(message)
        self.safe_message = safe_message or message


@dataclass(frozen=True, slots=True)
class ReclassificationProposal:
    site_count: int
    estimated_request_count: int
    estimated_input_characters: int
    allowed_categories: tuple[str, ...]
    sites_summary: tuple[dict[str, Any], ...]


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
            "message": "资料库中没有需要分类的网站。",
        }

    cat_stmt = select(Category).where(Category.user_id == user_id)
    categories = list((await session.execute(cat_stmt)).scalars().all())
    cat_mapping = {c.id: c.name for c in categories} if categories else {"cat_default": "通用"}

    sources, source_to_sites = prepare_reclassification_sources(sites, cat_mapping)
    budget = ClassificationBatchBudget(
        max_batches=50,
        max_total_payload_bytes=256 * 1024,
    )
    plan = build_candidate_classification_batches(
        candidates=sources,
        allowed_categories=cat_mapping,
        allowed_tags=(),
        requested_language="zh-CN",
        budget=budget,
    )

    req_count = estimated_request_count(plan)
    input_chars = estimated_input_characters(plan)

    return {
        "status": "awaiting_confirmation",
        "message": "全库重分类草稿已生成，请在界面确认预估 Token/请求消耗后开始分类。",
        "draft": {
            "kind": "reclassify",
            "site_count": len(sites),
            "estimated_request_count": req_count,
            "estimated_input_characters": input_chars,
            "allowed_categories": list(cat_mapping.values()),
            "expected_versions": {s.id: s.version for s in sites},
        },
    }


async def apply_reclassification(
    session: AsyncSession,
    user_id: str,
    expected_versions: dict[str, int],
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

    for site_id, exp_ver in expected_versions.items():
        if site_id not in site_map:
            raise ReclassificationError(f"网站 {site_id} 不存在或已被删除")
        if site_map[site_id].version != exp_ver:
            raise ReclassificationError(
                f"网站“{site_map[site_id].name}”在生成方案后已被改动，请重新发起提案。",
                safe_message="资料库状态已发生变化，请重新发起重分类草稿。",
            )

    cat_stmt = select(Category).where(Category.user_id == user_id)
    categories = list((await session.execute(cat_stmt)).scalars().all())
    cat_mapping = {c.id: c.name for c in categories} if categories else {"cat_default": "通用"}

    sources, source_to_sites = prepare_reclassification_sources(sites, cat_mapping)
    budget = ClassificationBatchBudget(
        max_batches=50,
        max_total_payload_bytes=256 * 1024,
    )
    plan = build_candidate_classification_batches(
        candidates=sources,
        allowed_categories=cat_mapping,
        allowed_tags=(),
        requested_language="zh-CN",
        budget=budget,
    )

    try:
        batch_results = await run_plan(binding, plan)
    except ClassificationUnavailableError as err:
        raise ReclassificationError(err.safe_message) from err

    cat_by_id = {c.id: c for c in categories}

    updated_count = 0
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
            target_cat = cat_by_id.get(mapping.category_id)
            if target_cat is None:
                continue

            for s_id in site_ids:
                site = site_map.get(s_id)
                if site is not None and site.category_id != target_cat.id:
                    site.category_id = target_cat.id
                    site.version += 1
                    site.updated_at = utc_now()
                    updated_count += 1

    await session.commit()

    return {
        "status": "success",
        "updated_count": updated_count,
        "total_sites": len(sites),
    }


__all__ = [
    "ReclassificationError",
    "ReclassificationProposal",
    "apply_reclassification",
    "prepare_reclassification_sources",
    "propose_reclassification",
]
