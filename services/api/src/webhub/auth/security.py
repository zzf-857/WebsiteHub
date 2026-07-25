from __future__ import annotations

import hashlib
import ipaddress
import secrets
import unicodedata
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from webhub.config import Settings


def normalize_username(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if not 3 <= len(normalized) <= 32:
        raise ValueError("用户名长度必须为 3 到 32 个字符")
    if not normalized[0].isalnum() or not normalized[-1].isalnum():
        raise ValueError("用户名必须以字母或数字开头和结尾")
    if any(not (character.isalnum() or character in {"_", "-", "."}) for character in normalized):
        raise ValueError("用户名只能包含字母、数字、点、下划线和连字符")
    return normalized


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def rate_limit_key(*parts: str) -> str:
    payload = "\x1f".join(part.casefold() for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ip_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.partition("%")[0])
    except ValueError:
        return None


def _request_from_loopback(request: Request) -> bool:
    client_host = request.client.host if request.client else ""
    address = _ip_address(client_host)
    return bool(address and address.is_loopback)


def _loopback_forwarded_host(request: Request) -> str | None:
    if not _request_from_loopback(request):
        return None

    forwarded_host = request.headers.get("x-forwarded-host", "").partition(",")[0].strip()
    if not forwarded_host or any(character.isspace() for character in forwarded_host):
        return None
    return forwarded_host.casefold()


def rate_limit_client_host(request: Request) -> str:
    client_host = request.client.host if request.client else "unknown"
    if not _request_from_loopback(request):
        return client_host

    forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    if not forwarded_for or "," in forwarded_for or any(
        character.isspace() for character in forwarded_for
    ):
        return client_host
    forwarded_address = _ip_address(forwarded_for)
    return forwarded_address.compressed if forwarded_address else client_host


def validate_request_origin(request: Request, settings: Settings) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return

    origin = request.headers.get("origin")
    has_session = settings.session_cookie_name in request.cookies
    if not origin:
        if has_session:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "缺少请求来源信息")
        return

    try:
        parsed = urlsplit(origin)
    except ValueError as error:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "请求来源不受信任",
        ) from error
    normalized_origin = f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"
    request_host = request.headers.get("host", "").casefold()
    origin_host = parsed.netloc.casefold()
    valid_origin = (
        parsed.scheme in {"http", "https"}
        and bool(origin_host)
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )
    trusted_request_hosts = {request_host}
    if forwarded_host := _loopback_forwarded_host(request):
        trusted_request_hosts.add(forwarded_host)
    if valid_origin and origin_host in trusted_request_hosts:
        return
    if valid_origin and normalized_origin.rstrip("/") in settings.allowed_origins:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "请求来源不受信任")
