from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import MAX_CONVERSATION_TITLE_LENGTH, MAX_MESSAGE_CONTENT_LENGTH

MessageRole = Literal["system", "user", "assistant", "tool"]
MessageStatus = Literal["streaming", "complete", "error", "aborted"]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationCreateRequest(StrictRequest):
    title: str | None = Field(default=None, min_length=1, max_length=MAX_CONVERSATION_TITLE_LENGTH)


class ConversationRenameRequest(StrictRequest):
    expected_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=MAX_CONVERSATION_TITLE_LENGTH)


class ConversationResponse(BaseModel):
    id: str
    title: str
    title_is_custom: bool
    version: int
    message_count: int
    last_message_at: datetime
    created_at: datetime
    updated_at: datetime


class ConversationDeleteResponse(BaseModel):
    message: str
    conversation_id: str


class UserMessageAppendRequest(StrictRequest):
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CONTENT_LENGTH)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    expected_version: int | None = Field(default=None, ge=1)


DraftConfirmationKind = Literal[
    "site_created",
    "site_updated",
    "site_batch_created",
    "space_member_added",
    "space_member_removed",
    "space_batch_applied",
    "reclassify_applied",
]


class DraftConfirmationRequest(StrictRequest):
    """Record that the user confirmed one Agent draft.

    Deliberately carries only identifiers and an operation kind, never prose.
    The sentence written into the transcript is composed server-side.
    """

    tool_call_id: str = Field(min_length=1, max_length=200)
    kind: DraftConfirmationKind
    site_id: str | None = Field(default=None, min_length=1, max_length=36)
    space_id: str | None = Field(default=None, min_length=1, max_length=36)
    site_ids: list[str] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_batch_site_ids(self) -> Self:
        if self.kind == "space_batch_applied":
            if self.site_ids is None:
                raise ValueError("Space 批量确认必须提供 site_ids")
            if any(not site_id.strip() or len(site_id) > 36 for site_id in self.site_ids):
                raise ValueError("网站 ID 无效")
            if len(set(self.site_ids)) != len(self.site_ids):
                raise ValueError("确认的网站不能重复")
        elif self.site_ids is not None:
            raise ValueError("该确认类型不接受 site_ids")
        return self


class DraftConfirmationResponse(BaseModel):
    message_id: str
    conversation_id: str
    recorded: bool
    content: str


class ConversationMessageResponse(BaseModel):
    id: str
    conversation_id: str
    sequence: int
    role: MessageRole
    content: str
    parts: list[Any]
    sources: list[Any]
    artifacts: list[Any]
    metadata: dict[str, Any]
    status: MessageStatus
    version: int
    created_at: datetime
    updated_at: datetime


class MessageAppendResponse(BaseModel):
    conversation: ConversationResponse
    message: ConversationMessageResponse
    replayed: bool = False


class MessageListResponse(BaseModel):
    items: list[ConversationMessageResponse]
    next_cursor: str | None


class ConversationDetailResponse(BaseModel):
    conversation: ConversationResponse
    messages: list[ConversationMessageResponse]
    next_cursor: str | None


HistoryGroupKey = Literal["today", "yesterday", "last_7_days", "last_30_days"]


class HistoryGroupResponse(BaseModel):
    key: str
    label: str
    month: str | None = None
    items: list[ConversationResponse]


class ConversationHistoryResponse(BaseModel):
    groups: list[HistoryGroupResponse]
    next_cursor: str | None
    total_count: int


class SlashCommandResponse(BaseModel):
    name: str
    aliases: list[str]
    description: str
    usage: str
    argument_hint: str | None


class SlashCommandListResponse(BaseModel):
    items: list[SlashCommandResponse]
