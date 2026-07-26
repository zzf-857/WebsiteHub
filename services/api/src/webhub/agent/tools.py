"""Account-scoped tools the Agent may call during one turn.

Two rules govern everything in this module.

**Scope is bound by the server, never by the model.**  ``user_id`` is captured
in a closure from the authenticated session; no tool takes an account
parameter, so no prompt injection can widen the blast radius.  Every query
goes through the existing library/spaces services, which already enforce
per-account ownership.

**Reads are free, writes are proposals.**  The Agent may look at anything the
account owns, but it cannot create or modify a Site on its own: ``propose_site``
returns a draft that the browser must confirm.  That keeps the
human-in-the-loop confirmation from Implementation Plan §5.4 honest even if the
model is talked into "just saving it".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from webhub.config import Settings
from webhub.db.database import Database
from webhub.library import service as library_service
from webhub.library.service import LibraryError
from webhub.spaces import service as spaces_service
from webhub.spaces.service import SpaceError

from .provider_binding import ProviderBinding
from .web_search import WebSearchUnavailableError, search_web

# Provenance markers required by the todolist: the user must always be able to
# tell a stored bookmark from something the model produced.
SOURCE_LIBRARY = "站内存储数据"
SOURCE_WEB = "联网搜索"
SOURCE_MODEL = "llm推荐"

MAX_TOOL_LIMIT = 20


class SearchLibraryArgs(BaseModel):
    query: str = Field(
        default="",
        max_length=200,
        description="关键字，可留空表示不过滤。支持中文与英文分词。",
    )
    limit: int = Field(default=8, ge=1, le=MAX_TOOL_LIMIT, description="返回条数上限")
    pinned_only: bool = Field(default=False, description="只看星标（常用）网站")


class SiteIdArgs(BaseModel):
    site_id: str = Field(min_length=1, max_length=36, description="站内网站 ID")


class EmptyArgs(BaseModel):
    pass


class WebSearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=200, description="联网搜索关键字")
    limit: int = Field(default=5, ge=1, le=8, description="返回条数上限")


class ProposeSiteArgs(BaseModel):
    url: str = Field(min_length=1, max_length=2_000, description="网站地址，必须是 http/https")
    name: str = Field(min_length=1, max_length=160, description="网站名称")
    description: str = Field(default="", max_length=1_000, description="一句话说明网站做什么")
    category: str = Field(default="", max_length=80, description="分类，每个网站只能有一个")
    tags: list[str] = Field(default_factory=list, max_length=12, description="细粒度标签")


def _site_summary(site: Any) -> dict[str, Any]:
    return {
        "site_id": site.id,
        "name": site.name,
        "url": site.original_url,
        "description": site.description,
        "category": site.category.name,
        "tags": [tag.name for tag in site.tags],
        "pinned": site.pinned,
    }


@dataclass(frozen=True, slots=True)
class AgentToolContext:
    """Everything a tool needs, with the account scope already fixed."""

    database: Database
    settings: Settings
    user_id: str
    search_binding: ProviderBinding | None = None


async def _search_library(context: AgentToolContext, args: SearchLibraryArgs) -> dict[str, Any]:
    async with context.database.sessions() as session:
        try:
            listing = await library_service.list_sites(
                session,
                context.user_id,
                q=args.query.strip() or None,
                category_id=None,
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
            "favicon_url": site.favicon_url,
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
            "error": WebSearchUnavailableError.safe_message,
            "items": [],
        }
    return {"source": SOURCE_WEB, "items": [result.as_dict() for result in results]}


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

    tags = [tag.strip() for tag in args.tags if tag.strip()][:12]
    return {
        "status": "awaiting_confirmation",
        "message": "草稿已生成，等待用户在界面上确认后才会写入资料库。",
        "duplicate": duplicate,
        "draft": {
            "url": url,
            "name": args.name.strip(),
            "description": args.description.strip(),
            "category": args.category.strip(),
            "tags": tags,
        },
    }


def build_tools(context: AgentToolContext) -> Sequence[Any]:
    """Build the LangChain tool list for one account-scoped turn."""

    from langchain_core.tools import StructuredTool

    def structured(
        name: str,
        description: str,
        args_schema: type[BaseModel],
        handler: Any,
    ) -> Any:
        async def run(**kwargs: Any) -> Any:
            return await handler(context, args_schema(**kwargs))

        return StructuredTool.from_function(
            coroutine=run,
            name=name,
            description=description,
            args_schema=args_schema,
        )

    tools = [
        structured(
            "search_library",
            "在【当前用户自己的资料库】里检索已收藏的网站。回答任何“我有没有/我收藏过”类问题前必须先调用它。",
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
            "列出当前用户已有的标签及其网站数量。打标签前先看已有标签，优先复用。",
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
            "propose_site",
            "为一个待收藏的网址生成草稿卡片，交给用户确认。它不会写库，"
            "所以调用后必须告诉用户“请确认后保存”，不能声称已经保存成功。",
            ProposeSiteArgs,
            _propose_site,
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
    "AgentToolContext",
    "ProposeSiteArgs",
    "SearchLibraryArgs",
    "build_tools",
]
