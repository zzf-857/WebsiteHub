from __future__ import annotations

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from webhub.agent.openai_compatible import (
    ReasoningCompatibleChatOpenAI,
    reasoning_content_from_chunk,
)


def _model() -> ReasoningCompatibleChatOpenAI:
    return ReasoningCompatibleChatOpenAI(
        model="deepseek-reasoner",
        api_key="test-key",
    )


def test_explicit_reasoning_delta_is_preserved_on_generation_chunk() -> None:
    raw_chunk = {
        "id": "chunk-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "deepseek-reasoner",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "先检查网址库。",
                },
                "finish_reason": None,
            }
        ],
    }

    generation = _model()._convert_chunk_to_generation_chunk(
        raw_chunk,
        AIMessageChunk,
        None,
    )

    assert generation is not None
    assert generation.message.additional_kwargs["reasoning_content"] == "先检查网址库。"


def test_ordinary_content_is_never_inferred_as_reasoning() -> None:
    assert (
        reasoning_content_from_chunk(
            {
                "choices": [
                    {
                        "delta": {
                            "content": "这只是最终回答。",
                        }
                    }
                ]
            }
        )
        is None
    )
    assert reasoning_content_from_chunk({"choices": []}) is None


def test_explicit_reasoning_is_replayed_with_assistant_tool_call() -> None:
    assistant = AIMessage(
        content="",
        additional_kwargs={"reasoning_content": "需要先检索站内资料。"},
        tool_calls=[
            {
                "id": "call-1",
                "name": "search_library",
                "args": {"query": "RAG"},
            }
        ],
    )
    payload = _model()._get_request_payload(
        [
            HumanMessage(content="帮我找 RAG 网站"),
            assistant,
            ToolMessage(
                content='{"items":[]}',
                tool_call_id="call-1",
                name="search_library",
            ),
        ]
    )

    assert payload["messages"][1]["reasoning_content"] == "需要先检索站内资料。"
    assert "reasoning_content" not in payload["messages"][0]
    assert "reasoning_content" not in payload["messages"][2]


def test_missing_reasoning_is_not_added_to_outbound_payload() -> None:
    payload = _model()._get_request_payload(
        [
            HumanMessage(content="你好"),
            AIMessage(content="你好，有什么可以帮你？"),
        ]
    )

    assert all("reasoning_content" not in message for message in payload["messages"])
