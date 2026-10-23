"""``GET /api/media/proxy?url=…``: remote images served from WebHub's origin.

Requires the cookie session like every other read.  The browser never talks to
the image host; see ``media.proxy`` for the fetch and cache rules.
"""

from __future__ import annotations

import math
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from webhub.auth.dependencies import SettingsDependency, require_current_identity
from webhub.db.database import Database
from webhub.media.proxy import (
    BROWSER_MAX_AGE_SECONDS,
    MAX_URL_LENGTH,
    STALE_BROWSER_MAX_AGE_SECONDS,
    MediaProxy,
    MediaProxyError,
    ProxiedImage,
)

router = APIRouter(tags=["media"])


async def _require_login(request: Request, settings: SettingsDependency) -> None:
    """The usual cookie-session check, in a session closed before the fetch.

    ``CurrentIdentityDependency`` keeps its database session open until the
    response is sent.  A page full of cold favicons would then park one pooled
    connection per image for the whole upstream fetch and starve every other
    API request; an image only needs to know the caller is signed in.
    """

    database: Database = request.app.state.database
    async with database.sessions() as session:
        await require_current_identity(request, settings, session)


LoginDependency = Annotated[None, Depends(_require_login)]

# Applied to every proxied image.  ``sandbox`` + ``default-src 'none'`` keep
# even a directly opened SVG inert on WebHub's origin.
_SECURITY_HEADERS = {
    "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "x-content-type-options": "nosniff",
    "cross-origin-resource-policy": "same-origin",
    "referrer-policy": "no-referrer",
}

_FAILURES: dict[str, tuple[int, str]] = {
    "invalid_url": (status.HTTP_400_BAD_REQUEST, "图片地址无效"),
    "unsafe_target": (status.HTTP_403_FORBIDDEN, "该图片地址指向不允许访问的目标"),
    "not_image": (status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "该地址返回的不是受支持的图片"),
    "unsafe_svg": (status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "该 SVG 图片包含不允许的内容"),
    "too_large": (status.HTTP_413_CONTENT_TOO_LARGE, "图片过大，已停止读取"),
    "upstream_error": (status.HTTP_502_BAD_GATEWAY, "暂时无法获取该图片"),
}


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    for candidate in header.split(","):
        value = candidate.strip()
        if value == "*" or value.removeprefix("W/") == etag:
            return True
    return False


def _cache_control(image: ProxiedImage, now: float) -> str:
    if image.stale:
        max_age = STALE_BROWSER_MAX_AGE_SECONDS
    else:
        remaining = math.floor(image.expires_at - now)
        max_age = max(STALE_BROWSER_MAX_AGE_SECONDS, min(BROWSER_MAX_AGE_SECONDS, remaining))
    # private: the response sits behind a login, so shared caches must not keep it.
    return f"private, max-age={max_age}"


@router.get("/media/proxy", response_class=Response)
async def proxy_image(
    request: Request,
    _login: LoginDependency,
    url: Annotated[str, Query(min_length=1, max_length=MAX_URL_LENGTH)],
) -> Response:
    proxy: MediaProxy = request.app.state.media_proxy
    try:
        image = await proxy.get(url)
    except MediaProxyError as error:
        status_code, detail = _FAILURES[error.code]
        headers = {"cache-control": "no-store"}
        if error.retry_after is not None:
            headers["retry-after"] = str(error.retry_after)
        raise HTTPException(status_code, detail, headers=headers) from None

    headers = {
        **_SECURITY_HEADERS,
        "etag": image.etag,
        "cache-control": _cache_control(image, proxy.now()),
    }
    if image.content_type == "image/svg+xml":
        # Opening the URL directly downloads instead of rendering; <img> ignores it.
        headers["content-disposition"] = 'attachment; filename="image.svg"'
    if _etag_matches(request.headers.get("if-none-match"), image.etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(content=image.body, media_type=image.content_type, headers=headers)
