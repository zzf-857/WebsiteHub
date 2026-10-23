from __future__ import annotations

import json
from collections.abc import Mapping


def provider_config_values(config_json: str) -> dict[str, object]:
    """Read optional settings without making older or damaged rows unusable."""

    try:
        value = json.loads(config_json)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def stream_usage_enabled(provider: str, config: Mapping[str, object]) -> bool:
    """Compatible endpoints must opt in; first-party usage remains enabled."""

    if provider in {"openai", "deepseek"}:
        return True
    return provider == "openai_compatible" and config.get("stream_usage") is True
