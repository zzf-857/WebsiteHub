from __future__ import annotations

from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import Space
from webhub.library import service as library_service
from webhub.library.service import LibraryError

from ..commands import default_slash_command_registry
from ..schemas import (
    DraftConfirmationKind,
    DraftConfirmationResponse,
)
from ._common import (
    ChatNotFoundError,
    ChatValidationError,
    _owned_conversation,
)
from .messages import (
    append_message,
)

SortDirection = Literal["asc", "desc"]
MessageRole = Literal["system", "user", "assistant", "tool"]
MessageStatus = Literal["streaming", "complete", "error", "aborted"]

MAX_HISTORY_LIMIT = 100
MAX_MESSAGE_LIMIT = 100
MAX_IDEMPOTENCY_KEY_LENGTH = 200
_MAX_CAS_RETRIES = 3


def slash_command_definitions() -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "name": definition.name,
            "aliases": list(definition.aliases),
            "description": definition.description,
            "usage": definition.usage,
            "argument_hint": definition.argument_hint,
        }
        for definition in default_slash_command_registry().list()
    )


async def record_draft_confirmation(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
    *,
    tool_call_id: str,
    kind: DraftConfirmationKind,
    site_id: str,
    space_id: str | None,
) -> DraftConfirmationResponse:
    """Write the outcome of a confirmed Agent draft back into the transcript.

    Why this exists: ``_history_messages`` replays only message *content*, so a
    tool result frozen at ``awaiting_confirmation`` is what every later turn
    sees.  Without this the Agent tells the user their site "was never saved"
    moments after saving it.

    The sentence is composed here from the rows themselves rather than accepted
    from the client.  A browser can say "this draft was confirmed"; it cannot
    dictate what the transcript claims is in the library.

    Idempotent on ``tool_call_id``: confirming twice (double click, retry after
    a flaky response) records one note, not two.
    """

    await _owned_conversation(session, user_id, conversation_id)

    try:
        site = await library_service.get_site(session, user_id, site_id)
    except LibraryError as error:
        raise ChatNotFoundError("网站不存在或不属于当前账号") from error

    space_name: str | None = None
    if space_id is not None:
        space = await session.scalar(
            select(Space).where(Space.user_id == user_id, Space.id == space_id)
        )
        if space is None:
            raise ChatNotFoundError("Space 不存在或不属于当前账号")
        space_name = space.name

    if kind in {"space_member_added", "space_member_removed"} and space_name is None:
        raise ChatValidationError("Space 变更必须提供 space_id")

    if kind == "site_created":
        content = (
            f"[系统记录] 用户已确认草稿，网站「{site.name}」（{site.original_url}）"
            f"已写入资料库，site_id={site.id}，分类「{site.category.name}」。"
        )
    elif kind == "site_updated":
        tags = "、".join(tag.name for tag in site.tags) or "无"
        content = (
            f"[系统记录] 用户已确认修改，网站「{site.name}」（site_id={site.id}）现状："
            f"分类「{site.category.name}」，标签 {tags}，"
            f"{'已置顶' if site.pinned else '未置顶'}。"
        )
    elif kind == "space_member_added":
        content = (
            f"[系统记录] 用户已确认，网站「{site.name}」（site_id={site.id}）"
            f"已加入 Space「{space_name}」。"
        )
    else:
        content = (
            f"[系统记录] 用户已确认，网站「{site.name}」（site_id={site.id}）"
            f"已移出 Space「{space_name}」。"
        )

    result = await append_message(
        session,
        user_id,
        conversation_id,
        role="system",
        content=content,
        # One note per confirmed draft, no matter how many times it is clicked.
        idempotency_key=f"draft-confirmation:{tool_call_id}",
    )
    return DraftConfirmationResponse(
        message_id=result.message.id,
        conversation_id=conversation_id,
        recorded=not result.replayed,
        content=content,
    )
