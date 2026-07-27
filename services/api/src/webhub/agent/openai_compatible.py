"""Small compatibility extensions for OpenAI-shaped chat streams."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI


def reasoning_content_from_chunk(chunk: Mapping[str, Any]) -> str | None:
    """Return only an explicit ``delta.reasoning_content`` string.

    Some OpenAI-compatible vendors preserve this extension on the SDK chunk,
    while ``langchain-openai`` intentionally ignores non-OpenAI delta fields.
    Never infer reasoning from ordinary content: an absent extension means the
    provider did not expose reasoning to WebHub.
    """

    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, Mapping):
        return None
    delta = first.get("delta")
    if not isinstance(delta, Mapping):
        return None
    reasoning = delta.get("reasoning_content")
    return reasoning if isinstance(reasoning, str) and reasoning else None


class ReasoningCompatibleChatOpenAI(ChatOpenAI):
    """Preserve an OpenAI-compatible vendor's explicit reasoning delta."""

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Replay Provider-supplied reasoning on assistant tool-call turns.

        DeepSeek requires the original ``reasoning_content`` to accompany an
        assistant tool call when that message is sent back for the next model
        step.  ``langchain-openai`` deliberately drops vendor extension fields
        while converting messages, so restore only the explicit value captured
        from the Provider.  Ordinary answer text is never treated as reasoning.
        """

        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        payload_messages = payload.get("messages")
        if not isinstance(payload_messages, list):
            return payload

        for message, payload_message in zip(messages, payload_messages, strict=False):
            if not isinstance(message, AIMessage) or not isinstance(payload_message, dict):
                continue
            reasoning = message.additional_kwargs.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                payload_message["reasoning_content"] = reasoning
        return payload

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict[str, Any],
        default_chunk_class: type,
        base_generation_info: dict[str, Any] | None,
    ) -> ChatGenerationChunk | None:
        generation = super()._convert_chunk_to_generation_chunk(
            chunk,
            default_chunk_class,
            base_generation_info,
        )
        reasoning = reasoning_content_from_chunk(chunk)
        if reasoning is not None and generation is not None:
            message = generation.message
            if isinstance(message, AIMessageChunk):
                message.additional_kwargs["reasoning_content"] = reasoning
        return generation


__all__ = ["ReasoningCompatibleChatOpenAI", "reasoning_content_from_chunk"]
