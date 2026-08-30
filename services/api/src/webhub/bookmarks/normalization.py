from __future__ import annotations

import ipaddress
import re
from urllib.parse import SplitResult, urlsplit, urlunsplit

from webhub.bookmarks.models import (
    FetchPolicy,
    NormalizationStatus,
    NormalizedUrl,
)

_SUPPORTED_SCHEMES = {"http", "https"}
_LOCAL_HOST_SUFFIXES = (
    ".internal",
    ".lan",
    ".local",
    ".localhost",
    ".home",
    ".home.arpa",
)
_IPV4_NUMBER_COMPONENT = re.compile(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)")
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


def _looks_like_noncanonical_ipv4(host: str) -> bool:
    """Recognize browser/DNS numeric IPv4 spellings that ipaddress rejects."""

    components = host.split(".")
    return 1 <= len(components) <= 4 and all(
        _IPV4_NUMBER_COMPONENT.fullmatch(component) is not None
        for component in components
    )


def _fetch_policy(host: str) -> FetchPolicy:
    comparable = host.casefold().rstrip(".")
    if comparable in {"localhost", "home.arpa"} or comparable.endswith(
        _LOCAL_HOST_SUFFIXES
    ):
        return FetchPolicy.EXPORT_METADATA_ONLY

    address_text = comparable.partition("%")[0]
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError:
        if _looks_like_noncanonical_ipv4(address_text):
            return FetchPolicy.EXPORT_METADATA_ONLY
        return FetchPolicy.PUBLIC_REVALIDATION_REQUIRED
    return (
        FetchPolicy.PUBLIC_REVALIDATION_REQUIRED
        if address.is_global and not address.is_multicast
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
    if "\\" in candidate:
        # Browsers treat backslashes as path separators for special schemes,
        # while urllib may leave them in the authority. Reject the parser
        # differential instead of allowing a public-looking host to become a
        # private navigation target at click or fetch time.
        return _result(NormalizationStatus.INVALID, reason="backslash_in_url")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in candidate
    ):
        return _result(NormalizationStatus.INVALID, reason="control_character_in_url")

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return _result(NormalizationStatus.INVALID, reason="malformed_url")
    if "%" in parts.netloc:
        # WHATWG parsers percent-decode special-scheme hostnames before
        # navigation. urllib does not, which can hide localhost or an IP
        # literal from the fetch policy.
        return _result(NormalizationStatus.INVALID, reason="encoded_authority")

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
