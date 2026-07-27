from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from webhub.streaming.ui_message_stream import (
    CLIENT_ERROR_TEXT,
    UIMessageStreamEncoder,
    UIMessageStreamEncodingError,
    UIMessageStreamStateError,
    abort_chunk,
    data_chunk,
    encode_ui_message_chunk,
    encode_ui_message_stream,
    error_chunk,
    finish_chunk,
    message_metadata_chunk,
    reasoning_delta_chunk,
    reasoning_end_chunk,
    reasoning_start_chunk,
    start_chunk,
    text_delta_chunk,
    text_end_chunk,
    text_start_chunk,
    ui_message_stream_headers,
    ui_message_stream_response,
)


def _events(payload: bytes) -> list[str]:
    return [event for event in payload.decode("utf-8").split("\n\n") if event]


def _collect(iterator: Any) -> bytes:
    async def collect() -> bytes:
        return b"".join([part async for part in iterator])

    return asyncio.run(collect())


def _fixture_path() -> Path:
    return (
        Path(__file__).resolve().parents[4]
        / "packages"
        / "contracts"
        / "fixtures"
        / "ai-sdk-ui-message-stream-v1.sse"
    )


def test_golden_fixture_has_expected_v1_parts_and_done_marker() -> None:
    fixture = _fixture_path().read_bytes()
    events = _events(fixture)

    assert events[-1] == "data: [DONE]"
    chunks = [json.loads(event.removeprefix("data: ")) for event in events[:-1]]
    assert [chunk["type"] for chunk in chunks] == [
        "start",
        "text-start",
        "text-delta",
        "text-delta",
        "text-end",
        "message-metadata",
        "data-rag-sources",
        "finish",
    ]
    assert chunks[0]["messageMetadata"]["model"] == "webhub-fixture"
    assert chunks[6]["data"]["items"][0]["siteId"] == "site-001"
    assert chunks[-1]["finishReason"] == "stop"


def test_python_encoder_reproduces_cross_stack_golden_fixture() -> None:
    chunks = [
        start_chunk(
            message_id="assistant-fixture-001",
            message_metadata={
                "createdAt": "2026-07-26T00:00:00Z",
                "model": "webhub-fixture",
            },
        ),
        text_start_chunk("text-001"),
        text_delta_chunk("text-001", "Hello, "),
        text_delta_chunk("text-001", "WebHub."),
        text_end_chunk("text-001"),
        message_metadata_chunk({"phase": "answering"}),
        data_chunk(
            "rag-sources",
            {
                "items": [
                    {
                        "siteId": "site-001",
                        "title": "Example",
                        "url": "https://example.com",
                    }
                ]
            },
            part_id="sources-001",
        ),
        finish_chunk(
            finish_reason="stop",
            message_metadata={
                "createdAt": "2026-07-26T00:00:00Z",
                "model": "webhub-fixture",
                "totalTokens": 4,
            },
        ),
    ]

    assert _collect(encode_ui_message_stream(chunks)) == _fixture_path().read_bytes()


def test_text_metadata_data_and_finish_encode_deterministically() -> None:
    chunks = [
        start_chunk(message_id="assistant-fixture-001", message_metadata={"model": "test"}),
        text_start_chunk("text-001"),
        text_delta_chunk("text-001", "Hello, "),
        text_delta_chunk("text-001", "WebHub."),
        text_end_chunk("text-001"),
        message_metadata_chunk({"phase": "answering"}),
        data_chunk(
            "rag-sources",
            {"items": [{"siteId": "site-001", "title": "Example"}]},
            part_id="sources-001",
        ),
        finish_chunk(finish_reason="stop", message_metadata={"totalTokens": 4}),
    ]
    first = _collect(encode_ui_message_stream(chunks))
    second = _collect(encode_ui_message_stream(chunks))

    assert first == second
    assert _events(first)[0] == (
        'data: {"type":"start","messageId":"assistant-fixture-001",'
        '"messageMetadata":{"model":"test"}}'
    )
    assert _events(first)[-1] == "data: [DONE]"


def test_partial_abort_keeps_open_text_and_marks_stream_partial() -> None:
    encoder = UIMessageStreamEncoder()
    encoder.encode(start_chunk(message_id="assistant-1"))
    encoder.encode(text_start_chunk("text-1"))
    encoder.encode(text_delta_chunk("text-1", "partial output"))
    encoded_abort = encoder.encode(abort_chunk("user cancelled"))

    assert encoder.is_partial is True
    assert encoder.active_text_ids == {"text-1"}
    assert json.loads(encoded_abort.decode().removeprefix("data: ").strip()) == {
        "type": "abort",
        "reason": "user cancelled",
    }
    assert encoder.finalize() == b"data: [DONE]\n\n"


