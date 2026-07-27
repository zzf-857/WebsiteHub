"""A small, deterministic encoder for the AI SDK UI Message Stream v1.

The WebHub API is implemented in Python while the browser uses Vercel AI SDK
7.x.  The SDK's data protocol is deliberately simple: one compact JSON object
per SSE ``data`` event followed by a ``[DONE]`` marker.  This module keeps the
wire contract independent from routes and from any model/provider adapter.

Only the chunk families needed by the first agent slice are interpreted here:
message start/metadata, text parts, typed ``data-*`` parts, finish, error and
abort.  Unknown fields are preserved so provider metadata can be added without
changing this encoder.  Unknown chunk *types* are accepted as long as they are
objects with a string ``type``; this lets later slices add tool/source parts
without changing the framing layer.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi.responses import StreamingResponse

type JSONChunk = Mapping[str, Any]
type ChunkSource = Iterable[JSONChunk] | AsyncIterable[JSONChunk]
type TerminalType = Literal["finish", "error", "abort"]

CLIENT_ERROR_TEXT = "An error occurred."
DONE_MARKER = b"data: [DONE]\n\n"

# These values mirror ai@7.0.37's UI_MESSAGE_STREAM_HEADERS.  ``no-store`` is
# intentionally added to the cache directive for WebHub: the SDK only says
# ``no-cache``, while chat responses must never be persisted by a shared proxy.
UI_MESSAGE_STREAM_HEADERS: dict[str, str] = {
    "content-type": "text/event-stream",
    "cache-control": "no-cache, no-store",
    "connection": "keep-alive",
    "x-vercel-ai-ui-message-stream": "v1",
    "x-accel-buffering": "no",
}

_FINISH_REASONS = {
    "stop",
    "length",
    "content-filter",
    "tool-calls",
    "error",
    "other",
}
_JSON_DUMPS_KWARGS = {
    "ensure_ascii": False,
    "allow_nan": False,
    "separators": (",", ":"),
}


class UIMessageStreamEncodingError(ValueError):
    """Raised when a chunk cannot be represented safely on the wire."""


class UIMessageStreamStateError(ValueError):
    """Raised when chunks violate the UI Message Stream lifecycle."""


def _require_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise UIMessageStreamEncodingError(f"{field_name} must be a string")
    return value


def _validate_chunk_shape(chunk: JSONChunk) -> None:
    if not isinstance(chunk, Mapping):
        raise UIMessageStreamEncodingError("UI message chunk must be an object")
    chunk_type = _require_string(chunk.get("type"), field_name="type")
    if not chunk_type:
        raise UIMessageStreamEncodingError("type must not be empty")

    # Validate fields consumed by this module.  Other SDK fields are left
    # untouched for forward compatibility (for example providerMetadata).
    if chunk_type in {
        "text-start",
        "text-delta",
        "text-end",
        "reasoning-start",
        "reasoning-delta",
        "reasoning-end",
    }:
        _require_string(chunk.get("id"), field_name="id")
        if chunk_type in {"text-delta", "reasoning-delta"}:
            _require_string(chunk.get("delta"), field_name="delta")
    elif chunk_type == "start":
        if "messageId" in chunk and chunk["messageId"] is not None:
            _require_string(chunk["messageId"], field_name="messageId")
    elif chunk_type in {"error", "abort"}:
        field_name = "errorText" if chunk_type == "error" else "reason"
        if field_name in chunk and chunk[field_name] is not None:
            _require_string(chunk[field_name], field_name=field_name)
    elif chunk_type == "finish":
        finish_reason = chunk.get("finishReason")
        if finish_reason is not None:
            _require_string(finish_reason, field_name="finishReason")
            if finish_reason not in _FINISH_REASONS:
                raise UIMessageStreamEncodingError(f"unsupported finishReason: {finish_reason}")
    elif chunk_type == "message-metadata":
        if "messageMetadata" not in chunk:
            raise UIMessageStreamEncodingError("message-metadata requires messageMetadata")
    elif chunk_type.startswith("data-"):
        if "data" not in chunk:
            raise UIMessageStreamEncodingError("data-* chunk requires data")
        if "id" in chunk and chunk["id"] is not None:
            _require_string(chunk["id"], field_name="id")
        if "transient" in chunk and not isinstance(chunk["transient"], bool):
            raise UIMessageStreamEncodingError("transient must be a boolean")


def _json_payload(value: object) -> str:
    try:
        encoded = json.dumps(value, **_JSON_DUMPS_KWARGS)
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        # Do not include repr(value): it can contain credentials or huge model
        # output, and reprs are not guaranteed to be safe for an SSE response.
        raise UIMessageStreamEncodingError("UI message chunk is not JSON serializable") from error

    # json.dumps escapes all JSON control characters.  Keep this assertion as
    # a guard against a future encoder change accidentally enabling SSE line
    # injection through a user-controlled string or mapping key.
    if "\r" in encoded or "\n" in encoded:
        raise UIMessageStreamEncodingError("UI message chunk contains an unsafe SSE line break")
    return encoded


def encode_sse_event(payload: object) -> bytes:
    """Encode one JSON-compatible object as one UI Message SSE event."""

    encoded = _json_payload(payload)
    return f"data: {encoded}\n\n".encode()


def encode_done() -> bytes:
    """Return the AI SDK stream terminator."""

    return DONE_MARKER


def encode_ui_message_chunk(chunk: JSONChunk) -> bytes:
    """Validate and encode one UI Message chunk without lifecycle tracking."""

    _validate_chunk_shape(chunk)
    return encode_sse_event(chunk)


def _chunk(*, chunk_type: str, **fields: object) -> dict[str, object]:
    value: dict[str, object] = {"type": chunk_type}
    value.update(fields)
    return value


def start_chunk(
    *, message_id: str | None = None, message_metadata: object = None
) -> dict[str, object]:
    fields: dict[str, object] = {}
    if message_id is not None:
        fields["messageId"] = message_id
    if message_metadata is not None:
        fields["messageMetadata"] = message_metadata
    return _chunk(chunk_type="start", **fields)


def text_start_chunk(text_id: str) -> dict[str, object]:
    return _chunk(chunk_type="text-start", id=text_id)


def text_delta_chunk(text_id: str, delta: str) -> dict[str, object]:
    return _chunk(chunk_type="text-delta", id=text_id, delta=delta)


def text_end_chunk(text_id: str) -> dict[str, object]:
    return _chunk(chunk_type="text-end", id=text_id)


def reasoning_start_chunk(reasoning_id: str) -> dict[str, object]:
    return _chunk(chunk_type="reasoning-start", id=reasoning_id)


def reasoning_delta_chunk(reasoning_id: str, delta: str) -> dict[str, object]:
    return _chunk(chunk_type="reasoning-delta", id=reasoning_id, delta=delta)


def reasoning_end_chunk(reasoning_id: str) -> dict[str, object]:
    return _chunk(chunk_type="reasoning-end", id=reasoning_id)


def message_metadata_chunk(message_metadata: object) -> dict[str, object]:
    return _chunk(chunk_type="message-metadata", messageMetadata=message_metadata)


def data_chunk(
    name: str,
    data: object,
    *,
    part_id: str | None = None,
    transient: bool = False,
) -> dict[str, object]:
    """Build a typed ``data-{name}`` chunk.

    The SDK treats the suffix as an application-defined type name.  We only
    reject an empty name here; punctuation and unicode remain valid and are
    safely JSON encoded just like the JavaScript SDK.
    """

    if not isinstance(name, str) or not name:
        raise UIMessageStreamEncodingError("data part name must not be empty")
    if not isinstance(transient, bool):
        raise UIMessageStreamEncodingError("transient must be a boolean")
    fields: dict[str, object] = {}
    if part_id is not None:
        fields["id"] = part_id
    fields["data"] = data
    if transient:
        fields["transient"] = True
    return _chunk(chunk_type=f"data-{name}", **fields)


def finish_chunk(
    *, finish_reason: str | None = None, message_metadata: object = None
) -> dict[str, object]:
    fields: dict[str, object] = {}
    if finish_reason is not None:
        fields["finishReason"] = finish_reason
    if message_metadata is not None:
        fields["messageMetadata"] = message_metadata
    return _chunk(chunk_type="finish", **fields)


def error_chunk(error_text: str = CLIENT_ERROR_TEXT) -> dict[str, object]:
    return _chunk(chunk_type="error", errorText=error_text)


def abort_chunk(reason: str | None = None) -> dict[str, object]:
    fields: dict[str, object] = {}
    if reason is not None:
        fields["reason"] = reason
    return _chunk(chunk_type="abort", **fields)


@dataclass
class UIMessageStreamEncoder:
    """Encode chunks while checking text-part and terminal-state ordering.

    ``error`` and ``abort`` intentionally allow an open text part.  The AI SDK
    uses that shape for a partial assistant response; the accumulated deltas
    remain available for persistence/replay.  A normal ``finish`` requires all
    text parts to have an explicit ``text-end``.
    """

    active_text_ids: set[str] = field(default_factory=set)
    seen_text_ids: set[str] = field(default_factory=set)
    active_reasoning_ids: set[str] = field(default_factory=set)
    seen_reasoning_ids: set[str] = field(default_factory=set)
    terminal: TerminalType | None = None
    emitted_chunks: int = 0
    finalized: bool = False

    @property
    def is_partial(self) -> bool:
        return self.terminal in {"error", "abort"}

    def encode(self, chunk: JSONChunk) -> bytes:
        if self.finalized:
            raise UIMessageStreamStateError("stream is already finalized")
        _validate_chunk_shape(chunk)
        chunk_type = str(chunk["type"])

        if self.terminal is not None:
            raise UIMessageStreamStateError(
                f"cannot emit {chunk_type!r} after terminal {self.terminal!r}"
            )

        if chunk_type == "text-start":
            text_id = str(chunk["id"])
            if text_id in self.active_text_ids or text_id in self.seen_text_ids:
                raise UIMessageStreamStateError(f"text part id {text_id!r} was already started")
        elif chunk_type == "text-delta":
            text_id = str(chunk["id"])
            if text_id not in self.active_text_ids:
                raise UIMessageStreamStateError(
                    f"text-delta received before text-start for {text_id!r}"
                )
        elif chunk_type == "text-end":
            text_id = str(chunk["id"])
            if text_id not in self.active_text_ids:
                raise UIMessageStreamStateError(
                    f"text-end received before text-start for {text_id!r}"
                )
        elif chunk_type == "reasoning-start":
            reasoning_id = str(chunk["id"])
            if (
                reasoning_id in self.active_reasoning_ids
                or reasoning_id in self.seen_reasoning_ids
            ):
                raise UIMessageStreamStateError(
                    f"reasoning part id {reasoning_id!r} was already started"
                )
        elif chunk_type == "reasoning-delta":
            reasoning_id = str(chunk["id"])
            if reasoning_id not in self.active_reasoning_ids:
                raise UIMessageStreamStateError(
                    f"reasoning-delta received before reasoning-start for {reasoning_id!r}"
                )
        elif chunk_type == "reasoning-end":
            reasoning_id = str(chunk["id"])
            if reasoning_id not in self.active_reasoning_ids:
                raise UIMessageStreamStateError(
                    f"reasoning-end received before reasoning-start for {reasoning_id!r}"
                )
        elif chunk_type in {"finish", "error", "abort"}:
            if chunk_type == "finish" and self.active_text_ids:
                active = ", ".join(sorted(self.active_text_ids))
                raise UIMessageStreamStateError(f"finish received with open text parts: {active}")
            if chunk_type == "finish" and self.active_reasoning_ids:
                active = ", ".join(sorted(self.active_reasoning_ids))
                raise UIMessageStreamStateError(
                    f"finish received with open reasoning parts: {active}"
                )

        # Serialize before mutating lifecycle state.  A failed JSON encoding
        # must not leave a text part marked active when its start event was
        # never actually sent to the client.
        encoded = encode_sse_event(chunk)
        if chunk_type == "text-start":
            self.active_text_ids.add(str(chunk["id"]))
            self.seen_text_ids.add(str(chunk["id"]))
        elif chunk_type == "text-end":
            self.active_text_ids.remove(str(chunk["id"]))
        elif chunk_type == "reasoning-start":
            self.active_reasoning_ids.add(str(chunk["id"]))
            self.seen_reasoning_ids.add(str(chunk["id"]))
        elif chunk_type == "reasoning-end":
            self.active_reasoning_ids.remove(str(chunk["id"]))
        elif chunk_type in {"finish", "error", "abort"}:
            self.terminal = chunk_type  # type: ignore[assignment]
        self.emitted_chunks += 1
        return encoded

    def finalize(self, *, require_terminal: bool = True) -> bytes:
        if self.finalized:
            raise UIMessageStreamStateError("stream is already finalized")
        if require_terminal and self.terminal is None:
            raise UIMessageStreamStateError("stream ended without finish, error, or abort")
        self.finalized = True
        return encode_done()


def _safe_error_chunk(encoder: UIMessageStreamEncoder, error_text: str) -> bytes:
    """Emit a generic error only when the stream has not already terminated."""

    if encoder.terminal is not None:
        return b""
    return encoder.encode(error_chunk(error_text))


async def encode_ui_message_stream(
    chunks: ChunkSource,
    *,
    require_terminal: bool = True,
    recover_errors: bool = True,
    client_error_text: str = CLIENT_ERROR_TEXT,
) -> AsyncIterator[bytes]:
    """Turn sync/async chunks into an SSE byte stream.

    With ``recover_errors=True`` (the response default), producer and JSON
    serialization failures become a non-sensitive ``error`` chunk followed by
    ``[DONE]``.  Cancellation is deliberately not caught: a disconnected
    client must not be represented as an explicit AI SDK ``abort`` event.
    """

    encoder = UIMessageStreamEncoder()
    try:
        if isinstance(chunks, AsyncIterable):
            async for chunk in chunks:
                yield encoder.encode(chunk)
        else:
            for chunk in chunks:
                yield encoder.encode(chunk)
        yield encoder.finalize(require_terminal=require_terminal)
    except Exception:
        if not recover_errors:
            raise
        error_event = _safe_error_chunk(encoder, client_error_text)
        if error_event:
            yield error_event
        if not encoder.finalized:
            yield encoder.finalize(require_terminal=False)


def ui_message_stream_headers(
    extra_headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return safe response headers, rejecting CR/LF header injection."""

    result = dict(UI_MESSAGE_STREAM_HEADERS)
    for name, value in (extra_headers or {}).items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("SSE headers must contain string names and values")
        if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
            raise ValueError("SSE headers cannot contain CR/LF")
        # Protocol headers are authoritative; callers may add tracing headers
        # but cannot accidentally turn off no-store or the v1 marker.
        if name.lower() not in {key.lower() for key in UI_MESSAGE_STREAM_HEADERS}:
            result[name] = value
    return result


def ui_message_stream_response(
    chunks: ChunkSource,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> StreamingResponse:
    """Create a FastAPI response compatible with ``DefaultChatTransport``."""

    return StreamingResponse(
        encode_ui_message_stream(chunks),
        status_code=status_code,
        headers=ui_message_stream_headers(headers),
    )


__all__ = [
    "CLIENT_ERROR_TEXT",
    "DONE_MARKER",
    "UI_MESSAGE_STREAM_HEADERS",
    "UIMessageStreamEncoder",
    "UIMessageStreamEncodingError",
    "UIMessageStreamStateError",
    "abort_chunk",
    "data_chunk",
    "encode_done",
    "encode_sse_event",
    "encode_ui_message_chunk",
    "encode_ui_message_stream",
    "error_chunk",
    "finish_chunk",
    "message_metadata_chunk",
    "reasoning_delta_chunk",
    "reasoning_end_chunk",
    "reasoning_start_chunk",
    "start_chunk",
    "text_delta_chunk",
    "text_end_chunk",
    "text_start_chunk",
    "ui_message_stream_headers",
    "ui_message_stream_response",
]
