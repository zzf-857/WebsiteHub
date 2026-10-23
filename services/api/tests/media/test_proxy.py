"""Q56 图片代理：出站安全、MIME/魔数、大小上限、SVG 策略与磁盘缓存。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from ipaddress import IPv4Address
from pathlib import Path
from urllib.parse import urldefrag, urlsplit

import httpx
import pytest

from webhub.ingestion import fetcher as ingestion_fetcher
from webhub.media import proxy as media_proxy
from webhub.media.proxy import (
    MAX_IMAGE_BYTES,
    MAX_SVG_BYTES,
    FetchedImage,
    MediaProxy,
    MediaProxyError,
    cache_key,
    fetch_image,
    normalize_image_url,
    svg_is_safe,
    validated_image,
)
from webhub.providers.targets import ProviderTargetError, ResolvedConnectionTarget

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_PINNED_ADDRESS = IPv4Address("93.184.216.34")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><path d="M0 0h1v1z"/></svg>'


def _install_transport(
    handler: Callable[[httpx.Request], httpx.Response],
    monkeypatch: pytest.MonkeyPatch,
    *,
    skip_target_check: bool = True,
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(record)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    if skip_target_check:

        async def allow(value: str, **_kwargs: object) -> ResolvedConnectionTarget:
            logical_url = urldefrag(value.strip()).url
            parsed = urlsplit(logical_url)
            assert parsed.hostname is not None
            if parsed.hostname.endswith(".internal-test"):
                raise ProviderTargetError("unsafe_provider_target", "private")
            return ResolvedConnectionTarget(
                url=logical_url,
                hostname=parsed.hostname,
                port=parsed.port or (443 if parsed.scheme == "https" else 80),
                addresses=(_PINNED_ADDRESS,),
            )

        monkeypatch.setattr(ingestion_fetcher, "resolve_resource_target", allow)
    return seen


def _image(body: bytes, content_type: str = "image/png", **headers: str):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": content_type, **headers})

    return handler


def _fetch(url: str) -> FetchedImage:
    return asyncio.run(fetch_image(url))


def _fetch_error(url: str) -> str:
    with pytest.raises(MediaProxyError) as caught:
        _fetch(url)
    return caught.value.code


# ------------------------------------------------------------------ URL rules


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "/relative/icon.png",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "file:///etc/passwd",
        "ftp://example.com/a.png",
        "https://user:pass@example.com/a.png",
        "https://example.com/a b.png",
        "https:///nohost.png",
        "https://example.com:99999/a.png",
        "https://example.com/" + "a" * 5000,
    ],
)
def test_invalid_urls_are_refused_before_any_network(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _install_transport(_image(PNG), monkeypatch)
    assert _fetch_error(value) == "invalid_url"
    assert seen == []


def test_normalized_url_drops_the_fragment_and_keys_are_stable() -> None:
    assert normalize_image_url(" https://example.com/a.png#x ") == "https://example.com/a.png"
    assert cache_key(normalize_image_url("https://example.com/a.png#x")) == cache_key(
        "https://example.com/a.png"
    )


# ------------------------------------------------------------------ SSRF


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.png",
        "http://10.0.0.5/a.png",
        "http://192.168.1.1/a.png",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/a.png",
        "http://localhost/a.png",
    ],
)
def test_private_targets_never_leave_the_process(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _install_transport(_image(PNG), monkeypatch, skip_target_check=False)
    assert _fetch_error(url) == "unsafe_target"
    assert seen == []


def test_a_redirect_into_a_private_address_is_refused_for_real(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def resolve(hostname: str, _port: int) -> set[IPv4Address]:
        assert hostname == "cdn.example"
        return {_PINNED_ADDRESS}

    seen = _install_transport(
        lambda _r: httpx.Response(302, headers={"location": "http://169.254.169.254/x.png"}),
        monkeypatch,
        skip_target_check=False,
    )
    monkeypatch.setattr("webhub.providers.targets._resolve", resolve)
    assert _fetch_error("https://cdn.example/a.png") == "unsafe_target"
    assert len(seen) == 1


def test_a_redirect_to_a_hostname_with_a_private_answer_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.internal-test/a.png"})

    seen = _install_transport(handler, monkeypatch)
    assert _fetch_error("https://cdn.example/a.png") == "unsafe_target"
    assert len(seen) == 1


def test_redirects_are_followed_with_pinned_ip_host_and_no_browser_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "short.example":
            return httpx.Response(
                302,
                headers={"location": "https://cdn.example/final.png", "set-cookie": "s=1"},
            )
        return _image(PNG)(request)

    seen = _install_transport(handler, monkeypatch)
    image = _fetch("https://short.example/a.png")
    assert image.content_type == "image/png"
    assert [request.headers["host"] for request in seen] == ["short.example", "cdn.example"]
    assert {request.url.host for request in seen} == {str(_PINNED_ADDRESS)}
    for request in seen:
        assert "cookie" not in request.headers
        assert "referer" not in request.headers
        assert "authorization" not in request.headers


def test_a_redirect_to_a_non_http_scheme_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_transport(
        lambda _r: httpx.Response(302, headers={"location": "file:///etc/passwd"}),
        monkeypatch,
    )
    assert _fetch_error("https://cdn.example/a.png") == "unsafe_target"


def test_redirect_loops_stop_at_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_transport(
        lambda _r: httpx.Response(302, headers={"location": "/again.png"}),
        monkeypatch,
    )
    assert _fetch_error("https://cdn.example/a.png") == "upstream_error"
    assert len(seen) == media_proxy.MAX_REDIRECTS + 1


def test_proxy_fake_ip_is_recovered_through_independent_public_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_dns_checks: list[str] = []

    async def fake_ip(_value: str, **_kwargs: object) -> ResolvedConnectionTarget:
        raise ProviderTargetError("resource_fake_ip_detected", "fake ip")

    async def public_dns(hostname: str, **_kwargs: object) -> tuple[IPv4Address, ...]:
        public_dns_checks.append(hostname)
        return (_PINNED_ADDRESS,)

    seen = _install_transport(_image(PNG), monkeypatch, skip_target_check=False)
    monkeypatch.setattr(ingestion_fetcher, "resolve_resource_target", fake_ip)
    monkeypatch.setattr(ingestion_fetcher, "resolve_public_hostname", public_dns)
    assert _fetch("https://img.example/a.png").content_type == "image/png"
    assert public_dns_checks == ["img.example"]
    assert seen[0].url.host == str(_PINNED_ADDRESS)


def test_proxy_fake_ip_with_a_private_real_answer_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_ip(_value: str, **_kwargs: object) -> ResolvedConnectionTarget:
        raise ProviderTargetError("resource_fake_ip_detected", "fake ip")

    async def private_dns(_hostname: str, **_kwargs: object) -> tuple[IPv4Address, ...]:
        return (IPv4Address("10.0.0.8"),)

    seen = _install_transport(_image(PNG), monkeypatch, skip_target_check=False)
    monkeypatch.setattr(ingestion_fetcher, "resolve_resource_target", fake_ip)
    monkeypatch.setattr(ingestion_fetcher, "resolve_public_hostname", private_dns)
    assert _fetch_error("https://img.example/a.png") == "unsafe_target"
    assert seen == []


# ------------------------------------------------------------------ MIME / magic / size


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        (PNG, "image/png", "image/png"),
        (JPEG, "image/jpg", "image/jpeg"),
        (b"GIF89a" + b"\x00" * 8, "image/gif", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp", "image/webp"),
        (b"\x00\x00\x01\x00" + b"\x00" * 8, "image/vnd.microsoft.icon", "image/x-icon"),
        (PNG, "application/octet-stream", "image/png"),
        (PNG, "", "image/png"),
        (SVG, "image/svg+xml; charset=utf-8", "image/svg+xml"),
    ],
)
def test_images_are_served_with_their_detected_type(
    body: bytes, content_type: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(_image(body, content_type), monkeypatch)
    image = _fetch("https://cdn.example/a")
    assert image.content_type == expected
    assert image.body == body


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"<html>hi</html>", "text/html"),
        (PNG, "text/html"),
        (b"<html>not a png</html>", "image/png"),
        (JPEG, "image/png"),
        (b"plain bytes", "application/octet-stream"),
        (b"", "image/png"),
    ],
)
def test_mime_and_magic_mismatches_are_refused(
    body: bytes, content_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(_image(body, content_type), monkeypatch)
    assert _fetch_error("https://cdn.example/a.png") == "not_image"


def test_non_2xx_upstream_is_an_upstream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_transport(lambda _r: httpx.Response(404, content=PNG), monkeypatch)
    assert _fetch_error("https://cdn.example/a.png") == "upstream_error"


def test_a_declared_oversize_body_is_refused_before_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_transport(
        _image(PNG, "image/png", **{"content-length": str(MAX_IMAGE_BYTES + 1)}),
        monkeypatch,
    )
    assert _fetch_error("https://cdn.example/a.png") == "too_large"


def test_a_streamed_body_over_the_cap_is_cut_off(monkeypatch: pytest.MonkeyPatch) -> None:
    async def stream():
        yield PNG
        for _ in range(MAX_IMAGE_BYTES // 65_536 + 2):
            yield b"\x00" * 65_536

    _install_transport(
        lambda _r: httpx.Response(200, content=stream(), headers={"content-type": "image/png"}),
        monkeypatch,
    )
    assert _fetch_error("https://cdn.example/a.png") == "too_large"


def test_a_body_at_the_cap_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    body = PNG + b"\x00" * (MAX_IMAGE_BYTES - len(PNG))
    _install_transport(_image(body), monkeypatch)
    assert len(_fetch("https://cdn.example/a.png").body) == MAX_IMAGE_BYTES


def test_svg_has_a_smaller_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    body = SVG[:-6] + b"<!--" + b" " * MAX_SVG_BYTES + b"--></svg>"
    _install_transport(_image(body, "image/svg+xml"), monkeypatch)
    assert _fetch_error("https://cdn.example/a.svg") == "too_large"


# ------------------------------------------------------------------ SVG policy


def test_a_plain_svg_icon_is_safe() -> None:
    assert svg_is_safe(SVG)
    assert svg_is_safe(
        b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" '
        b'xmlns:xlink="http://www.w3.org/1999/xlink"><defs><linearGradient id="g"/></defs>'
        b'<rect fill="url(#g)" width="1" height="1"/><use xlink:href="#g"/></svg>'
    )


@pytest.mark.parametrize(
    "body",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><svg:script>x</svg:script></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><rect ONCLICK = "x"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><p/></foreignObject></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><a href="https://evil.example">x</a></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://t.example/p.png"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><use xlink:href="http://x/s.svg#a"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:url(http://x/a)"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import "http://x";</style></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><set attributeName="href" to="#a"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><image href="data:image/png;base64,A"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><text>&#106;avascript</text></svg>',
        b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x "y">]><svg>&x;</svg>',
        b'<?xml version="1.0"?><?xml-stylesheet href="http://x/a.css"?><svg></svg>',
        b"<svg>\xff\xfe</svg>",
        b"<html><svg></svg></html>",
        b"",
    ],
)
def test_unsafe_svg_is_rejected(body: bytes) -> None:
    assert not svg_is_safe(body)
    with pytest.raises(MediaProxyError) as caught:
        validated_image("image/svg+xml", body)
    assert caught.value.code in {"unsafe_svg", "not_image"}


@pytest.mark.parametrize("content_type", ["application/octet-stream", "", "image/*", "text/xml"])
def test_svg_without_an_explicit_svg_type_is_rejected(
    content_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(_image(SVG, content_type), monkeypatch)
    assert _fetch_error("https://cdn.example/a") in {"unsafe_svg", "not_image"}


def test_svg_with_a_raster_declared_type_is_rejected() -> None:
    with pytest.raises(MediaProxyError):
        validated_image("image/png", SVG)


# ------------------------------------------------------------------ cache


class _Clock:
    def __init__(self) -> None:
        self.value = 1_000_000.0

    def __call__(self) -> float:
        return self.value


class _Fetcher:
    def __init__(self, *results: FetchedImage | MediaProxyError) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    async def __call__(self, url: str) -> FetchedImage:
        self.calls.append(url)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, MediaProxyError):
            raise result
        return result


def _proxy(tmp_path: Path, fetcher: _Fetcher, clock: _Clock, **kwargs: int) -> MediaProxy:
    return MediaProxy(
        tmp_path / "media-cache",
        fetcher=fetcher,
        clock=clock,
        success_ttl_seconds=100,
        failure_cooldown_seconds=30,
        **kwargs,
    )


def test_cache_hit_avoids_the_network_and_survives_a_new_process(tmp_path: Path) -> None:
    clock = _Clock()
    fetcher = _Fetcher(FetchedImage(PNG, "image/png"))
    proxy = _proxy(tmp_path, fetcher, clock)

    first = asyncio.run(proxy.get("https://cdn.example/a.png"))
    second = asyncio.run(proxy.get("https://cdn.example/a.png#frag"))
    assert first.body == second.body == PNG
    assert first.etag == second.etag
    assert fetcher.calls == ["https://cdn.example/a.png"]

    reopened = _proxy(tmp_path, _Fetcher(MediaProxyError("upstream_error")), clock)
    assert asyncio.run(reopened.get("https://cdn.example/a.png")).body == PNG
    files = list((tmp_path / "media-cache").glob("*/*.img"))
    assert [path.stem for path in files] == [cache_key("https://cdn.example/a.png")]


def test_expired_entries_are_refetched(tmp_path: Path) -> None:
    clock = _Clock()
    fetcher = _Fetcher(FetchedImage(PNG, "image/png"), FetchedImage(JPEG, "image/jpeg"))
    proxy = _proxy(tmp_path, fetcher, clock)
    asyncio.run(proxy.get("https://cdn.example/a"))
    clock.value += 101
    refreshed = asyncio.run(proxy.get("https://cdn.example/a"))
    assert refreshed.content_type == "image/jpeg"
    assert len(fetcher.calls) == 2


def test_failures_cool_down_then_retry(tmp_path: Path) -> None:
    clock = _Clock()
    fetcher = _Fetcher(MediaProxyError("not_image"), FetchedImage(PNG, "image/png"))
    proxy = _proxy(tmp_path, fetcher, clock)

    for _ in range(3):
        with pytest.raises(MediaProxyError) as caught:
            asyncio.run(proxy.get("https://cdn.example/a"))
        assert caught.value.code == "not_image"
        assert caught.value.retry_after is not None
    assert len(fetcher.calls) == 1

    clock.value += 31
    assert asyncio.run(proxy.get("https://cdn.example/a")).body == PNG
    assert len(fetcher.calls) == 2


def test_an_expired_entry_is_served_stale_when_the_refresh_fails(tmp_path: Path) -> None:
    clock = _Clock()
    fetcher = _Fetcher(FetchedImage(PNG, "image/png"), MediaProxyError("upstream_error"))
    proxy = _proxy(tmp_path, fetcher, clock)
    asyncio.run(proxy.get("https://cdn.example/a"))
    clock.value += 101
    stale = asyncio.run(proxy.get("https://cdn.example/a"))
    assert stale.stale and stale.body == PNG
    # During the cool-down the stale copy is served without another attempt.
    assert asyncio.run(proxy.get("https://cdn.example/a")).stale
    assert len(fetcher.calls) == 2


def test_unexpected_fetcher_bugs_become_a_cooled_upstream_error(tmp_path: Path) -> None:
    class Broken:
        calls = 0

        async def __call__(self, _url: str) -> FetchedImage:
            Broken.calls += 1
            raise RuntimeError("secret vendor text")

    proxy = MediaProxy(tmp_path / "c", fetcher=Broken(), clock=_Clock())
    for _ in range(2):
        with pytest.raises(MediaProxyError) as caught:
            asyncio.run(proxy.get("https://cdn.example/a"))
        assert caught.value.code == "upstream_error"
        assert "secret" not in str(caught.value)
    assert Broken.calls == 1


def test_a_tampered_cache_file_is_discarded_not_served(tmp_path: Path) -> None:
    clock = _Clock()
    fetcher = _Fetcher(FetchedImage(PNG, "image/png"))
    proxy = _proxy(tmp_path, fetcher, clock)
    asyncio.run(proxy.get("https://cdn.example/a"))
    [path] = list((tmp_path / "media-cache").glob("*/*.img"))
    header, _, _body = path.read_bytes().partition(b"\n")
    path.write_bytes(header + b"\n<html><script>alert(1)</script></html>")
    assert asyncio.run(proxy.get("https://cdn.example/a")).body == PNG
    assert len(fetcher.calls) == 2


def test_a_custom_fetcher_result_is_revalidated(tmp_path: Path) -> None:
    proxy = _proxy(tmp_path, _Fetcher(FetchedImage(b"<html/>", "image/png")), _Clock())
    with pytest.raises(MediaProxyError) as caught:
        asyncio.run(proxy.get("https://cdn.example/a"))
    assert caught.value.code == "not_image"


def test_concurrent_requests_for_one_url_share_a_single_fetch(tmp_path: Path) -> None:
    class Slow:
        calls = 0

        async def __call__(self, _url: str) -> FetchedImage:
            Slow.calls += 1
            await asyncio.sleep(0.02)
            return FetchedImage(PNG, "image/png")

    proxy = MediaProxy(tmp_path / "c", fetcher=Slow(), clock=_Clock())

    async def run() -> None:
        results = await asyncio.gather(*(proxy.get("https://cdn.example/a") for _ in range(5)))
        assert all(result.body == PNG for result in results)

    asyncio.run(run())
    assert Slow.calls == 1


def test_prune_keeps_the_cache_under_its_byte_cap(tmp_path: Path) -> None:
    clock = _Clock()
    body = PNG + b"\x00" * 2_000
    fetcher = _Fetcher(FetchedImage(body, "image/png"))
    proxy = _proxy(tmp_path, fetcher, clock, max_cache_bytes=5_000)
    for index in range(4):
        asyncio.run(proxy.get(f"https://cdn.example/{index}.png"))
    proxy.cache.prune(clock())
    files = list((tmp_path / "media-cache").glob("*/*.img"))
    assert 0 < len(files) <= 2
    assert sum(path.stat().st_size for path in files) <= 5_000


def test_a_write_burst_triggers_pruning_before_the_timer(tmp_path: Path) -> None:
    clock = _Clock()
    body = PNG + b"\x00" * 2_000
    fetcher = _Fetcher(FetchedImage(body, "image/png"))
    proxy = _proxy(tmp_path, fetcher, clock, max_cache_bytes=5_000)
    # The clock never advances, so only the written-bytes trigger can prune.
    for index in range(10):
        asyncio.run(proxy.get(f"https://cdn.example/{index}.png"))
    files = list((tmp_path / "media-cache").glob("*/*.img"))
    assert sum(path.stat().st_size for path in files) <= 5_000 + len(body) + 200
