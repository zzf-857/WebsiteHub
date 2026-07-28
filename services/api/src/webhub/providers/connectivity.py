"""Probe a Provider endpoint to verify credentials.

This is the only place in the codebase that sends an account's Provider key to a
vendor outside of an actual Agent turn, so the rules are tighter than usual:

* **No vendor text ever reaches the caller.**  On a non-2xx response the body is
  never even read — vendor error payloads routinely embed the request URL, the
  echoed request body, and a prefix of the API key.  Every failure collapses
  into one of the fixed Chinese messages in ``_FAILURES``.
* **Configuration-safe.**  A probe never writes Provider configuration.  Model
  probes read a catalogue; search probes issue one minimal real query and may
  consume quota, which the UI states before the user starts it.
* **The target is re-validated immediately before the call**, because a
  hostname that passed validation at save time can be re-pointed at a private
  address afterwards.
* **Redirects are not followed.**  A 302 to ``http://169.254.169.254`` would
  otherwise walk straight past the SSRF check.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from webhub.providers.registry import ProviderDefinition
from webhub.providers.targets import ProviderTargetError, validate_connection_target

_USER_AGENT = "WebHub/0.1 (+https://github.com/webhub)"

# Ollama serves its native catalogue at the root and an OpenAI-compatible
# surface under /v1; users normally save the root, so strip a trailing /v1
# before appending the native path.
_OPENAI_COMPATIBLE_SUFFIX = "/v1"

# A model catalogue is a few KB at most.  The cap exists so a hostile or broken
# endpoint cannot stream an unbounded body into memory.
MAX_RESPONSE_BYTES = 512 * 1024
# Enough to cover any real catalogue while keeping the response payload small.
MAX_MODELS = 200
MAX_MODEL_NAME_LENGTH = 160


class ProviderProbeError(Exception):
    """A probe failure already reduced to a safe, caller-facing message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ProviderProbeResult:
    models: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ProviderSearchProbeResult:
    result_count: int


# Every failure mode maps to a fixed pair here.  Nothing derived from the
# vendor response is ever interpolated into these strings.
_FAILURES: dict[str, str] = {
    "provider_auth_failed": "API Key 无效或没有访问该接口的权限，请核对后重试",
    "provider_endpoint_not_found": "该地址下没有可用的连接测试接口，请检查 Base URL 是否填对",
    "provider_rate_limited": "服务商正在限流，请稍后再试",
    "provider_quota_exhausted": "搜索额度不足或已达到用量上限，请检查服务商账户",
    "provider_upstream_error": "服务商暂时不可用，请稍后再试",
    "provider_timeout": "连接服务商超时，请检查网络或 Base URL",
    "provider_unreachable": "无法连接到该地址，请检查 Base URL 是否可达",
    "provider_redirected": "该地址发生了跳转，出于安全考虑不会跟随，请填写最终地址",
    "provider_response_invalid": "服务商返回了无法识别的响应，请检查 Base URL 是否填对",
    "provider_response_too_large": "服务商返回的内容过大，已中止读取",
    "provider_request_failed": "连接服务商失败，请稍后再试",
}


def _fail(code: str) -> ProviderProbeError:
    return ProviderProbeError(code, _FAILURES[code])


def _status_code(status: int) -> str:
    if status in {402, 432, 433}:
        return "provider_quota_exhausted"
    if status in {401, 403}:
        return "provider_auth_failed"
    if status == 404:
        return "provider_endpoint_not_found"
    if status == 429:
        return "provider_rate_limited"
    if 300 <= status < 400:
        return "provider_redirected"
    if status >= 500:
        return "provider_upstream_error"
    # 400/405/422 and friends: almost always a Base URL that does not point at
    # the expected catalogue or search endpoint.
    return "provider_response_invalid"


def probe_base_url(definition: ProviderDefinition, stored_base_url: str | None) -> str:
    """Resolve the origin the probe should talk to, or raise if there is none."""

    candidate = (
        ""
        if definition.fixed_base_url
        else (stored_base_url or "").strip().rstrip("/")
    )
    if not candidate:
        candidate = (definition.default_base_url or "").rstrip("/")
    if not candidate:
        raise _fail("provider_unreachable")
    return candidate


def _catalogue_url(definition: ProviderDefinition, base_url: str) -> str:
    if definition.provider == "ollama":
        root = base_url
        if root.endswith(_OPENAI_COMPATIBLE_SUFFIX):
            root = root[: -len(_OPENAI_COMPATIBLE_SUFFIX)]
        return f"{root.rstrip('/')}/api/tags"
    return f"{base_url}/models"


