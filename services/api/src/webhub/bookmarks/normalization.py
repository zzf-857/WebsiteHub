from __future__ import annotations

import ipaddress
from urllib.parse import SplitResult, urlsplit, urlunsplit

from webhub.bookmarks.models import (
    FetchPolicy,
    NormalizationStatus,
    NormalizedUrl,
)

_SUPPORTED_SCHEMES = {"http", "https"}
_LOCAL_HOST_SUFFIXES = (".internal", ".lan", ".local", ".localhost", ".home")
NORMALIZER_VERSION = "conservative-url.v1"


def _result(
    status: NormalizationStatus,
    *,
    normalized_url: str | None = None,
    host: str | None = None,
    fetch_policy: FetchPolicy | None = None,
    reason: str | None = None,
) -> NormalizedUrl:
    return NormalizedUrl(
        status=status,
        normalized_url=normalized_url,
        host=host,
        fetch_policy=fetch_policy,
        reason=reason,
    )


def _fetch_policy(host: str) -> FetchPolicy:
    comparable = host.casefold().rstrip(".")
    if comparable == "localhost" or comparable.endswith(_LOCAL_HOST_SUFFIXES):
        return FetchPolicy.EXPORT_METADATA_ONLY

    address_text = comparable.partition("%")[0]
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError:
        return FetchPolicy.PUBLIC_REVALIDATION_REQUIRED
    return (
        FetchPolicy.PUBLIC_REVALIDATION_REQUIRED
        if address.is_global
        else FetchPolicy.EXPORT_METADATA_ONLY
    )


def _normalized_netloc(parts: SplitResult, host: str, port: int | None) -> str:
    host_text = f"[{host}]" if ":" in host else host
    default_port = (parts.scheme.casefold() == "http" and port == 80) or (
        parts.scheme.casefold() == "https" and port == 443
    )
    return host_text if port is None or default_port else f"{host_text}:{port}"


def normalize_bookmark_url(raw_url: str) -> NormalizedUrl:
    candidate = raw_url.strip()
    if not candidate:
        return _result(NormalizationStatus.INVALID, reason="missing_url")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in candidate
    ):
        return _result(NormalizationStatus.INVALID, reason="control_character_in_url")

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return _result(NormalizationStatus.INVALID, reason="malformed_url")

    scheme = parts.scheme.casefold()
    if scheme not in _SUPPORTED_SCHEMES:
        reason = f"unsupported_scheme:{scheme or 'missing'}"
        return _result(NormalizationStatus.UNSUPPORTED, reason=reason)
    if parts.username is not None or parts.password is not None:
        return _result(NormalizationStatus.INVALID, reason="embedded_credentials")
    if not parts.hostname:
        return _result(NormalizationStatus.INVALID, reason="missing_host")

    try:
        port = parts.port
    except ValueError:
        return _result(NormalizationStatus.INVALID, reason="invalid_port")

    host = parts.hostname.rstrip(".").casefold()
    try:
        normalized_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return _result(NormalizationStatus.INVALID, reason="invalid_idn_host")
    if not normalized_host:
        return _result(NormalizationStatus.INVALID, reason="missing_host")

    normalized_url = urlunsplit(
        (
            scheme,
            _normalized_netloc(parts, normalized_host, port),
            parts.path or "/",
            parts.query,
            parts.fragment,
        )
    )
    return _result(
        NormalizationStatus.ACCEPTED,
        normalized_url=normalized_url,
        host=normalized_host,
        fetch_policy=_fetch_policy(normalized_host),
    )
