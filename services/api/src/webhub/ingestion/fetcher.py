"""Fetch one user-supplied URL safely enough to read its metadata.

Every other outbound call in WebHub targets an address we control (a vendor's
API).  This one targets whatever the *user* pasted, which makes it the natural
home for a server-side request forgery.  The defences, in the order they run:

* **Every hop is validated, not just the first.**  Redirects are followed
  manually — ``follow_redirects=False`` — and each new location goes through
  ``validate_connection_target`` again.  Following redirects automatically
  would mean one ``302`` to ``169.254.169.254`` walks straight past the check.
* **DNS is re-resolved at each hop**, because a name that resolved publicly a
  moment ago can be re-pointed at a private address (DNS rebinding).
* **Only ``text/html`` is read.**  A wrong content type is reported as
  ``limited``, not parsed and not guessed at.
* **The body is capped and streamed**, so a hostile endpoint cannot stream
  gigabytes into memory.
* **Nothing is executed.**  No JS engine, no headless browser — a page that
  only renders client-side simply yields no metadata, which is reported
  honestly as ``limited``.

Failure never raises to the caller: a site that cannot be analysed must still
be storable.  Outcomes map onto the ``analysis_status`` column.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from webhub.providers.targets import ProviderTargetError, validate_connection_target

from .metadata import parse_metadata

MAX_REDIRECTS = 3
MAX_BODY_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 8
_USER_AGENT = "WebHub/0.1 (+https://github.com/webhub)"

AnalysisStatus = Literal["complete", "failed", "limited"]


@dataclass(frozen=True, slots=True)
class SiteMetadata:
    title: str | None = None
    description: str | None = None
    image_url: str | None = None
    icon_url: str | None = None
    related_urls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """One analysis attempt, already reduced to what the column stores."""

    status: AnalysisStatus
    reason: str
    metadata: SiteMetadata = SiteMetadata()
    final_url: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"


# Every failure the user can see, in Chinese, composed here rather than taken
# from the remote server.  A fetched page's own error text is untrusted content.
_REASONS: dict[str, str] = {
    "unsafe_target": "该地址指向本机、内网或其他不允许访问的目标，已拒绝访问",
    "unreachable": "无法连接到该网站",
    "timeout": "访问该网站超时",
    "too_many_redirects": "跳转次数过多，已停止跟随",
    "http_error": "该网站返回了错误状态，无法读取内容",
    "not_html": "该地址返回的不是网页内容，已跳过分析",
    "too_large": "网页内容过大，已停止读取",
    "no_metadata": "该网页没有可提取的标题或描述（可能需要执行脚本才能渲染）",
    "ok": "已提取网页元数据",
}


def _reason(code: str) -> str:
    return _REASONS.get(code, "分析失败")


def _outcome(status: AnalysisStatus, code: str) -> FetchOutcome:
    return FetchOutcome(status=status, reason=_reason(code))


async def _decoded(body: bytes, content_type: str) -> str:
    charset = ""
    for part in content_type.split(";"):
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "charset":
            charset = value.strip().strip('"').lower()
            break
    for encoding in (charset, "utf-8", "gb18030", "latin-1"):
        if not encoding:
            continue
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


async def fetch_site_metadata(
    url: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    allow_private: bool = False,
) -> FetchOutcome:
    """Fetch ``url`` and read its metadata, never raising.

    ``allow_private`` exists for tests and for a future "analyse my intranet
    wiki" opt-in; it defaults to off, and the default is what every caller in
    the product uses today.
    """

    import httpx

    current = url.strip()
    if not current.lower().startswith(("http://", "https://")):
        return _outcome("failed", "unsafe_target")

    try:
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={
                "user-agent": _USER_AGENT,
                "accept": "text/html,application/xhtml+xml",
                # Some CDNs serve brotli/zstd only; httpx handles gzip/deflate
                # without extra dependencies, so ask for what we can decode.
                "accept-encoding": "gzip, deflate",
            },
        ) as client:
            for _hop in range(MAX_REDIRECTS + 1):
                try:
                    # Re-validated on *every* hop: the first URL being safe says
                    # nothing about where a redirect points.
                    await validate_connection_target(
                        current,
                        allow_private=allow_private,
                        timeout_seconds=timeout_seconds,
                    )
                except ProviderTargetError:
                    return _outcome("failed", "unsafe_target")

                async with client.stream("GET", current) as response:
                    if 300 <= response.status_code < 400:
                        location = response.headers.get("location", "").strip()
                        if not location:
                            return _outcome("failed", "http_error")
                        current = str(response.url.join(location))
                        if not current.lower().startswith(("http://", "https://")):
                            return _outcome("failed", "unsafe_target")
                        continue
                    if response.status_code != 200:
                        return _outcome("failed", "http_error")

                    content_type = response.headers.get("content-type", "")
                    if "text/html" not in content_type.lower() and (
                        "application/xhtml" not in content_type.lower()
                    ):
                        # Trust the declared type only; sniffing would mean
                        # parsing bytes the server said were not HTML.
                        return _outcome("limited", "not_html")

                    body = bytearray()
                    truncated = False
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) >= MAX_BODY_BYTES:
                            truncated = True
                            break

                final_url = current
                html = await _decoded(bytes(body), content_type)
                parsed = parse_metadata(html, base_url=final_url)
                metadata = SiteMetadata(
                    title=parsed.best_title,
                    description=parsed.description,
                    image_url=parsed.image_url,
                    icon_url=parsed.icon_href,
                    related_urls=tuple(parsed.github_links),
                )
                if metadata.title is None and metadata.description is None:
                    # Truncation is the more useful explanation when both apply.
                    code = "too_large" if truncated else "no_metadata"
                    return FetchOutcome(
                        status="limited",
                        reason=_reason(code),
                        metadata=metadata,
                        final_url=final_url,
                    )
                return FetchOutcome(
                    status="complete",
                    reason=_reason("ok"),
                    metadata=metadata,
                    final_url=final_url,
                )

            return _outcome("failed", "too_many_redirects")
    except httpx.TimeoutException:
        return _outcome("failed", "timeout")
    except httpx.TransportError:
        return _outcome("failed", "unreachable")
    except Exception:  # noqa: BLE001 - a bad page must never break saving a site
        return _outcome("failed", "unreachable")


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_BODY_BYTES",
    "MAX_REDIRECTS",
    "AnalysisStatus",
    "FetchOutcome",
    "SiteMetadata",
    "fetch_site_metadata",
]