def _model_names(payload: Any, definition: ProviderDefinition) -> list[str]:
    if not isinstance(payload, Mapping):
        raise _fail("provider_response_invalid")

    # OpenAI-compatible: {"data": [{"id": ...}]}.  Ollama: {"models": [{"name": ...}]}.
    if definition.provider == "ollama":
        entries, keys = payload.get("models"), ("name", "model")
    else:
        entries, keys = payload.get("data"), ("id", "name")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise _fail("provider_response_invalid")

    seen: set[str] = set()
    names: list[str] = []
    for entry in entries:
        raw: Any = entry
        if isinstance(entry, Mapping):
            raw = next((entry.get(key) for key in keys if entry.get(key)), None)
        if not isinstance(raw, str):
            continue
        name = " ".join(raw.split())[:MAX_MODEL_NAME_LENGTH]
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
        if len(names) >= MAX_MODELS:
            break
    # An endpoint that answers 200 with an empty catalogue is still a working
    # endpoint: the key was accepted.  Report it as a success with no models
    # rather than inventing a failure.
    return sorted(names)


def _search_result_count(payload: Any, definition: ProviderDefinition) -> int:
    if not isinstance(payload, Mapping):
        raise _fail("provider_response_invalid")
    key = "data" if definition.provider == "jina" else "results"
    entries = payload.get(key)
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise _fail("provider_response_invalid")
    return len(entries)


async def _request_json(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    timeout_seconds: int,
    params: Mapping[str, str] | None = None,
    json_body: Mapping[str, Any] | None = None,
) -> Any:
    import httpx

    body = bytearray()
    try:
        # Streamed rather than fetched whole so the body can be capped, and so a
        # non-2xx response is abandoned before a single byte of vendor error
        # text is read.
        async with (
            httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client,
            client.stream(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
            ) as response,
        ):
            if response.status_code >= 300:
                raise _fail(_status_code(response.status_code))
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise _fail("provider_response_too_large")
    except ProviderProbeError:
        raise
    except httpx.TimeoutException as error:
        raise _fail("provider_timeout") from error
    except httpx.TransportError as error:
        raise _fail("provider_unreachable") from error
    except Exception as error:  # noqa: BLE001 - vendor/client errors must not escape
        raise _fail("provider_request_failed") from error

    try:
        return json.loads(bytes(body))
    except ValueError as error:
        raise _fail("provider_response_invalid") from error


async def probe_models(
    definition: ProviderDefinition,
    *,
    base_url: str,
    api_key: str | None,
    timeout_seconds: int,
) -> ProviderProbeResult:
    """Issue one read-only catalogue request and return the model names.

    Raises ``ProviderProbeError`` for every failure; the caller may surface
    ``error.code`` and ``error.message`` verbatim.
    """

    try:
        # Re-resolve right now rather than trusting the check done at save time.
        await validate_connection_target(
            base_url,
            allow_private=definition.allows_private_base_url,
            timeout_seconds=timeout_seconds,
        )
    except ProviderTargetError as error:
        raise ProviderProbeError(error.code, error.message) from error

    headers = {"user-agent": _USER_AGENT, "accept": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"

    payload = await _request_json(
        method="GET",
        url=_catalogue_url(definition, base_url),
        headers=headers,
        timeout_seconds=timeout_seconds,
    )
    return ProviderProbeResult(models=_model_names(payload, definition))


async def probe_search(
    definition: ProviderDefinition,
    *,
    base_url: str,
    api_key: str | None,
    timeout_seconds: int,
) -> ProviderSearchProbeResult:
    """Run one minimal search to verify a search endpoint and credential.

    This is intentionally a real query: these vendors do not share a stable,
    read-only key-validation endpoint.  The fixed query carries no user data,
    requests at most one result where the vendor supports that control, and
    none of the returned third-party content is exposed to the caller.
    """

    if not definition.search_test_supported:
        raise _fail("provider_response_invalid")
    try:
        await validate_connection_target(
            base_url,
            allow_private=definition.allows_private_base_url,
            timeout_seconds=timeout_seconds,
        )
    except ProviderTargetError as error:
        raise ProviderProbeError(error.code, error.message) from error

    headers = {"user-agent": _USER_AGENT, "accept": "application/json"}
    query = "WebHub connectivity test"
    params: Mapping[str, str] | None = None
    json_body: Mapping[str, Any] | None = None
    url = base_url.rstrip("/")

    if definition.provider == "tavily":
        headers["authorization"] = f"Bearer {api_key or ''}"
        url = f"{url}/search"
        json_body = {
            "query": query,
            "search_depth": "basic",
            "max_results": 1,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
    elif definition.provider == "exa":
        headers["x-api-key"] = api_key or ""
        url = f"{url}/search"
        json_body = {"query": query, "numResults": 1}
    elif definition.provider == "jina":
        headers["authorization"] = f"Bearer {api_key or ''}"
        params = {"q": query}
    else:
        raise _fail("provider_response_invalid")

    payload = await _request_json(
        method="GET" if definition.provider == "jina" else "POST",
        url=url,
        headers=headers,
        timeout_seconds=timeout_seconds,
        params=params,
        json_body=json_body,
    )
    return ProviderSearchProbeResult(
        result_count=_search_result_count(payload, definition),
    )


__all__ = [
    "MAX_MODELS",
    "MAX_RESPONSE_BYTES",
    "ProviderProbeError",
    "ProviderProbeResult",
    "ProviderSearchProbeResult",
    "probe_base_url",
    "probe_models",
    "probe_search",
]