def test_error_can_terminate_a_partial_message_without_leaking_details() -> None:
    secret = "provider key should never be sent"

    def source():
        yield start_chunk(message_id="assistant-1")
        yield text_start_chunk("text-1")
        yield text_delta_chunk("text-1", "partial")
        raise RuntimeError(secret)

    body = _collect(encode_ui_message_stream(source()))
    events = _events(body)
    assert secret not in body.decode("utf-8")
    assert json.loads(events[-2].removeprefix("data: ")) == {
        "type": "error",
        "errorText": CLIENT_ERROR_TEXT,
    }
    assert events[-1] == "data: [DONE]"


def test_json_serialization_failures_are_recovered_as_generic_error() -> None:
    body = _collect(
        encode_ui_message_stream(
            [
                start_chunk(message_id="assistant-1"),
                data_chunk("bad", object()),
            ]
        )
    )
    events = _events(body)
    assert json.loads(events[-2].removeprefix("data: "))["errorText"] == CLIENT_ERROR_TEXT
    assert events[-1] == "data: [DONE]"


def test_nonfinite_json_values_raise_in_strict_encoder() -> None:
    with pytest.raises(UIMessageStreamEncodingError, match="JSON serializable"):
        encode_ui_message_chunk({"type": "data-metrics", "data": {"score": float("nan")}})


def test_crlf_payload_is_escaped_and_cannot_create_extra_sse_events() -> None:
    encoded = encode_ui_message_chunk(
        {
            "type": "data-note",
            "data": {"message": "first\r\n\ndata: injected"},
        }
    )
    decoded = encoded.decode("utf-8")
    assert decoded.count("\n\n") == 1
    assert [line for line in decoded.splitlines() if line.startswith("data: ")] == [
        decoded.splitlines()[0]
    ]
    assert "\ndata: injected" not in decoded
    assert "\\r\\n" in decoded


def test_state_machine_rejects_invalid_text_order_and_open_finish() -> None:
    encoder = UIMessageStreamEncoder()
    with pytest.raises(UIMessageStreamStateError, match="before text-start"):
        encoder.encode(text_delta_chunk("text-1", "oops"))

    encoder.encode(text_start_chunk("text-1"))
    with pytest.raises(UIMessageStreamStateError, match="open text parts"):
        encoder.encode(finish_chunk(finish_reason="stop"))

    encoder.encode(text_end_chunk("text-1"))
    encoder.encode(finish_chunk(finish_reason="stop"))
    encoder.finalize()
    with pytest.raises(UIMessageStreamStateError, match="already finalized"):
        encoder.finalize()


def test_reasoning_parts_require_a_complete_lifecycle_before_finish() -> None:
    encoder = UIMessageStreamEncoder()
    with pytest.raises(UIMessageStreamStateError, match="before reasoning-start"):
        encoder.encode(reasoning_delta_chunk("reasoning-1", "oops"))

    encoder.encode(start_chunk(message_id="assistant-1"))
    encoder.encode(reasoning_start_chunk("reasoning-1"))
    encoder.encode(reasoning_delta_chunk("reasoning-1", "先分析。"))
    with pytest.raises(UIMessageStreamStateError, match="open reasoning parts"):
        encoder.encode(finish_chunk(finish_reason="stop"))

    encoder.encode(reasoning_end_chunk("reasoning-1"))
    encoder.encode(finish_chunk(finish_reason="stop"))
    assert encoder.finalize() == b"data: [DONE]\n\n"


def test_headers_include_v1_and_no_store_and_reject_crlf() -> None:
    headers = ui_message_stream_headers({"x-request-id": "fixture-1"})
    assert headers["x-vercel-ai-ui-message-stream"] == "v1"
    assert headers["cache-control"] == "no-cache, no-store"
    assert headers["x-request-id"] == "fixture-1"

    with pytest.raises(ValueError, match="CR/LF"):
        ui_message_stream_headers({"x-request-id": "bad\r\nX-Evil: 1"})


def test_fastapi_response_uses_stream_contract_headers_and_body() -> None:
    response = ui_message_stream_response(
        [
            start_chunk(message_id="assistant-1"),
            text_start_chunk("text-1"),
            text_delta_chunk("text-1", "ok"),
            text_end_chunk("text-1"),
            finish_chunk(finish_reason="stop"),
        ],
        headers={"x-request-id": "fixture-1"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache, no-store"
    assert response.headers["x-vercel-ai-ui-message-stream"] == "v1"
    assert response.headers["x-request-id"] == "fixture-1"
    assert _events(_collect(response.body_iterator))[-1] == "data: [DONE]"


def test_explicit_error_chunk_is_valid_protocol_data() -> None:
    encoded = encode_ui_message_chunk(error_chunk("safe client message"))
    assert json.loads(encoded.decode().removeprefix("data: ").strip()) == {
        "type": "error",
        "errorText": "safe client message",
    }
