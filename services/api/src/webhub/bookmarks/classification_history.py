"""Reuse an account's trusted host/category decisions without model calls."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.bookmarks.models import CategorySuggestion
from webhub.db.models import Category, Site, SiteMetadataPreference

from .similarity import site_key_for_url

# These hosts contain unrelated user/project content. Host equality does not
# mean semantic equality on them, so account history must not propagate there.
_SHARED_CONTENT_HOSTS = frozenset(
    {
        "bilibili.com",
        "docs.google.com",
        "drive.google.com",
        "facebook.com",
        "figma.com",
        "gitee.com",
        "github.com",
        "github.io",
        "gitlab.com",
        "google.com",
        "linkedin.com",
        "medium.com",
        "notion.so",
        "notion.site",
        "reddit.com",
        "substack.com",
        "twitter.com",
        "x.com",
        "youtube.com",
        "youtu.be",
        "zhihu.com",
    }
)


@dataclass(frozen=True, slots=True)
class HostCategoryRecord:
    url_or_host: str
    category_name: str
    is_default: bool = False
    is_manual: bool = False
    is_llm: bool = False


def normalized_history_host(value: str) -> str | None:
    """Return the same authority key used by bookmark similarity clusters."""

    candidate = value.strip()
    if not candidate:
        return None
    return site_key_for_url(candidate if "://" in candidate else f"https://{candidate}")


def _hostname_from_site_key(site_key: str) -> str | None:
    try:
        return urlsplit(f"https://{site_key}").hostname
    except ValueError:
        return None


def _is_shared_content_host(site_key: str) -> bool:
    # Compare the hostname separately so a non-default port cannot bypass the
    # shared-platform safety exclusion.
    host = _hostname_from_site_key(site_key)
    return bool(
        host
        and any(host == shared or host.endswith(f".{shared}") for shared in _SHARED_CONTENT_HOSTS)
    )


def _conflict(reason: str, count: int) -> CategorySuggestion:
    return CategorySuggestion("未分类", "ambiguous", (f"history:{reason}:{count}",))


def build_host_category_history(
    records: list[HostCategoryRecord],
) -> dict[str, CategorySuggestion]:
    """Aggregate only decisions strong enough to propagate to another page."""

    grouped: defaultdict[str, list[HostCategoryRecord]] = defaultdict(list)
    for record in records:
        host = normalized_history_host(record.url_or_host)
        if host is None or _is_shared_content_host(host):
            continue
        grouped[host].append(record)

    history: dict[str, CategorySuggestion] = {}
    for host, host_records in grouped.items():
        manual = [record for record in host_records if record.is_manual]
        if manual:
            categories = {record.category_name for record in manual}
            if len(categories) != 1:
                history[host] = _conflict("manual_conflict", len(manual))
                continue
            history[host] = CategorySuggestion(
                next(iter(categories)),
                "high",
                (f"history:manual:{len(manual)}",),
            )
            continue

        categorized = [record for record in host_records if not record.is_default]
        categories = {record.category_name for record in categorized}
        if len(categories) > 1:
            history[host] = _conflict("category_conflict", len(categorized))
            continue
        if not categorized:
            continue

        llm = [record for record in categorized if record.is_llm]
        if llm:
            history[host] = CategorySuggestion(
                categorized[0].category_name,
                "high",
                (f"history:llm:{len(llm)}",),
            )
            continue

        # Rule/import history is weaker than an explicit manual or LLM choice.
        # Require repetition and at least 80% coverage, so a single accidental
        # classification never fans out across a large bookmark import.
        if len(categorized) >= 2 and len(categorized) / len(host_records) >= 0.8:
            history[host] = CategorySuggestion(
                categorized[0].category_name,
                "high",
                (f"history:consistent:{len(categorized)}/{len(host_records)}",),
            )
    return history


async def load_account_host_category_history(
    session: AsyncSession,
    user_id: str,
) -> dict[str, CategorySuggestion]:
    """Load one account's history in one bounded database round trip."""

    rows = await session.execute(
        select(
            Site.identity_url,
            Category.name,
            Category.is_default,
            SiteMetadataPreference.category_is_manual,
            SiteMetadataPreference.category_is_llm,
        )
        .join(
            Category,
            and_(Category.user_id == Site.user_id, Category.id == Site.category_id),
        )
        .outerjoin(
            SiteMetadataPreference,
            and_(
                SiteMetadataPreference.user_id == Site.user_id,
                SiteMetadataPreference.site_id == Site.id,
            ),
        )
        .where(Site.user_id == user_id, Category.user_id == user_id)
    )
    records = [
        HostCategoryRecord(
            url_or_host=identity_url,
            category_name=category_name,
            is_default=bool(is_default),
            is_manual=bool(is_manual),
            is_llm=bool(is_llm),
        )
        for identity_url, category_name, is_default, is_manual, is_llm in rows.all()
    ]
    return build_host_category_history(records)


__all__ = [
    "HostCategoryRecord",
    "build_host_category_history",
    "load_account_host_category_history",
    "normalized_history_host",
]
