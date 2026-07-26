"""LangGraph assembly for one account-scoped Agent turn.

Kept separate from the runner so the RAG slice can insert a retrieval node
without touching stream translation, and so tests can build a graph against a
fake chat model.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def build_agent_graph(
    *,
    model: Any,
    tools: Sequence[Any],
    system_prompt: str,
) -> Any:
    """Compile the tool-calling agent used for a single turn.

    No checkpointer is attached: conversation history is replayed from the
    WebHub tables, which stay the single source of truth for anything the user
    can see.  ``checkpoints.sqlite3`` is reserved for long-running background
    execution recovery instead.
    """

    from langgraph.prebuilt import create_react_agent

    return create_react_agent(
        model,
        list(tools),
        prompt=system_prompt,
        name="webhub-agent",
    )


__all__ = ["build_agent_graph"]
