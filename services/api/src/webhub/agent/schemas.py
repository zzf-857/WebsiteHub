"""HTTP request models for the first agent chat slice."""

from __future__ import annotations

import json
from typing import Annotated, Any, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

MAX_AGENT_METADATA_BYTES = 16 * 1024
MAX_SLASH_COMMAND_METADATA_BYTES = 64 * 1024
SlashCommandArgument = Annotated[str, Field(max_length=4_096)]
_RESERVED_ACCOUNT_METADATA_KEYS = {
    "accountid",
    "userid",
    "ownerid",
}


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SlashCommandMetadataInput(StrictRequest):
    """Optional client-provided command metadata.

    The server reparses the message and verifies this metadata.  It is never
    trusted as an instruction by itself, which keeps future clients from
    bypassing the command registry.
    """

    name: str | None = Field(default=None, min_length=2, max_length=64)
    argument_text: str | None = Field(
        default=None,
        max_length=32_000,
        validation_alias=AliasChoices("argument_text", "argumentText"),
    )
    arguments: tuple[SlashCommandArgument, ...] | None = Field(
        default=None,
        max_length=256,
    )
    known: bool | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value.startswith("/") or any(character.isspace() for character in value):
            raise ValueError("slash command name must start with '/' and contain no whitespace")
        return value

    @model_validator(mode="after")
    def validate_encoded_size(self) -> Self:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_SLASH_COMMAND_METADATA_BYTES:
            raise ValueError(
                "slash command metadata must not exceed "
                f"{MAX_SLASH_COMMAND_METADATA_BYTES} encoded bytes"
            )
        return self


class AgentChatRequest(StrictRequest):
    """Minimal request understood by the agent stream route.

    ``conversation_id`` is deliberately opaque to this layer.  Ownership is
    checked through the injected :class:`AgentConversationAccess` contract.
    ``metadata`` is client context only; the account id is always derived from
    the authenticated identity and cannot be supplied by the caller.
    """

    conversation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("conversation_id", "conversationId"),
    )
    message: str = Field(min_length=1, max_length=64_000)
    slash_command: SlashCommandMetadataInput | None = Field(
        default=None,
        validation_alias=AliasChoices("slash_command", "slashCommand"),
    )
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=32)

    @field_validator("conversation_id")
    @classmethod
    def normalize_conversation_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("conversation_id must not be blank")
        return normalized

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message must not be blank")
        return normalized

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        # Metadata is an untrusted client hint object, never an authorization
        # source.  Bound both cardinality and encoded size before it can reach
        # a runner or become part of a prompt.
        for key in value:
            if not isinstance(key, str) or not key.strip():
                raise ValueError("metadata keys must be non-empty strings")
            normalized_key = key.replace("_", "").replace("-", "").casefold()
            if normalized_key in _RESERVED_ACCOUNT_METADATA_KEYS:
                raise ValueError("metadata must not contain account identity fields")
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError, RecursionError) as error:
            raise ValueError("metadata must be JSON serializable") from error
        if len(encoded) > MAX_AGENT_METADATA_BYTES:
            raise ValueError(f"metadata must not exceed {MAX_AGENT_METADATA_BYTES} encoded bytes")
        return value


__all__ = [
    "MAX_AGENT_METADATA_BYTES",
    "MAX_SLASH_COMMAND_METADATA_BYTES",
    "AgentChatRequest",
    "SlashCommandArgument",
    "SlashCommandMetadataInput",
]
