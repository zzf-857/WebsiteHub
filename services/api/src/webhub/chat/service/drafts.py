from __future__ import annotations

import hashlib
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import Space, SpaceBatchOperationReceipt
from webhub.library import service as library_service
from webhub.library.service import LibraryError
from webhub.space_batch_state import normalize_space_batch_state_artifact
from webhub.spaces.schemas import SpaceMemberBatchResponse
from webhub.spaces.service import space_batch_space_id

from ..commands import default_slash_command_registry
from ..models import ConversationMessage
from ..schemas import (
    DraftConfirmationKind,
    DraftConfirmationResponse,
)
from ._common import (
    ChatConflictError,
    ChatError,
    ChatNotFoundError,
    ChatValidationError,
    _idempotency_hash,
    _json_value,
    _owned_conversation,
)
from .messages import (
    _existing_idempotent_message,
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


async def _latest_space_batch_state(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
) -> dict[str, object] | None:
    statement = (
        select(ConversationMessage.artifacts_json)
        .where(
            ConversationMessage.user_id == user_id,
            ConversationMessage.conversation_id == conversation_id,
            ConversationMessage.role == "assistant",
            ConversationMessage.status == "complete",
            ConversationMessage.artifacts_json != "[]",
        )
        .order_by(
            ConversationMessage.sequence.desc(),
            ConversationMessage.id.desc(),
        )
    )
    encoded_rows = await session.scalars(statement)
    for encoded in encoded_rows:
        artifacts = _json_value(encoded, field="artifacts", expected=list)
        assert isinstance(artifacts, list)
        for artifact in reversed(artifacts):
            state = normalize_space_batch_state_artifact(artifact)
            if state is not None:
                return state
    return None


async def _validate_space_batch_confirmation(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
    *,
    tool_call_id: str,
    space_id: str,
    site_ids: list[str],
) -> None:
    state = await _latest_space_batch_state(session, user_id, conversation_id)
    if (
        state is None
        or state.get("status") != "awaiting_confirmation"
        or state.get("toolCallId") != tool_call_id
    ):
        raise ChatConflictError(
            "该 Space 草稿不是当前待确认任务，请使用最新草稿",
            code="draft_superseded",
        )

    draft = state.get("draft")
    assert isinstance(draft, dict)
    target = draft["target"]
    candidates = {
        item["site_id"]
        for item in draft["sites"]
        if isinstance(item, dict) and isinstance(item.get("site_id"), str)
    }
    selected = set(site_ids)
    if not selected.issubset(candidates):
        raise ChatValidationError("确认的网站不属于该 Space 草稿候选")

    mode = target["mode"]
    if mode == "existing":
        if not site_ids:
            raise ChatValidationError("已有 Space 的批量确认至少需要一个网站")
        if space_id != target["space_id"]:
            raise ChatValidationError("确认的 Space 与草稿目标不一致")
    else:
        if space_id != space_batch_space_id(user_id, tool_call_id):
            raise ChatValidationError("确认的 Space 不属于该创建任务")


async def _space_batch_receipt_result(
    session: AsyncSession,
    user_id: str,
    *,
    tool_call_id: str,
    space_id: str,
    site_ids: list[str],
) -> SpaceMemberBatchResponse:
    receipt = await session.scalar(
        select(SpaceBatchOperationReceipt).where(
            SpaceBatchOperationReceipt.user_id == user_id,
            SpaceBatchOperationReceipt.operation_id == tool_call_id,
        )
    )
    if receipt is None:
        raise ChatConflictError(
            "Space 批量任务尚未完整写入，不能记录确认",
            code="draft_not_applied",
        )
    try:
        selected_site_ids = _json_value(
            receipt.selected_site_ids_json,
            field="Space 批量回执网站",
            expected=list,
        )
        result = SpaceMemberBatchResponse.model_validate_json(receipt.result_json)
    except (ChatError, TypeError, ValueError) as error:
        raise ChatConflictError(
            "Space 批量操作回执损坏",
            code="operation_receipt_invalid",
        ) from error
    if (
        selected_site_ids != site_ids
        or result.site_ids != site_ids
        or receipt.target_mode not in {"create", "existing"}
        or receipt.target_space_id != space_id
        or receipt.result_space_id != space_id
        or result.space.id != space_id
        or receipt.added_count != result.added_count
        or receipt.already_member_count != result.already_member_count
        or result.added_count < 0
        or result.already_member_count < 0
        or result.added_count + result.already_member_count != len(site_ids)
        or result.space.member_count < len(site_ids)
        or result.space.version < 1
    ):
        raise ChatConflictError(
            "确认内容与 Space 批量操作回执不一致",
            code="draft_confirmation_mismatch",
        )
    return result


def _draft_confirmation_idempotency_key(tool_call_id: str) -> str:
    idempotency_key = f"draft-confirmation:{tool_call_id}"
    if len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        digest = hashlib.sha256(tool_call_id.encode("utf-8")).hexdigest()
        return f"draft-confirmation-sha256:{digest}"
    return idempotency_key


def _confirmation_metadata(
    *,
    tool_call_id: str,
    kind: DraftConfirmationKind,
    site_id: str | None,
    space_id: str | None,
    site_ids: list[str] | None,
) -> dict[str, object]:
    return {
        "toolCallId": tool_call_id,
        "kind": kind,
        "siteId": site_id,
        "spaceId": space_id,
        "siteIds": site_ids,
    }


async def _existing_draft_confirmation(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
    *,
    idempotency_key: str,
    confirmation_metadata: dict[str, object],
) -> ConversationMessage | None:
    existing = await _existing_idempotent_message(
        session,
        user_id=user_id,
        conversation_id=conversation_id,
        idempotency_key_hash=_idempotency_hash(idempotency_key),
    )
    if existing is None:
        return None
    metadata = _json_value(existing.metadata_json, field="metadata", expected=dict)
    assert isinstance(metadata, dict)
    marker = metadata.get("draftConfirmation")
    if not isinstance(marker, dict) or any(
        marker.get(field) != value for field, value in confirmation_metadata.items()
    ):
        raise ChatConflictError(
            "该草稿确认标识已用于其他确认内容",
            code="idempotency_conflict",
        )
    return existing


async def record_draft_confirmation(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
    *,
    tool_call_id: str,
    kind: DraftConfirmationKind,
    site_id: str | None,
    space_id: str | None,
    site_ids: list[str] | None = None,
) -> DraftConfirmationResponse:
    """Write the outcome of a confirmed Agent draft back into the transcript.

    Why this exists: ``_history_messages`` replays only message *content*, so a
    tool result frozen at ``awaiting_confirmation`` is what every later turn
    sees.  Without this the Agent tells the user their site "was never saved"
    moments after saving it.

    The sentence is composed here rather than accepted from the client. Single
    resource confirmations are also resolved through account-scoped rows;
    operation-wide confirmations can only select one of the bounded kinds.

    Idempotent on ``tool_call_id``: confirming twice (double click, retry after
    a flaky response) records one note, not two.
    """

    await _owned_conversation(session, user_id, conversation_id)
    if kind == "space_batch_applied" and site_ids is None:
        raise ChatValidationError("Space 批量确认必须提供 site_ids")
    confirmation_metadata = _confirmation_metadata(
        tool_call_id=tool_call_id,
        kind=kind,
        site_id=site_id,
        space_id=space_id,
        site_ids=site_ids,
    )
    idempotency_key = _draft_confirmation_idempotency_key(tool_call_id)
    existing_confirmation = await _existing_draft_confirmation(
        session,
        user_id,
        conversation_id,
        idempotency_key=idempotency_key,
        confirmation_metadata=confirmation_metadata,
    )
    if existing_confirmation is not None:
        return DraftConfirmationResponse(
            message_id=existing_confirmation.id,
            conversation_id=conversation_id,
            recorded=False,
            content=existing_confirmation.content,
        )

    site_kinds = {
        "site_created",
        "site_updated",
        "space_member_added",
        "space_member_removed",
    }
    site = None
    if kind in site_kinds:
        if site_id is None:
            raise ChatValidationError("该确认类型必须提供 site_id")
        try:
            site = await library_service.get_site(session, user_id, site_id)
        except LibraryError as error:
            raise ChatNotFoundError("网站不存在或不属于当前账号") from error
    elif site_id is not None:
        raise ChatValidationError("该确认类型不接受 site_id")

    space: Space | None = None
    space_name: str | None = None
    if kind == "space_batch_applied":
        if space_id is None:
            raise ChatValidationError("Space 变更必须提供 space_id")
        assert site_ids is not None
        batch_result = await _space_batch_receipt_result(
            session,
            user_id,
            tool_call_id=tool_call_id,
            space_id=space_id,
            site_ids=site_ids,
        )
        space_name = batch_result.space.name
    elif space_id is not None:
        space = await session.scalar(
            select(Space).where(Space.user_id == user_id, Space.id == space_id)
        )
        if space is None:
            raise ChatNotFoundError("Space 不存在或不属于当前账号")
        space_name = space.name

    if kind in {"space_member_added", "space_member_removed", "space_batch_applied"}:
        if space_name is None:
            raise ChatValidationError("Space 变更必须提供 space_id")
    elif space_id is not None:
        raise ChatValidationError("该确认类型不接受 space_id")

    if kind == "space_batch_applied":
        assert space_id is not None
        assert site_ids is not None
        await _validate_space_batch_confirmation(
            session,
            user_id,
            conversation_id,
            tool_call_id=tool_call_id,
            space_id=space_id,
            site_ids=site_ids,
        )
    elif site_ids is not None:
        raise ChatValidationError("该确认类型不接受 site_ids")

    if kind == "site_created":
        assert site is not None
        content = (
            f"[系统记录] 用户已确认草稿，网站「{site.name}」（{site.original_url}）"
            f"已写入网址库，site_id={site.id}，分类「{site.category.name}」。"
        )
    elif kind == "site_updated":
        assert site is not None
        tags = "、".join(tag.name for tag in site.tags) or "无"
        content = (
            f"[系统记录] 用户已确认修改，网站「{site.name}」（site_id={site.id}）现状："
            f"分类「{site.category.name}」，标签 {tags}，"
            f"{'已置顶' if site.pinned else '未置顶'}。"
        )
    elif kind == "space_member_added":
        assert site is not None
        content = (
            f"[系统记录] 用户已确认，网站「{site.name}」（site_id={site.id}）"
            f"已加入 Space「{space_name}」。"
        )
    elif kind == "space_member_removed":
        assert site is not None
        content = (
            f"[系统记录] 用户已确认，网站「{site.name}」（site_id={site.id}）"
            f"已移出 Space「{space_name}」。"
        )
    elif kind == "space_batch_applied":
        assert site_ids is not None
        content = (
            f"[系统记录] 用户已确认创建空 Space「{space_name}」。"
            if not site_ids
            else (
                f"[系统记录] 用户已确认 Space 批量任务，{len(site_ids)} 个选中网站"
                f"已整体处理到 Space「{space_name}」。"
            )
        )
    elif kind == "site_batch_created":
        content = "[系统记录] 用户已确认批量收录草稿，批量收录请求已执行。"
    elif kind == "reclassify_applied":
        content = "[系统记录] 用户已确认并执行全库重分类草稿。"
    else:
        raise ChatValidationError("不支持的草稿确认类型")

    result = await append_message(
        session,
        user_id,
        conversation_id,
        role="system",
        content=content,
        metadata={
            "draftConfirmation": confirmation_metadata,
        },
        # One note per confirmed draft, no matter how many times it is clicked.
        idempotency_key=idempotency_key,
    )
    return DraftConfirmationResponse(
        message_id=result.message.id,
        conversation_id=conversation_id,
        recorded=not result.replayed,
        content=content,
    )
