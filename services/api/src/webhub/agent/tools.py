"""Account-scoped tools the Agent may call during one turn.

Two rules govern everything in this module.

**Scope is bound by the server, never by the model.**  ``user_id`` is captured
in a closure from the authenticated session; no tool takes an account
parameter, so no prompt injection can widen the blast radius.  Every query
goes through the existing library/spaces services, which already enforce
per-account ownership.

**Reads are free, writes are proposals.**  The Agent may look at anything the
account owns, but it cannot create or modify anything on its own: every
``propose_*`` tool returns a draft that the browser must confirm, and the write
that follows goes through the ordinary library/spaces endpoints authorised by
the user's own session.  That keeps the human-in-the-loop confirmation from
Implementation Plan §5.4 honest even if the model is talked into "just saving
it".  Modifying drafts additionally carry the row's current ``version``, so a
change made elsewhere between proposal and click surfaces as a conflict rather
than silently overwriting.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Self
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from webhub.bookmarks import persistence
from webhub.bookmarks import queries as bookmark_queries
from webhub.bookmarks.apply import category_distribution
from webhub.bookmarks.models import NormalizationStatus
from webhub.bookmarks.normalization import normalize_bookmark_url
from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.models import BookmarkImportJob, Site, SpaceMember
from webhub.library import service as library_service
from webhub.library.batch import extract_urls, preview_batch
from webhub.library.schemas import MAX_BATCH_URLS
from webhub.library.service import LibraryError
from webhub.spaces import service as spaces_service
from webhub.spaces.service import SpaceError

from .provider_binding import ProviderBinding
from .web_search import MAX_RESULTS as MAX_WEB_SEARCH_RESULTS
from .web_search import WebSearchUnavailableError, search_web

# Provenance markers required by the todolist: the user must always be able to
# tell a stored bookmark from something the model produced.
SOURCE_LIBRARY = "站内存储数据"
SOURCE_WEB = "联网搜索"
SOURCE_MODEL = "llm推荐"

MAX_TOOL_LIMIT = 20
MAX_PROPOSAL_TEXT_LENGTH = 20_000
RECOMMENDATION_MANIFEST_VERSION = 2
_COLLECTION_ACTION_SUFFIX = re.compile(
    r"(?:/|\s|[，,。:：;；])+(?:入库|收藏|保存|收录)\s*[.!。！?？]*\s*$",
    re.IGNORECASE,
)


class SearchLibraryArgs(BaseModel):
    query: str = Field(
        default="",
        max_length=200,
        description="关键字，可留空表示不过滤。支持中文与英文分词。",
    )
    limit: int = Field(default=8, ge=1, le=MAX_TOOL_LIMIT, description="返回条数上限")
    pinned_only: bool = Field(default=False, description="只看星标（常用）网站")
    category_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=36,
        description="只检索这个分类；必须先从 list_categories 获取真实 ID",
    )
    include_all: bool = Field(
        default=False,
        description=(
            "用户明确要求全部结果时设为 true。模型仍只看到有限预览，完整结果由界面分页展示"
        ),
    )


class SiteIdArgs(BaseModel):
    site_id: str = Field(min_length=1, max_length=36, description="站内网站 ID")


class EmptyArgs(BaseModel):
    pass


class WebSearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=200, description="联网搜索关键字")
    limit: int = Field(
        default=5,
        ge=1,
        le=MAX_WEB_SEARCH_RESULTS,
        description="返回条数上限",
    )


class WebsiteRecommendation(BaseModel):
    name: str = Field(min_length=1, max_length=160, description="网站的准确名称")
    url: str = Field(
        min_length=1,
        max_length=2_000,
        description="网站的完整 http(s) 地址，不能只给名称或编造地址",
    )
    description: str = Field(
        default="",
        max_length=400,
        description="说明推荐理由的一句话简介，不使用 Markdown",
    )


class PresentWebsiteRecommendationsArgs(BaseModel):
    items: list[WebsiteRecommendation] = Field(
        default_factory=list,
        max_length=12,
        description="本轮筛选后的推荐清单；展示完整检索结果时留空并改传 result_set_id",
    )
    result_set_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=36,
        description="search_library(include_all=true) 在本轮返回的完整结果集 ID",
    )

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if bool(self.items) == bool(self.result_set_id):
            raise ValueError("items 与 result_set_id 必须且只能提供一个")
        return self


class ProposeSiteArgs(BaseModel):
    url: str = Field(min_length=1, max_length=2_000, description="网站地址，必须是 http/https")
    name: str = Field(min_length=1, max_length=160, description="网站名称")
    description: str = Field(default="", max_length=1_000, description="一句话说明网站做什么")
    category: str = Field(default="", max_length=80, description="分类，每个网站只能有一个")
    tags: list[str] = Field(
        default_factory=list,
        max_length=12,
        description="细粒度标签；已有同义或近义标签时必须复用 list_tags 返回的原名称",
    )


class ProposeSiteUpdateArgs(BaseModel):
    """Every editable field is optional and defaults to ``None`` = "leave alone".

    ``None`` and "cleared" must stay distinguishable: omitting ``description``
    keeps the current text, while passing ``""`` clears it.  Collapsing the two
    would let a rename silently wipe a description the user never mentioned.
    """

    site_id: str = Field(min_length=1, max_length=36, description="要修改的站内网站 ID")
    name: str | None = Field(default=None, max_length=160, description="新的网站名称，不改就不要传")
    description: str | None = Field(
        default=None,
        max_length=1_000,
        description="新的一句话说明；传空字符串表示清空，不改就不要传",
    )
    category: str | None = Field(
        default=None,
        max_length=80,
        description="新的分类名（每个网站只能有一个），不改就不要传",
    )
    tags: list[str] | None = Field(
        default=None,
        max_length=12,
        description=(
            "新的标签集合，会整体替换原有标签；已有同义或近义标签时必须复用其原名称；"
            "不改就不要传"
        ),
    )
    pinned: bool | None = Field(default=None, description="是否置顶（星标），不改就不要传")


class ProposeSpaceMembershipArgs(BaseModel):
    site_id: str = Field(min_length=1, max_length=36, description="站内网站 ID")
    space: str = Field(min_length=1, max_length=120, description="Space 的名称或 ID")
    action: Literal["remove"] = Field(description="remove=从已有 Space 移出")


class ProposeSpaceBatchArgs(BaseModel):
    space_name: str = Field(
        min_length=1,
        max_length=120,
        description="要创建或使用的 Space 准确名称",
    )
    site_ids: list[str] = Field(
        default_factory=list,
        max_length=100,
        description=(
            "要一次性加入的站内网站 ID 完整清单；纯创建空 Space 时传空数组。"
            "修改上一张待确认草稿时，必须传剔除后剩余的完整清单"
        ),
    )


def _site_summary(site: Any) -> dict[str, Any]:
    return {
        "site_id": site.id,
        "name": site.name,
        "url": site.original_url,
        "favicon_url": site.favicon_url,
        "summary": site.summary,
        "description": site.description,
        "category": site.category.name,
        "tags": [tag.name for tag in site.tags],
        "pinned": site.pinned,
    }


def _selection_site_summary(site: Any) -> dict[str, Any]:
    """Keep an all-results snapshot small while retaining every clickable field."""

    return {
        "site_id": site.id,
        "name": site.name,
        "url": site.original_url,
        "favicon_url": site.favicon_url,
    }


@dataclass(frozen=True, slots=True)
class AgentToolContext:
    """Everything a tool needs, with the account scope already fixed."""

    database: Database
    settings: Settings
    user_id: str
    search_binding: ProviderBinding | None = None
    _library_result_sets: dict[str, tuple[dict[str, Any], ...]] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )

    def register_library_result_set(self, items: Sequence[dict[str, Any]]) -> str:
        result_set_id = str(uuid4())
        self._library_result_sets[result_set_id] = tuple(dict(item) for item in items)
        return result_set_id

    def library_result_set(self, result_set_id: str) -> tuple[dict[str, Any], ...] | None:
        return self._library_result_sets.get(result_set_id)


async def _search_library(context: AgentToolContext, args: SearchLibraryArgs) -> dict[str, Any]:
    async with context.database.sessions() as session:
        try:
            if args.include_all:
                selection = await library_service.list_site_selection(
                    session,
                    context.user_id,
                    q=args.query.strip() or None,
                    category_id=args.category_id,
                    tag_id=None,
                    pinned=True if args.pinned_only else None,
                )
                items = sorted(
                    (_selection_site_summary(site) for site in selection.items),
                    key=lambda item: (str(item["name"]).casefold(), str(item["site_id"])),
                )
                result_set_id = (
                    context.register_library_result_set(items) if items else None
                )
                return {
                    "source": SOURCE_LIBRARY,
                    "matched_count": len(items),
                    "items": items[: min(args.limit, MAX_TOOL_LIMIT)],
                    "complete_result_set": bool(result_set_id),
                    **({"result_set_id": result_set_id} if result_set_id is not None else {}),
                }
            listing = await library_service.list_sites(
                session,
                context.user_id,
                q=args.query.strip() or None,
                category_id=args.category_id,
                tag_id=None,
                space_id=None,
                pinned=True if args.pinned_only else None,
                sort="updated",
                direction="desc",
                cursor=None,
                limit=min(args.limit, MAX_TOOL_LIMIT),
            )
        except LibraryError as error:
            return {"source": SOURCE_LIBRARY, "error": error.message, "items": []}
    return {
        "source": SOURCE_LIBRARY,
        "matched_count": listing.aggregate.matched_count,
        "items": [_site_summary(site) for site in listing.items],
    }


async def _get_site_detail(context: AgentToolContext, args: SiteIdArgs) -> dict[str, Any]:
    async with context.database.sessions() as session:
        try:
            site = await library_service.get_site(session, context.user_id, args.site_id)
        except LibraryError as error:
            return {"source": SOURCE_LIBRARY, "error": error.message}
    detail = _site_summary(site)
    detail.update(
        {
            "source": SOURCE_LIBRARY,
            "analysis_status": site.analysis_status,
            "created_at": site.created_at.isoformat(),
            "updated_at": site.updated_at.isoformat(),
        }
    )
    return detail


async def _list_categories(context: AgentToolContext, _: EmptyArgs) -> dict[str, Any]:
    async with context.database.sessions() as session:
        listing = await library_service.list_categories(session, context.user_id)
    return {
        "source": SOURCE_LIBRARY,
        "items": [
            {"id": item.id, "name": item.name, "site_count": item.site_count}
            for item in listing.items
        ],
    }


async def _list_tags(context: AgentToolContext, _: EmptyArgs) -> dict[str, Any]:
    async with context.database.sessions() as session:
        listing = await library_service.list_tags(session, context.user_id)
    return {
        "source": SOURCE_LIBRARY,
        "items": [
            {"id": item.id, "name": item.name, "site_count": item.site_count}
            for item in listing.items
        ],
    }


async def _list_spaces(context: AgentToolContext, _: EmptyArgs) -> dict[str, Any]:
    async with context.database.sessions() as session:
        try:
            listing = await spaces_service.list_spaces(
                session,
                context.user_id,
                sort="updated",
                direction="desc",
                cursor=None,
                limit=MAX_TOOL_LIMIT,
            )
        except SpaceError as error:
            return {"source": SOURCE_LIBRARY, "error": error.message, "items": []}
    return {
        "source": SOURCE_LIBRARY,
        "total_count": listing.aggregate.total_count,
        "items": [
            {"id": item.id, "name": item.name, "member_count": item.member_count}
            for item in listing.items
        ],
    }


async def _web_search(context: AgentToolContext, args: WebSearchArgs) -> dict[str, Any]:
    if context.search_binding is None:
        return {
            "source": SOURCE_WEB,
            "error": "当前账号尚未配置联网搜索 Provider，无法联网检索。",
            "items": [],
        }
    try:
        results = await search_web(context.search_binding, args.query, limit=args.limit)
    except WebSearchUnavailableError:
        return {
            "source": SOURCE_WEB,
            "provider": context.search_binding.display_name,
            "provider_id": context.search_binding.provider,
            "error": WebSearchUnavailableError.safe_message,
            "items": [],
        }
    return {
        "source": SOURCE_WEB,
        "provider": context.search_binding.display_name,
        "provider_id": context.search_binding.provider,
        "items": [result.as_dict() for result in results],
    }


def _recommendation_identity_candidates(url: str) -> list[str]:
    """Return conservative identity variants for an explicit recommendation URL."""

    normalized = normalize_bookmark_url(url)
    if (
        normalized.status is not NormalizationStatus.ACCEPTED
        or normalized.normalized_url is None
        or normalized.host is None
    ):
        return []

    primary = normalized.normalized_url
    parts = urlsplit(primary)
    variants = [primary]
    # A saved root URL commonly differs only by https/http or the conventional
    # ``www`` alias. Keep path/query identical so an arbitrary page on the same
    # large host is never mistaken for the recommendation.
    if parts.port is None:
        hosts = [normalized.host]
        if any(character.isalpha() for character in normalized.host):
            alternate_host = (
                normalized.host.removeprefix("www.")
                if normalized.host.startswith("www.")
                else f"www.{normalized.host}"
            )
            hosts.append(alternate_host)
        for scheme in (parts.scheme, "https", "http"):
            for host in hosts:
                variants.append(
                    urlunsplit((scheme, host, parts.path, parts.query, parts.fragment))
                )
    return list(dict.fromkeys(variants))


async def _present_website_recommendations(
    context: AgentToolContext,
    args: PresentWebsiteRecommendationsArgs,
) -> tuple[str, dict[str, Any]]:
    """Project the model's final recommendations into trusted render data.

    This tool is read-only. The model supplies explicit URLs; the server
    validates and de-duplicates them, then resolves exact saved URL variants so
    those cards open WebHub detail pages instead of leaving the site.
    """

    if args.result_set_id is not None:
        result_set = context.library_result_set(args.result_set_id)
        if result_set is None:
            artifact = {
                "manifest_version": RECOMMENDATION_MANIFEST_VERSION,
                "source": SOURCE_LIBRARY,
                "code": "result_set_unavailable",
                "error": "完整结果集已失效，请重新检索。",
            }
            return _recommendation_model_content(artifact), artifact
        artifact = {
            "manifest_version": RECOMMENDATION_MANIFEST_VERSION,
            "complete": True,
            "result_set_id": args.result_set_id,
            "source": SOURCE_LIBRARY,
            "provider": None,
            "matched_count": len(result_set),
            "items": [dict(item) for item in result_set],
            "rejected_count": 0,
        }
        return _recommendation_model_content(artifact), artifact

    prepared: list[tuple[WebsiteRecommendation, list[str]]] = []
    seen: set[str] = set()
    for item in args.items:
        identities = _recommendation_identity_candidates(item.url.strip())
        if not identities or any(identity in seen for identity in identities):
            continue
        seen.update(identities)
        prepared.append((item, identities))

    stored_ids: dict[str, str] = {}
    stored_sites: dict[str, Any] = {}
    if prepared:
        all_identities = list(
            dict.fromkeys(identity for _, identities in prepared for identity in identities)
        )
        async with context.database.sessions() as session:
            rows = (
                await session.execute(
                    select(Site.id, Site.identity_url).where(
                        Site.user_id == context.user_id,
                        Site.identity_url.in_(all_identities),
                    )
                )
            ).all()
            stored_ids = {identity_url: site_id for site_id, identity_url in rows}
            for site_id in dict.fromkeys(stored_ids.values()):
                stored_sites[site_id] = await library_service.get_site(
                    session,
                    context.user_id,
                    site_id,
                )

    items: list[dict[str, Any]] = []
    for item, identities in prepared:
        site_id = next(
            (stored_ids[identity] for identity in identities if identity in stored_ids),
            None,
        )
        if site_id is not None:
            items.append(_site_summary(stored_sites[site_id]))
            continue
        items.append(
            {
                "name": " ".join(item.name.split()),
                "url": identities[0],
                "favicon_url": None,
                "summary": None,
                "description": " ".join(item.description.split()),
                "category": None,
                "tags": [],
                "pinned": False,
            }
        )

    all_items_are_stored = bool(items) and all("site_id" in item for item in items)
    if all_items_are_stored:
        source = SOURCE_LIBRARY
        provider = None
    elif context.search_binding is not None:
        source = SOURCE_WEB
        provider = context.search_binding.display_name
    else:
        source = SOURCE_MODEL
        provider = SOURCE_MODEL

    artifact = {
        "manifest_version": RECOMMENDATION_MANIFEST_VERSION,
        "complete": True,
        "source": source,
        "provider": provider,
        "matched_count": len(items),
        "items": items,
        "rejected_count": len(args.items) - len(prepared),
    }
    return _recommendation_model_content(artifact), artifact


def _recommendation_model_content(artifact: dict[str, Any]) -> str:
    """Return only bounded status data to the model; cards stay in ToolMessage.artifact."""

    error = artifact.get("error")
    if isinstance(error, str) and error:
        payload = {
            "source": artifact.get("source"),
            "code": artifact.get("code", "recommendation_unavailable"),
            "error": error,
        }
    else:
        items = artifact.get("items")
        payload = {
            "source": artifact.get("source"),
            "matched_count": artifact.get("matched_count", 0),
            "presented_count": len(items) if isinstance(items, list) else 0,
            "rejected_count": artifact.get("rejected_count", 0),
            **(
                {"result_set_id": artifact["result_set_id"]}
                if isinstance(artifact.get("result_set_id"), str)
                else {}
            ),
        }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


async def _propose_site(context: AgentToolContext, args: ProposeSiteArgs) -> dict[str, Any]:
    """Return a draft for the user to confirm; never writes to the database."""

    url = args.url.strip()
    if not url.lower().startswith(("http://", "https://")):
        return {"status": "rejected", "reason": "网址必须以 http:// 或 https:// 开头"}

    duplicate: dict[str, Any] | None = None
    async with context.database.sessions() as session:
        try:
            existing = await library_service.list_sites(
                session,
                context.user_id,
                q=url,
                category_id=None,
                tag_id=None,
                space_id=None,
                pinned=None,
                sort="updated",
                direction="desc",
                cursor=None,
                limit=1,
            )
        except LibraryError:
            existing = None
        if existing is not None and existing.items:
            duplicate = _site_summary(existing.items[0])

    tags = _normalized_tags(args.tags)
    return {
        "status": "awaiting_confirmation",
        "message": "草稿已生成，等待用户在界面上确认后才会写入网址库。",
        "duplicate": duplicate,
        "draft": {
            "url": url,
            "name": args.name.strip(),
            "description": args.description.strip(),
            "category": args.category.strip(),
            "tags": tags,
        },
    }


def _normalized_tags(values: list[str]) -> list[str]:
    """Apply the shared NFKC/whitespace/case-fold identity to proposed tags."""

    seen: dict[str, str] = {}
    for value in values:
        tag = " ".join(unicodedata.normalize("NFKC", value).split())
        if tag:
            seen.setdefault(tag.casefold(), tag)
    return list(seen.values())[:12]


async def _propose_site_update(
    context: AgentToolContext,
    args: ProposeSiteUpdateArgs,
) -> dict[str, Any]:
    """Return a before/after diff for the user to confirm; never writes.

    The draft carries the site's current ``version``.  The browser sends it back
    on confirmation, so a site edited elsewhere between proposal and click makes
    the write fail with a conflict instead of silently overwriting that edit.
    """

    async with context.database.sessions() as session:
        try:
            # Account-scoped by construction: a site_id belonging to another
            # account simply does not resolve.
            site = await library_service.get_site(session, context.user_id, args.site_id)
        except LibraryError as error:
            return {"status": "rejected", "reason": error.message}

    before = _site_summary(site)
    changes: dict[str, Any] = {}

    if args.name is not None:
        name = " ".join(args.name.split())
        if not name:
            return {"status": "rejected", "reason": "网站名称不能为空"}
        if name != site.name:
            changes["name"] = name
    if args.description is not None:
        description = args.description.strip()
        if description != (site.description or ""):
            changes["description"] = description
    if args.category is not None:
        category = " ".join(args.category.split())
        if not category:
            return {"status": "rejected", "reason": "分类不能为空，每个网站必须有一个分类"}
        if category != site.category.name:
            changes["category"] = category
    if args.tags is not None:
        tags = _normalized_tags(args.tags)
        if sorted(tags) != sorted(tag.name for tag in site.tags):
            changes["tags"] = tags
    if args.pinned is not None and args.pinned != site.pinned:
        changes["pinned"] = args.pinned

    if not changes:
        # Saying "nothing to change" beats generating a draft whose confirm
        # button would be a no-op the user cannot tell apart from a real edit.
        return {
            "status": "noop",
            "message": "该网站当前已经是这个状态，没有需要修改的内容。",
            "site": before,
        }

    return {
        "status": "awaiting_confirmation",
        "message": "修改草稿已生成，等待用户在界面上确认后才会写入。",
        "draft": {
            "kind": "site_update",
            "site_id": site.id,
            "expected_version": site.version,
            "before": before,
            "changes": changes,
            # ``after`` is ``before`` with ``changes`` applied, so the card can
            # render a diff without re-implementing the merge in the browser.
            "after": {**before, **changes},
        },
    }


async def _propose_space_membership(
    context: AgentToolContext,
    args: ProposeSpaceMembershipArgs,
) -> dict[str, Any]:
    """Return a draft for removing one existing member; never writes."""

    if args.action != "remove":
        return {
            "status": "rejected",
            "reason": "新增 Space 成员必须使用 propose_space_batch 生成单一批量草稿。",
        }

    async with context.database.sessions() as session:
        try:
            site = await library_service.get_site(session, context.user_id, args.site_id)
        except LibraryError as error:
            return {"status": "rejected", "reason": error.message}

        space = await spaces_service.resolve_space_reference(session, context.user_id, args.space)
        if space is None:
            # Creating a Space is itself a write; propose nothing and let the
            # user (or a later turn) decide, listing what does exist.
            try:
                listing = await spaces_service.list_spaces(
                    session,
                    context.user_id,
                    sort="updated",
                    direction="desc",
                    cursor=None,
                    limit=MAX_TOOL_LIMIT,
                )
                available = [item.name for item in listing.items]
            except SpaceError:
                available = []
            return {
                "status": "rejected",
                "reason": f"没有找到名为“{args.space}”的 Space。",
                "available_spaces": available,
            }

        member = await spaces_service.is_member(session, context.user_id, space.id, site.id)

    if not member:
        return {
            "status": "noop",
            "message": f"“{site.name}”本来就不在 Space“{space.name}”里。",
            "site": _site_summary(site),
        }

    return {
        "status": "awaiting_confirmation",
        "message": "Space 变更草稿已生成，等待用户在界面上确认后才会写入。",
        "draft": {
            "kind": "space_membership",
            "action": args.action,
            "site_id": site.id,
            "site_name": site.name,
            "space_id": space.id,
            "space_name": space.name,
            "expected_version": space.version,
        },
    }


async def _propose_space_batch(
    context: AgentToolContext,
    args: ProposeSpaceBatchArgs,
) -> dict[str, Any]:
    """Propose one Space create/add task; never write any row."""

    space_name = " ".join(unicodedata.normalize("NFKC", args.space_name).split())
    if not space_name:
        return {"status": "rejected", "reason": "Space 名称不能为空"}
    if len(space_name) > 120:
        return {"status": "rejected", "reason": "Space 名称不能超过 120 个字符"}

    site_ids: list[str] = []
    seen_site_ids: set[str] = set()
    for raw_site_id in args.site_ids:
        site_id = raw_site_id.strip()
        if not site_id or len(site_id) > 36:
            return {"status": "rejected", "reason": "候选网站 ID 无效，请重新检索网址库"}
        if site_id not in seen_site_ids:
            seen_site_ids.add(site_id)
            site_ids.append(site_id)

    async with context.database.sessions() as session:
        rows = (
            await session.execute(
                select(Site.id, Site.name, Site.original_url).where(
                    Site.user_id == context.user_id,
                    Site.id.in_(site_ids),
                )
            )
        ).all()
        sites_by_id = {
            site_id: {"site_id": site_id, "name": name, "url": original_url}
            for site_id, name, original_url in rows
        }
        if len(sites_by_id) != len(site_ids):
            return {
                "status": "rejected",
                "reason": "有候选网站不存在或不属于当前账号，请重新检索网址库",
            }

        space = await spaces_service.resolve_space_reference(
            session,
            context.user_id,
            space_name,
        )
        if space is not None:
            if not site_ids:
                return {
                    "status": "noop",
                    "message": f"Space“{space.name}”已经存在，没有需要执行的变更。",
                }
            existing_site_ids = set(
                (
                    await session.scalars(
                        select(SpaceMember.site_id).where(
                            SpaceMember.user_id == context.user_id,
                            SpaceMember.space_id == space.id,
                            SpaceMember.site_id.in_(site_ids),
                        )
                    )
                ).all()
            )
            if len(existing_site_ids) == len(site_ids):
                return {
                    "status": "noop",
                    "message": f"这些网站已经全部在 Space“{space.name}”中。",
                    "sites": [sites_by_id[site_id] for site_id in site_ids],
                }
            target = {
                "mode": "existing",
                "space_id": space.id,
                "space_name": space.name,
                "expected_version": space.version,
            }
        else:
            existing_site_ids = set()
            target = {
                "mode": "create",
                "space_name": space_name,
            }

    sites = [sites_by_id[site_id] for site_id in site_ids]
    return {
        "status": "awaiting_confirmation",
        "message": "Space 任务草稿已生成，用户确认一次后才会整体写入。",
        "draft": {
            "kind": "space_batch",
            "target": target,
            "sites": sites,
            "already_member_count": len(existing_site_ids),
        },
    }


class BookmarkImportIdArgs(BaseModel):
    job_id: str = Field(min_length=1, max_length=36, description="书签导入任务 ID")


async def _list_bookmark_imports(context: AgentToolContext, _: EmptyArgs) -> dict[str, Any]:
    """List the account's recent bookmark import jobs and their state."""

    async with context.database.sessions() as session:
        rows = list(
            (
                await session.execute(
                    select(
                        BookmarkImportJob.id,
                        BookmarkImportJob.state,
                        BookmarkImportJob.version,
                        BookmarkImportJob.created_at,
                    )
                    .where(BookmarkImportJob.user_id == context.user_id)
                    .order_by(BookmarkImportJob.created_at.desc())
                    .limit(MAX_TOOL_LIMIT)
                )
            ).all()
        )
    return {
        "source": SOURCE_LIBRARY,
        "items": [
            {
                "job_id": row[0],
                "state": row[1],
                "job_version": row[2],
                "created_at": row[3].isoformat(),
            }
            for row in rows
        ],
        # Uploading a file is something only the browser can do; say so rather
        # than letting the model imply it could start an import itself.
        "note": "Agent 无法上传文件；书签文件需要用户在界面上传后，这里才会出现任务。",
    }


