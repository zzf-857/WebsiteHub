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

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from webhub.bookmarks import persistence
from webhub.bookmarks import queries as bookmark_queries
from webhub.bookmarks.apply import category_distribution
from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.models import BookmarkImportJob
from webhub.library import service as library_service
from webhub.library.batch import extract_urls, preview_batch
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
        description="新的标签集合，会整体替换原有标签；不改就不要传",
    )
    pinned: bool | None = Field(default=None, description="是否置顶（星标），不改就不要传")


class ProposeSpaceMembershipArgs(BaseModel):
    site_id: str = Field(min_length=1, max_length=36, description="站内网站 ID")
    space: str = Field(min_length=1, max_length=120, description="Space 的名称或 ID")
    action: Literal["add", "remove"] = Field(description="add=移入该 Space，remove=移出该 Space")


def _site_summary(site: Any) -> dict[str, Any]:
    return {
        "site_id": site.id,
        "name": site.name,
        "url": site.original_url,
        "favicon_url": site.favicon_url,
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


def _normalized_tags(values: list[str]) -> list[str]:
    """Trim, drop blanks, de-duplicate case-insensitively, keep first spelling."""

    seen: dict[str, str] = {}
    for value in values:
        tag = " ".join(value.split())
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
    """Return a draft for moving a site into or out of a Space; never writes."""

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

    if (args.action == "add") == member:
        return {
            "status": "noop",
            "message": (
                f"“{site.name}”已经在 Space“{space.name}”里了。"
                if member
                else f"“{site.name}”本来就不在 Space“{space.name}”里。"
            ),
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
        "message": "导入草稿已生成，等待用户在界面上确认后才会写入资料库。",
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
        max_length=20_000,
        description="包含一个或多个网址的原文，原样传入即可——服务端会自己把 URL 全部解析出来",
    )


async def _propose_sites(context: AgentToolContext, args: ProposeSitesArgs) -> dict[str, Any]:
    """Batch draft.  Extraction happens in code, not by the model looping.

    The model passes the user's text through unchanged; ``extract_urls`` finds
    every address.  That is the point: a loop the model can forget to run is
    not a loop, and "did it handle all ten?" stops being a question.
    """

    urls = extract_urls(args.text)
    if not urls:
        return {"status": "rejected", "reason": "这段文字里没有找到 http(s) 网址"}

    async with context.database.sessions() as session:
        items = await preview_batch(session, context.user_id, urls)

    ready = [item for item in items if item.status == "ready"]
    if not ready:
        return {
            "status": "noop",
            "message": "这些网址要么资料库里已经有了，要么无法识别，没有需要新增的。",
            "items": [
                {"url": item.url, "status": item.status, "reason": item.reason} for item in items
            ],
        }

    return {
        "status": "awaiting_confirmation",
        "message": "批量收录草稿已生成，等待用户在界面上确认后才会写入资料库。",
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
            "propose_space_membership",
            "把一个已收藏的网站移入或移出某个 Space。需要先用 search_library 拿到 site_id，"
            "用 list_spaces 确认 Space 名称。它不会写库，也不会新建 Space，"
            "调用后必须说“请确认后生效”。",
            ProposeSpaceMembershipArgs,
            _propose_space_membership,
        ),
        structured(
            "propose_reclassify",
            "对当前资料库里所有网站发起 LLM 批量自动分类提案。它在提案阶段零模型消耗，"
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
    "AgentToolContext",
    "ProposeSiteArgs",
    "ProposeSitesArgs",
    "ProposeSiteUpdateArgs",
    "ProposeSpaceMembershipArgs",
    "SearchLibraryArgs",
    "build_tools",
]
