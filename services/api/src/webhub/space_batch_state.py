"""Strict persisted state for the newest Agent Space batch proposal."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SPACE_BATCH_STATE_ARTIFACT = "agent-space-batch-state"
_LEGACY_PENDING_ARTIFACT = "agent-pending-space-batch"
_TERMINAL_STATUSES = {"noop", "rejected"}


def _tool_call_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized and len(normalized) <= 200 else None


def _pending_draft(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or value.get("kind") != "space_batch":
        return None
    target = value.get("target")
    if not isinstance(target, Mapping):
        return None
    mode = target.get("mode")
    space_name = target.get("space_name")
    if (
        not isinstance(mode, str)
        or mode not in {"existing", "create"}
        or not isinstance(space_name, str)
        or not space_name.strip()
        or len(space_name) > 120
    ):
        return None
    normalized_target: dict[str, Any] = {
        "mode": mode,
        "space_name": space_name.strip(),
    }
    if mode == "existing":
        space_id = target.get("space_id")
        expected_version = target.get("expected_version")
        if (
            not isinstance(space_id, str)
            or not space_id.strip()
            or len(space_id) > 36
            or not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version < 1
        ):
            return None
        normalized_target.update(
            {
                "space_id": space_id.strip(),
                "expected_version": expected_version,
            }
        )
    elif target.get("space_id") is not None or target.get("expected_version") is not None:
        return None

    raw_sites = value.get("sites")
    if not isinstance(raw_sites, list) or len(raw_sites) > 100:
        return None
    sites: list[dict[str, str]] = []
    seen_site_ids: set[str] = set()
    for raw_site in raw_sites:
        if not isinstance(raw_site, Mapping):
            return None
        site_id = raw_site.get("site_id")
        name = raw_site.get("name")
        if (
            not isinstance(site_id, str)
            or not site_id.strip()
            or len(site_id) > 36
            or site_id in seen_site_ids
            or not isinstance(name, str)
            or not name.strip()
            or len(name) > 160
        ):
            return None
        seen_site_ids.add(site_id)
        sites.append({"site_id": site_id, "name": " ".join(name.split())})

    already_member_count = value.get("already_member_count")
    if (
        not isinstance(already_member_count, int)
        or isinstance(already_member_count, bool)
        or already_member_count < 0
        or already_member_count > len(sites)
    ):
        return None
    return {
        "kind": "space_batch",
        "target": normalized_target,
        "sites": sites,
        "already_member_count": already_member_count,
    }


def space_batch_state_from_tool_result(value: Any) -> dict[str, Any] | None:
    """Convert any Space batch result into a pending state or tombstone."""

    if not isinstance(value, Mapping) or value.get("name") != "propose_space_batch":
        return None
    tool_call_id = _tool_call_id(value.get("toolCallId"))
    if tool_call_id is None:
        return None
    result = value.get("result")
    if not isinstance(result, Mapping):
        status = "rejected"
        draft = None
    else:
        raw_status = result.get("status")
        status = (
            raw_status
            if isinstance(raw_status, str) and raw_status in _TERMINAL_STATUSES
            else "rejected"
        )
        draft = None
        if raw_status == "awaiting_confirmation":
            draft = _pending_draft(result.get("draft"))
            if draft is not None:
                status = "awaiting_confirmation"
    state: dict[str, Any] = {
        "type": SPACE_BATCH_STATE_ARTIFACT,
        "toolCallId": tool_call_id,
        "status": status,
    }
    if draft is not None:
        state["draft"] = draft
    return state


def normalize_space_batch_state_artifact(value: Any) -> dict[str, Any] | None:
    """Revalidate a stored state before history or confirmation may trust it."""

    if not isinstance(value, Mapping):
        return None
    artifact_type = value.get("type")
    if artifact_type == _LEGACY_PENDING_ARTIFACT:
        return space_batch_state_from_tool_result(
            {
                "toolCallId": value.get("toolCallId"),
                "name": "propose_space_batch",
                "result": {
                    "status": "awaiting_confirmation",
                    "draft": value.get("draft"),
                },
            }
        )
    if artifact_type != SPACE_BATCH_STATE_ARTIFACT:
        return None
    tool_call_id = _tool_call_id(value.get("toolCallId"))
    status = value.get("status")
    if (
        tool_call_id is None
        or not isinstance(status, str)
        or status not in {"awaiting_confirmation", *_TERMINAL_STATUSES}
    ):
        return None
    state: dict[str, Any] = {
        "type": SPACE_BATCH_STATE_ARTIFACT,
        "toolCallId": tool_call_id,
        "status": status,
    }
    if status == "awaiting_confirmation":
        draft = _pending_draft(value.get("draft"))
        if draft is None:
            return None
        state["draft"] = draft
    return state


def space_batch_state_artifacts(sources: Sequence[Any]) -> list[dict[str, Any]]:
    """Persist exactly the latest Space batch result from one assistant turn."""

    for source in reversed(sources):
        state = space_batch_state_from_tool_result(source)
        if state is not None:
            return [state]
    return []


__all__ = [
    "SPACE_BATCH_STATE_ARTIFACT",
    "normalize_space_batch_state_artifact",
    "space_batch_state_artifacts",
    "space_batch_state_from_tool_result",
]