async def _get_bookmark_import_preview(
    context: AgentToolContext,
    args: BookmarkImportIdArgs,
) -> dict[str, Any]:
    """Return aggregate counts for one import job.

    Deliberately aggregates only.  A 2000-bookmark export has 2000 candidate
    rows; putting them in the model's context would cost hundreds of thousands
    of tokens and tell it nothing these dozen numbers do not.  The per-row data
    stays in the database, where apply reads it directly.
    """

    async with context.database.sessions() as session:
        try:
            summary = await bookmark_queries.get_preview_summary(
                session,
                context.user_id,
                args.job_id,
            )
        except persistence.BookmarkPersistenceError as error:
            return {"source": SOURCE_LIBRARY, "error": error.message}
        distribution = await category_distribution(session, context.user_id, summary.run_id)

    actions = summary.candidate_action_counts
    return {
        "source": SOURCE_LIBRARY,
        "job_id": summary.job_id,
        "job_version": summary.job_version,
        "counts": {
            "folders": summary.folder_count,
            "bookmarks": summary.occurrence_count,
            "unique_candidates": summary.candidate_count,
            "duplicates_merged": summary.duplicate_occurrence_count,
            "sensitive_urls": summary.sensitive_candidate_count,
        },
        "proposed_actions": {
            "create": actions.create,
            "skip_existing": actions.skip_existing,
            "merge_missing_metadata": actions.merge_missing_metadata,
            "reject": actions.reject,
            "needs_review": actions.needs_review,
        },
        "category_distribution": distribution,
    }


