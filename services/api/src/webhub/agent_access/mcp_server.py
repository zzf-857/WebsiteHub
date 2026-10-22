"""The MCP endpoint external Agents use: ``/api/mcp`` (clients reach it through
the website's same-origin proxy at ``http://<host>:3100/api/backend/mcp``).

Wiring (verified against ``mcp==1.29.0``):

* One ``FastMCP`` per ``create_app()``: its session manager can run only once,
  and tests build many apps. ``main.py`` runs ``session_manager.run()`` for the
  app's lifetime.
* Stateless + JSON responses, so ``tools/list`` / ``tools/call`` need no prior
  ``initialize`` and nothing is buffered as an event stream.
* The route is registered at the exact path instead of ``app.mount``: a mount
  307-redirects ``/api/mcp`` to ``/api/mcp/`` with an internal Location header
  that clients behind the proxy cannot follow.
* ``McpBearerAuth`` guards the route: bearer token only, never cookies, no
  browser Origin check (an Agent is not a browser, and a browser cannot attach
  an ``Authorization`` header cross-origin without a CORS preflight we never
  grant).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Annotated, Any, Literal

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from webhub.agent_access import tools
from webhub.agent_access.tokens import AgentIdentity, resolve_token
from webhub.db.database import Database
from webhub.library import service

logger = logging.getLogger(__name__)

MCP_PATH = "/api/mcp"
READ_ONLY_TOKEN_MESSAGE = (
    "this token is read-only: it can search and read the library but cannot save or "
    "change anything. Ask the user to create a read-write token in WebHub "
    "(Settings → Agent access) if writing is needed."
)

CURRENT_AGENT: ContextVar[AgentIdentity | None] = ContextVar("webhub_mcp_agent", default=None)

INSTRUCTIONS = (
    "WebHub is the user's personal library of saved resources: GitHub repositories, "
    "articles and posts, videos, and websites/tools they bookmarked to come back to. "
    "When the user mentions something they saved, bookmarked, starred or 'saw "
    "somewhere before', search here with search_resources before searching the web, "
    "and prefer these saved items when recommending resources. Every item has an id "
    "for get_resource, update_resource and mark_resource. get_rediscover returns "
    "saved-but-never-opened items worth bringing back today, with a reason. Saving "
    "(save_resource) is cheap, deduplicated and reversible, so save useful links the "
    "user shares or asks you to keep; the summary and category (and any tags or "
    "description you did not give) are filled in automatically afterwards. Search is "
    "literal keyword matching where every keyword must match, so search with one to "
    "three distinctive words and retry with fewer or translated keywords before "
    "concluding something was never saved. Nothing can be deleted through this "
    "server. Texts are stored in their original language (often Chinese); quote them "
    "as they are."
)

_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
_IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

KindArgument = Annotated[
    Literal["repo", "article", "video", "website"] | None,
    Field(
        description="Resource type: repo (code repository or model/dataset page), "
        "article (post, blog, paper, issue, discussion), video, or website (tool, "
        "product or site home page)."
    ),
]
ResourceId = Annotated[
    str,
    Field(min_length=1, max_length=64, description="Resource id from search_resources."),
]
TagNames = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=40)]] | None,
    Field(max_length=8, description="Tag names (at most 8)."),
]

Operation = Callable[[AsyncSession, AgentIdentity], Awaitable[dict[str, Any]]]


def current_agent() -> AgentIdentity:
    identity = CURRENT_AGENT.get()
    if identity is None:  # pragma: no cover - the route guard always sets it
        raise ToolError("Not authenticated.")
    return identity


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_mcp_server(database: Database) -> FastMCP:
    mcp = FastMCP(
        "webhub",
        instructions=INSTRUCTIONS,
        # FastMCP calls logging.basicConfig; WARNING keeps that a no-op in
        # effect instead of turning on INFO logs for every library.
        log_level="WARNING",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        # The SDK's DNS-rebinding guard checks Host/Origin against a fixed
        # allowlist. It is redundant here and would reject the proxy's forwarded
        # Host: the API listens only on loopback behind the Next proxy, the
        # Next server enforces its own Host allowlist, and every request must
        # carry a valid bearer token (DNS rebinding cannot supply one).
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def run(tool: str, operation: Operation, *, write: bool) -> str:
        identity = current_agent()
        if write and not identity.can_write:
            raise ToolError(READ_ONLY_TOKEN_MESSAGE)
        try:
            async with database.sessions() as session:
                return _json(await operation(session, identity))
        except tools.ToolInputError as error:
            raise ToolError(str(error)) from None
        except service.LibraryNotFoundError as error:
            raise ToolError(
                f"Not found ({error.message}). Use search_resources to get a valid id."
            ) from None
        except service.LibraryError as error:
            raise ToolError(error.message) from None
        except Exception:
            # Internal details (SQL, paths) never reach the Agent.
            logger.exception("MCP tool %s failed", tool)
            raise ToolError("WebHub could not complete this request; try again later.") from None

    @mcp.tool(
        name="search_resources",
        description=(
            "Search the user's saved resources by keywords and/or filters. Use this first "
            "whenever the user refers to something they saved or asks for resources on a "
            "topic. Matching is literal, not semantic: every keyword must appear "
            "(case-insensitive) in the title, URL, description, category or tags, or in the "
            "saved page text / README snapshot, so pass "
            "one to three distinctive words such as a name, product or topic rather than a "
            "sentence. If nothing matches, retry with fewer or different keywords, or their "
            "Chinese/English equivalent (much of the library is in Chinese). An empty query "
            "lists the most recently saved items. Returns "
            "{total, items: [{id, title, url, kind, summary, category, tags, saved_at, "
            "opened, starred, matched_in, snippet?}]}, best matches first; with a query, "
            "matched_in is 'title' (matched the item's own fields) or 'content' (matched "
            "only its page text, with a short snippet), and title matches come first. "
            "Call get_resource for full details."
        ),
        annotations=_READ,
        structured_output=False,
    )
    async def search_resources(
        query: Annotated[
            str,
            Field(
                max_length=300,
                description="Space-separated keywords; every one must appear in the "
                "title, URL, description, category, tags or page text. Leave empty to browse.",
            ),
        ] = "",
        kind: KindArgument = None,
        state: Annotated[
            Literal["unopened", "opened", "starred"] | None,
            Field(
                description="unopened: never opened and not marked seen; opened: opened "
                "at least once; starred: marked as a treasure by the user."
            ),
        ] = None,
        tag: Annotated[
            str | None,
            Field(max_length=40, description="Only items with this tag (exact name)."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=30, description="Maximum items (1-30).")] = 10,
    ) -> str:
        return await run(
            "search_resources",
            lambda session, identity: tools.search_resources(
                session, identity, query=query, kind=kind, state=state, tag=tag, limit=limit
            ),
            write=False,
        )

    @mcp.tool(
        name="get_resource",
        description=(
            "Get everything stored about one saved resource: title, URL, kind, summary, "
            "long description, category, tags, kind-specific meta (e.g. repo owner/name), "
            "open_count, last_opened_at, seen, starred and analysis status. With "
            "include_content=true it also returns content: {text, chars, source "
            "(page|readme), extracted_at} — the saved page text or README excerpt (up to "
            "12,000 characters), or null when none was captured yet."
        ),
        annotations=_READ,
        structured_output=False,
    )
    async def get_resource(
        id: ResourceId,  # noqa: A002 - MCP argument name
        include_content: Annotated[
            bool,
            Field(
                description="Also return the saved page text / README snapshot. "
                "Long; request it only when the summary is not enough."
            ),
        ] = False,
    ) -> str:
        return await run(
            "get_resource",
            lambda session, identity: tools.get_resource(
                session, identity, resource_id=id, include_content=include_content
            ),
            write=False,
        )

    @mcp.tool(
        name="get_rediscover",
        description=(
            "Get today's rediscovery picks: resources saved at least 3 days ago that the "
            "user never opened, ranked by overlap with their recent interests. Each item "
            "has a short reason (in Chinese) explaining why it resurfaced. Use it for "
            "daily digests or when the user asks what they saved but forgot. Picks are "
            "stable for the whole UTC day."
        ),
        annotations=_READ,
        structured_output=False,
    )
    async def get_rediscover(
        limit: Annotated[int, Field(ge=1, le=12, description="Number of picks (1-12).")] = 4,
    ) -> str:
        return await run(
            "get_rediscover",
            lambda session, identity: tools.get_rediscover(session, identity, limit=limit),
            write=False,
        )

    @mcp.tool(
        name="list_taxonomy",
        description=(
            "List the user's categories and tags with item counts. Use it to pick exact "
            "names for search_resources(tag=...) and update_resource(category=..., "
            "add_tags=...), and to reuse existing tags instead of inventing near-duplicates."
        ),
        annotations=_READ,
        structured_output=False,
    )
    async def list_taxonomy() -> str:
        return await run("list_taxonomy", tools.list_taxonomy, write=False)

    @mcp.tool(
        name="save_resource",
        description=(
            "Save a URL to the user's library (requires a read-write token). Saving the "
            "same URL twice is safe: the existing item is returned with "
            "already_saved=true and left unchanged. Pass title whenever you know the "
            "page title; otherwise owner/repo (repositories) or the host name is used. "
            "Background analysis then fills in the summary and category, plus the "
            "description and tags unless you passed note or tags yourself. Tags are "
            "matched to existing tags by name (case-insensitive) or created. Returns "
            "{already_saved, resource}."
        ),
        annotations=_IDEMPOTENT_WRITE,
        structured_output=False,
    )
    async def save_resource(
        url: Annotated[
            str,
            Field(min_length=1, max_length=4096, description="Absolute http(s) URL."),
        ],
        title: Annotated[
            str | None,
            Field(
                max_length=1_000,
                description="Optional display title; longer titles are shortened to "
                "160 characters.",
            ),
        ] = None,
        tags: TagNames = None,
        note: Annotated[
            str | None,
            Field(
                max_length=4000,
                description="Optional note on why it is worth keeping; stored as the "
                "item's description.",
            ),
        ] = None,
    ) -> str:
        return await run(
            "save_resource",
            lambda session, identity: tools.save_resource(
                session, identity, database, url=url, title=title, tags=tags, note=note
            ),
            write=True,
        )

    @mcp.tool(
        name="update_resource",
        description=(
            "Change one saved resource (requires a read-write token): add or remove tags "
            "by name, move it to an existing category (exact name; see list_taxonomy), "
            "star or unstar it, or correct its kind. Only the given fields change. "
            "Returns {changed, resource}."
        ),
        annotations=_IDEMPOTENT_WRITE,
        structured_output=False,
    )
    async def update_resource(
        id: ResourceId,  # noqa: A002 - MCP argument name
        add_tags: TagNames = None,
        remove_tags: TagNames = None,
        category: Annotated[
            str | None,
            Field(max_length=80, description="Name of an existing category."),
        ] = None,
        starred: Annotated[
            bool | None, Field(description="true to star (mark as treasure), false to unstar.")
        ] = None,
        kind: KindArgument = None,
    ) -> str:
        return await run(
            "update_resource",
            lambda session, identity: tools.update_resource(
                session,
                identity,
                resource_id=id,
                add_tags=add_tags,
                remove_tags=remove_tags,
                category=category,
                starred=starred,
                kind=kind,
            ),
            write=True,
        )

    @mcp.tool(
        name="mark_resource",
        description=(
            "Record what the user did with a resource (requires a read-write token). "
            "opened: the user opened/read it now (counts an open); seen: they already "
            "know it, stop treating it as unread; snooze: bring it back in rediscovery "
            "after snooze_days; mute: never resurface it again. All are reversible on "
            "the website. Returns {action, resource}."
        ),
        annotations=_WRITE,
        structured_output=False,
    )
    async def mark_resource(
        id: ResourceId,  # noqa: A002 - MCP argument name
        action: Annotated[
            Literal["opened", "seen", "snooze", "mute"],
            Field(description="opened | seen | snooze | mute"),
        ],
        snooze_days: Annotated[
            int, Field(ge=1, le=90, description="Days to snooze (only for action=snooze).")
        ] = 7,
    ) -> str:
        return await run(
            "mark_resource",
            lambda session, identity: tools.mark_resource(
                session, identity, resource_id=id, action=action, snooze_days=snooze_days
            ),
            write=True,
        )

    return mcp


class McpBearerAuth:
    """ASGI guard: resolve ``Authorization: Bearer whk_…`` or answer JSON 401."""

    def __init__(self, app: ASGIApp, database: Database) -> None:
        self.app = app
        self.database = database

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        identity = await self._identity(Headers(scope=scope).get("authorization"))
        if identity is None:
            response = JSONResponse(
                {
                    "error": "unauthorized",
                    "message": "A valid WebHub Agent token is required: "
                    "Authorization: Bearer whk_…",
                },
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="webhub", error="invalid_token"'},
            )
            await response(scope, receive, send)
            return
        if scope.get("method") == "GET":
            # The spec lets a server without a standalone SSE stream answer GET
            # with 405. In stateless mode nothing could ever be pushed on it, and
            # the Next proxy holds such an idle stream without flushing headers.
            response = JSONResponse(
                {
                    "error": "method_not_allowed",
                    "message": "This server has no SSE stream; send JSON-RPC with POST.",
                },
                status_code=405,
                headers={"Allow": "POST, DELETE"},
            )
            await response(scope, receive, send)
            return
        marker = CURRENT_AGENT.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            CURRENT_AGENT.reset(marker)

    async def _identity(self, header: str | None) -> AgentIdentity | None:
        if not header:
            return None
        scheme, _, credential = header.strip().partition(" ")
        if scheme.casefold() != "bearer" or not credential.strip():
            return None
        async with self.database.sessions() as session:
            return await resolve_token(session, credential.strip())


def install_mcp(application: FastAPI, database: Database) -> FastMCP:
    """Build the MCP server and register its guarded route on ``application``."""

    mcp = build_mcp_server(database)
    # Creates the session manager; its route endpoint is the transport ASGI app.
    starlette_app = mcp.streamable_http_app()
    endpoint = next(route.endpoint for route in starlette_app.routes)  # type: ignore[attr-defined]
    application.router.routes.append(
        Route(
            MCP_PATH,
            endpoint=McpBearerAuth(endpoint, database),
            methods=["GET", "POST", "DELETE"],
        )
    )
    return mcp
