from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from webhub.auth.dependencies import (
    CurrentIdentityDependency,
    DatabaseSessionDependency,
    require_trusted_origin,
)

from . import service
from .schemas import (
    ConversationCreateRequest,
    ConversationDeleteResponse,
    ConversationDetailResponse,
    ConversationHistoryResponse,
    ConversationRenameRequest,
    ConversationResponse,
    DraftConfirmationRequest,
    DraftConfirmationResponse,
    MessageAppendResponse,
    MessageListResponse,
    SlashCommandListResponse,
    SlashCommandResponse,
    UserMessageAppendRequest,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])
WriteOriginDependency = Annotated[None, Depends(require_trusted_origin)]


async def _call[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except service.ChatError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


@router.get("", response_model=ConversationHistoryResponse)
async def conversations(
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    cursor: Annotated[str | None, Query(max_length=2_048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    timezone_offset_minutes: Annotated[int, Query(ge=-840, le=840)] = 480,
) -> ConversationHistoryResponse:
    return await _call(
        service.list_conversations(
            session,
            identity.user.id,
            cursor=cursor,
            limit=limit,
            timezone_offset_minutes=timezone_offset_minutes,
        )
    )


@router.get("/commands", response_model=SlashCommandListResponse)
async def slash_commands(
    identity: CurrentIdentityDependency,
) -> SlashCommandListResponse:
    del identity
    return SlashCommandListResponse(
        items=[SlashCommandResponse(**item) for item in service.slash_command_definitions()]
    )


@router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def add_conversation(
    payload: ConversationCreateRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> ConversationResponse:
    return await _call(
        service.create_conversation(
            session,
            identity.user.id,
            title=payload.title,
        )
    )


@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
async def conversation(
    conversation_id: str,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    cursor: Annotated[str | None, Query(max_length=2_048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> ConversationDetailResponse:
    return await _call(
        service.get_conversation_detail(
            session,
            identity.user.id,
            conversation_id,
            cursor=cursor,
            limit=limit,
        )
    )


@router.patch("/{conversation_id}", response_model=ConversationResponse)
async def rename(
    conversation_id: str,
    payload: ConversationRenameRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> ConversationResponse:
    return await _call(
        service.rename_conversation(
            session,
            identity.user.id,
            conversation_id,
            title=payload.title,
            expected_version=payload.expected_version,
        )
    )


@router.delete("/{conversation_id}", response_model=ConversationDeleteResponse)
async def remove(
    conversation_id: str,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
    expected_version: Annotated[int, Query(ge=1)],
) -> ConversationDeleteResponse:
    return await _call(
        service.delete_conversation(
            session,
            identity.user.id,
            conversation_id,
            expected_version=expected_version,
        )
    )


@router.get("/{conversation_id}/messages", response_model=MessageListResponse)
async def messages(
    conversation_id: str,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    cursor: Annotated[str | None, Query(max_length=2_048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> MessageListResponse:
    return await _call(
        service.list_messages(
            session,
            identity.user.id,
            conversation_id,
            cursor=cursor,
            limit=limit,
        )
    )


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageAppendResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_message(
    conversation_id: str,
    payload: UserMessageAppendRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
    idempotency_header: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=200),
    ] = None,
) -> MessageAppendResponse:
    if (
        idempotency_header is not None
        and payload.idempotency_key is not None
        and idempotency_header.strip() != payload.idempotency_key.strip()
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "idempotency_key_mismatch",
                "message": "请求头与请求体中的幂等键不一致",
            },
        )
    result = await _call(
        service.append_message(
            session,
            identity.user.id,
            conversation_id,
            role="user",
            content=payload.content,
            idempotency_key=idempotency_header or payload.idempotency_key,
            expected_version=payload.expected_version,
        )
    )
    return MessageAppendResponse(
        conversation=result.conversation,
        message=result.message,
        replayed=result.replayed,
    )


@router.post(
    "/{conversation_id}/draft-confirmations",
    response_model=DraftConfirmationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def confirm_draft(
    conversation_id: str,
    payload: DraftConfirmationRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> DraftConfirmationResponse:
    """Record that the user confirmed one Agent draft.

    The browser performs the actual write through the ordinary library/spaces
    endpoints; this only tells the transcript it happened, so the next turn
    does not replay a history claiming the draft is still pending.
    """

    return await _call(
        service.record_draft_confirmation(
            session,
            identity.user.id,
            conversation_id,
            tool_call_id=payload.tool_call_id,
            kind=payload.kind,
            site_id=payload.site_id,
            space_id=payload.space_id,
        )
    )