async def _propose_bookmark_import(
    context: AgentToolContext,
    args: BookmarkImportIdArgs,
) -> dict[str, Any]:
    """Return a draft for importing one staged job; never writes."""

    async with context.database.sessions() as session:
        try:
            summary = await bookmark_queries.get_preview_summary(
                session,
                context.user_id,
                args.job_id,
            )
        except persistence.BookmarkPersistenceError as error:
            return {"status": "rejected", "reason": error.message}
        distribution = await category_distribution(session, context.user_id, summary.run_id)

    actions = summary.candidate_action_counts
    will_create = actions.create + actions.merge_missing_metadata + actions.skip_existing
    if will_create == 0:
        return {
            "status": "noop",
            "message": "这份书签里没有可以导入的条目。",
        }

    return {
        "status": "awaiting_confirmation",
        "message": "导入草稿已生成，等待用户在界面上确认后才会写入网址库。",
        "draft": {
            "kind": "bookmark_import",
            "job_id": summary.job_id,
            "expected_job_version": summary.job_version,
            "candidate_count": summary.candidate_count,
            "will_import": will_create,
            "will_skip": actions.reject + actions.needs_review,
            "duplicates_merged": summary.duplicate_occurrence_count,
            "category_distribution": distribution,
        },
    }


class ProposeSitesArgs(BaseModel):
    text: str = Field(
        min_length=1,
        max_length=MAX_PROPOSAL_TEXT_LENGTH,
        description="包含一个或多个网址的原文，原样传入即可——服务端会自己把 URL 全部解析出来",
    )


