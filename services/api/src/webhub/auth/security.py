from __future__ import annotations

import hashlib
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


def validate_request_origin(request: Request, settings: Settings) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return

    origin = request.headers.get("origin")
    has_session = settings.session_cookie_name in request.cookies
    if not origin:
        if has_session:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "缺少请求来源信息")
        return

    parsed = urlsplit(origin)
    normalized_origin = f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"
    request_host = request.headers.get("host", "").casefold()
    if parsed.scheme in {"http", "https"} and parsed.netloc.casefold() == request_host:
        return
    if normalized_origin.rstrip("/") in settings.allowed_origins:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "请求来源不受信任")
