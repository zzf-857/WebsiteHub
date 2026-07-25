from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

SENSITIVE_URL_RULESET_VERSION = "sensitive-url-keys.v2"

_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "auth_token",
    "awsaccesskeyid",
    "client_secret",
    "cookie",
    "credential",
    "googleaccessid",
    "id_token",
    "jwt",
    "password",
    "passwd",
    "private_token",
    "refresh_token",
    "samlresponse",
    "secret",
    "security_token",
    "session",
    "session_id",
    "sessionid",
    "session_token",
    "sig",
    "signature",
    "token",
}

_KEY_SEPARATOR_RE = re.compile(r"[-_.\[\]]+")
_SENSITIVE_KEY_FAMILY_RE = re.compile(
    r"(?:^|_)(?:(?:access|refresh|id|auth|session|security|private)_?token|"
    r"api_?key|client_?secret|credential|signature|sig|password|passwd|jwt)$"
)

_ABSOLUTE_URL_RE = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]{1,31}://|www\.)[^\s<>\"']+")
_NON_HIERARCHICAL_URI_RE = re.compile(
    r"(?i)(?:mailto|tel|data|javascript|about|chrome|edge):[^\s<>\"']+"
)
_BARE_URL_WITH_PATH_RE = re.compile(
    r"(?i)(?:[^\s<>\"'./?#:]+\.)+[^\s<>\"'./?#:]+"
    r"(?::\d{1,5})?[/\\?#][^\s<>\"']*"
)
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_LOCAL_PATH_RE = re.compile(r"(?i)(?:^|[\s(\[{'\"])(?:[a-z]:[\\/]|\\\\|/(?!/)[^\s]+)")
_QUERY_PAYLOAD_RE = re.compile(r"\?[^\s]*=[^\s]*")
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:access[-_]?token|api[-_]?key|apikey|authorization|credential|jwt|"
    r"password|passwd|secret|session(?:[-_]?id)?|sig(?:nature)?|token)\s*[:=]"
)
_AGENT_LABEL_MAX_CHARS = 256


def sensitive_url_keys(url: str) -> tuple[str, ...]:
    """Return only matched key names; never return or log their values."""
    parts = urlsplit(url)
    matched: set[str] = set()
    for payload in (parts.query, parts.fragment):
        for key, _ in parse_qsl(payload, keep_blank_values=True):
            normalized_key = _KEY_SEPARATOR_RE.sub("_", key.strip().casefold()).strip("_")
            if normalized_key in _SENSITIVE_KEYS or _SENSITIVE_KEY_FAMILY_RE.search(normalized_key):
                matched.add(normalized_key)
    return tuple(sorted(matched))


def agent_safe_label(value: str, *, max_chars: int = _AGENT_LABEL_MAX_CHARS) -> str | None:
    """Project untrusted labels into bounded text safe for external classification."""
    collapsed = " ".join(value.split())
    if not collapsed or max_chars < 1:
        return None

    sanitized = _HTML_TAG_RE.sub(" ", collapsed)
    sanitized = _ABSOLUTE_URL_RE.sub(" ", sanitized)
    sanitized = _NON_HIERARCHICAL_URI_RE.sub(" ", sanitized)
    sanitized = _BARE_URL_WITH_PATH_RE.sub(" ", sanitized)
    if (
        _LOCAL_PATH_RE.search(sanitized)
        or _SENSITIVE_ASSIGNMENT_RE.search(sanitized)
        or _QUERY_PAYLOAD_RE.search(sanitized)
    ):
        return None

    sanitized = " ".join(sanitized.split()).strip(" \t\r\n-:;,.()[]{}")
    if not sanitized:
        return None
    return sanitized[:max_chars].rstrip()