def deterministic_collection_text(
    message: str,
    *,
    slash_command_name: str | None = None,
    slash_command_argument: str = "",
) -> str | None:
    """Return URL text only when the user made an explicit collect request.

    The UI accepts natural Chinese commands in addition to the canonical
    ``/存入 <URL>`` form.  A suffix such as ``/入库`` is user intent, not part of
    the URL path, so remove it before the shared URL extractor sees the text.
    Ordinary questions that merely contain a URL are deliberately left to the
    Agent instead of being turned into write proposals.
    """

    if slash_command_name == "/存入":
        # The leading command already proves intent.  Preserve the argument
        # exactly so a legitimate URL whose real path is `/收藏` is not altered.
        return slash_command_argument.strip()

    suffix = _COLLECTION_ACTION_SUFFIX.search(message)
    if suffix is None:
        return None
    candidate = message[: suffix.start()].rstrip()
    return candidate if extract_urls(candidate) else None


async def propose_sites_from_text(
    context: AgentToolContext,
    text: str,
) -> dict[str, Any]:
    """Generate the existing batch draft without relying on a model tool call."""

    normalized = text.strip()
    if not normalized:
        return {"status": "rejected", "reason": "请在收录命令后提供 http(s) 网址"}
    if len(normalized) > MAX_PROPOSAL_TEXT_LENGTH:
        return {
            "status": "rejected",
            "reason": f"单次收录文本不能超过 {MAX_PROPOSAL_TEXT_LENGTH} 个字符",
        }
    return await _propose_sites(context, ProposeSitesArgs(text=normalized))


