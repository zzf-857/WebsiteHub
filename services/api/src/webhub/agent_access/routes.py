"""Cookie-session REST behind /settings/agents (never reachable with an Agent token)."""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from webhub.agent_access import tokens
from webhub.agent_access.schemas import (
    AgentActivityItem,
    AgentActivityListResponse,
    AgentTokenCreatedResponse,
    AgentTokenCreateRequest,
    AgentTokenListResponse,
    AgentTokenResponse,
)
from webhub.auth.dependencies import (
    CurrentIdentityDependency,
    DatabaseSessionDependency,
    require_trusted_origin,
)
from webhub.db.models import AgentToken

router = APIRouter(tags=["agent-access"])
WriteOriginDependency = Annotated[None, Depends(require_trusted_origin)]


async def _call[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except tokens.AgentAccessError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


def _utc(value: datetime) -> datetime:
    # SQLite hands back naive UTC values; answer with an explicit offset so
    # the browser never reads them as local time.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _token_response(token: AgentToken) -> AgentTokenResponse:
    return AgentTokenResponse(
        id=token.id,
        name=token.name,
        prefix=token.token_prefix,
        scopes=tokens.scope_list(token.scopes),  # type: ignore[arg-type]
        created_at=_utc(token.created_at),
        last_used_at=_utc(token.last_used_at) if token.last_used_at else None,
    )


@router.get("/agent-tokens", response_model=AgentTokenListResponse)
async def list_agent_tokens(
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
) -> AgentTokenListResponse:
    items = await tokens.list_tokens(session, identity.user.id)
    return AgentTokenListResponse(
        items=[_token_response(token) for token in items],
        max_active=tokens.MAX_ACTIVE_TOKENS,
    )


@router.post(
    "/agent-tokens",
    response_model=AgentTokenCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_token(
    payload: AgentTokenCreateRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> AgentTokenCreatedResponse:
    token, plaintext = await _call(
        tokens.issue_token(
            session,
            identity.user.id,
            name=payload.name,
            write=tokens.WRITE_SCOPE in payload.scopes,
        )
    )
    return AgentTokenCreatedResponse(
        **_token_response(token).model_dump(),
        token=plaintext,
    )


@router.delete("/agent-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_agent_token(
    token_id: str,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> Response:
    await _call(tokens.revoke_token(session, identity.user.id, token_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/agent-activity", response_model=AgentActivityListResponse)
async def list_agent_activity(
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    limit: Annotated[int, Query(ge=1, le=tokens.MAX_ACTIVITY_LIMIT)] = 50,
) -> AgentActivityListResponse:
    entries = await tokens.list_activity(session, identity.user.id, limit=limit)
    return AgentActivityListResponse(
        items=[
            AgentActivityItem(
                id=item.entry.id,
                created_at=_utc(item.entry.created_at),
                token_id=item.entry.token_id,
                token_name=item.token_name,
                tool=item.entry.tool,
                site_id=item.entry.site_id,
                summary=item.entry.summary,
            )
            for item in entries
        ]
    )