async def _propose_sites(context: AgentToolContext, args: ProposeSitesArgs) -> dict[str, Any]:
    """Batch draft.  Extraction happens in code, not by the model looping.

    The model passes the user's text through unchanged; ``extract_urls`` finds
    every address.  That is the point: a loop the model can forget to run is
    not a loop, and "did it handle all ten?" stops being a question.
    """

    urls = extract_urls(args.text, limit=MAX_BATCH_URLS + 1)
    if not urls:
        return {"status": "rejected", "reason": "这段文字里没有找到 http(s) 网址"}
    if len(urls) > MAX_BATCH_URLS:
        return {
            "status": "rejected",
            "reason": f"单次最多收录 {MAX_BATCH_URLS} 个网址，请分批提交",
        }

    async with context.database.sessions() as session:
        items = await preview_batch(session, context.user_id, urls)

    ready = [item for item in items if item.status == "ready"]
    if not ready:
        return {
            "status": "noop",
            "message": "这些网址要么网址库里已经有了，要么无法识别，没有需要新增的。",
            "items": [
                {"url": item.url, "status": item.status, "reason": item.reason} for item in items
            ],
        }

    return {
        "status": "awaiting_confirmation",
        "message": "批量收录草稿已生成，等待用户在界面上确认后才会写入网址库。",
        "draft": {
            "kind": "site_batch",
            "urls": [item.url for item in ready],
            "total": len(items),
            "ready": len(ready),
            "duplicate": sum(1 for item in items if item.status == "duplicate"),
            "invalid": sum(1 for item in items if item.status == "invalid"),
            "items": [
                {"url": item.url, "status": item.status, "reason": item.reason} for item in items
            ],
        },
    }


async def _propose_reclassify(context: AgentToolContext, _: EmptyArgs) -> dict[str, Any]:
    """Return a draft for full-library LLM reclassification; zero model calls."""

    from webhub.library import reclassify

    async with context.database.sessions() as session:
        return await reclassify.propose_reclassification(session, context.user_id)


def build_tools(context: AgentToolContext) -> Sequence[Any]:
    """Build the LangChain tool list for one account-scoped turn."""

    from langchain_core.tools import StructuredTool

    def structured(
        name: str,
        description: str,
        args_schema: type[BaseModel],
        handler: Any,
        *,
        response_format: Literal["content", "content_and_artifact"] = "content",
    ) -> Any:
        async def run(**kwargs: Any) -> Any:
            return await handler(context, args_schema(**kwargs))

        return StructuredTool.from_function(
            coroutine=run,
            name=name,
            description=description,
            args_schema=args_schema,
            response_format=response_format,
            handle_validation_error=lambda _: json.dumps(
                {
                    "code": "invalid_tool_arguments",
                    "error": "工具参数不符合要求，请修正后重试。",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    tools = [
        structured(
            "search_library",
            "在【当前用户自己的网址库】里检索已收藏的网站。回答任何“我有没有/我收藏过”类问题前必须先调用它。"
            "用户明确要求全部结果时设 include_all=true；按分类检索必须先用 list_categories "
            "获取 category_id。",
            SearchLibraryArgs,
            _search_library,
        ),
        structured(
            "get_site_detail",
            "读取站内某个网站的完整信息，需要先从 search_library 拿到 site_id。",
            SiteIdArgs,
            _get_site_detail,
        ),
        structured(
            "list_categories",
            "列出当前用户已有的分类及其网站数量。给网站定分类前先看已有分类，避免制造重复分类。",
            EmptyArgs,
            _list_categories,
        ),
        structured(
            "list_tags",
            "列出当前用户已有的标签及其网站数量。打标签前必须先看已有标签；"
            "名称相同、同义或近义时必须直接复用已有标签，不能只因措辞不同而新建。",
            EmptyArgs,
            _list_tags,
        ),
        structured(
            "list_spaces",
            "列出当前用户创建的 space（可一键全部打开的网站集合）。",
            EmptyArgs,
            _list_spaces,
        ),
        structured(
            "present_website_recommendations",
            "把本轮最终推荐的具体网站转换成界面可点击的站内卡片。只要回答里会推荐一个或多个具体网站，"
            "就必须在最终回答前调用一次；普通推荐传 items，search_library(include_all=true) "
            "的完整结果必须原样传 result_set_id，不能把预览项改写成 items，也不要用 Markdown "
            "表格、列表或普通链接替代。"
            "服务端会自动识别已收藏网址并让卡片优先打开站内详情；未收藏网址保留收录和打开能力。",
            PresentWebsiteRecommendationsArgs,
            _present_website_recommendations,
            response_format="content_and_artifact",
        ),
        structured(
            "propose_site",
            "为一个待收藏的网址生成草稿卡片，交给用户确认。它不会写库，"
            "标签必须优先复用 list_tags 中语义相同或相近的已有名称。"
            "所以调用后必须告诉用户“请确认后保存”，不能声称已经保存成功。",
            ProposeSiteArgs,
            _propose_site,
        ),
        structured(
            "propose_sites",
            "一次收录**多个**网址。把用户原文原样传进来即可，服务端会自己解析出所有 URL——"
            "**不要自己逐个调用 propose_site**，那样无法保证每个网址都被处理。"
            "它不会写库，调用后必须说「请确认后保存」。",
            ProposeSitesArgs,
            _propose_sites,
        ),
        structured(
            "propose_site_update",
            "修改一个**已经收藏**的网站：改名、改说明、换分类、换标签、置顶或取消置顶。"
            "需要先用 search_library 拿到 site_id。只传要改的字段，不改的字段一律省略。"
            "换标签前先调用 list_tags，同义或近义标签必须复用已有名称。"
            "它同样不会写库，调用后必须说“请确认后生效”，不能声称已经改好。",
            ProposeSiteUpdateArgs,
            _propose_site_update,
        ),
        structured(
            "list_bookmark_imports",
            "列出当前账号最近的书签导入任务及其状态。用户提到「导入书签」时先看这里有没有已上传的任务。"
            "Agent 无法上传文件，文件必须由用户在界面上传。",
            EmptyArgs,
            _list_bookmark_imports,
        ),
        structured(
            "get_bookmark_import_preview",
            "读取一个书签导入任务的**汇总统计**：文件夹数、书签数、去重后候选数、重复数、"
            "各 proposed_action 的数量、以及按分类的分布。只返回聚合数字，不返回逐条书签——"
            "一份两千条的导出逐条读会白白烧掉几十万 token，而这十几个数字足够做判断。",
            BookmarkImportIdArgs,
            _get_bookmark_import_preview,
        ),
        structured(
            "propose_bookmark_import",
            "为一个已解析完成的书签导入任务生成确认草稿。它不会写库，"
            "调用后必须告诉用户「请确认后导入」，不能声称已经导入成功。",
            BookmarkImportIdArgs,
            _propose_bookmark_import,
        ),
        structured(
            "propose_space_batch",
            "创建一个 Space，或把一个/多个已收藏网站一次性加入已有 Space。"
            "调用前必须用 list_spaces 核对名称，并用 search_library 获取全部真实 site_id。"
            "site_ids 是这一张草稿的完整候选清单；用户要求剔除候选时，用剩余完整清单重新调用。"
            "纯创建空 Space 时 site_ids 传空数组。它不会写库，用户只需对整张任务确认一次。",
            ProposeSpaceBatchArgs,
            _propose_space_batch,
        ),
        structured(
            "propose_space_membership",
            "把一个已收藏的网站移出某个已有 Space。需要先用 search_library 拿到 site_id，"
            "用 list_spaces 确认 Space 名称。新增成员或新建 Space 必须改用 propose_space_batch。"
            "它不会写库，调用后必须说“请确认后生效”。",
            ProposeSpaceMembershipArgs,
            _propose_space_membership,
        ),
        structured(
            "propose_reclassify",
            "对当前网址库里所有网站发起 LLM 批量自动分类提案。它在提案阶段零模型消耗，"
            "会计算预估请求数与字符。未配置 Provider 时会被直接拒绝；"
            "调用后必须说“请确认后开始分类”。",
            EmptyArgs,
            _propose_reclassify,
        ),
    ]
    if context.search_binding is not None:
        tools.append(
            structured(
                "web_search",
                "只有在 search_library 没有命中、且需要站外最新信息时才联网搜索。",
                WebSearchArgs,
                _web_search,
            )
        )

    return tools


__all__ = [
    "SOURCE_LIBRARY",
    "SOURCE_MODEL",
    "SOURCE_WEB",
    "RECOMMENDATION_MANIFEST_VERSION",
    "AgentToolContext",
    "ProposeSiteArgs",
    "ProposeSitesArgs",
    "ProposeSiteUpdateArgs",
    "ProposeSpaceBatchArgs",
    "ProposeSpaceMembershipArgs",
    "PresentWebsiteRecommendationsArgs",
    "SearchLibraryArgs",
    "WebsiteRecommendation",
    "build_tools",
    "deterministic_collection_text",
    "propose_sites_from_text",
]
