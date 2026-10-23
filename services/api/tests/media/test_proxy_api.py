"""Q56 图片代理 HTTP 面：登录要求、响应头、ETag 与不转发 Cookie。"""

from __future__ import annotations

from collections.abc import Iterator
from ipaddress import IPv4Address
from pathlib import Path
from urllib.parse import urldefrag, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.migrations import upgrade_database
from webhub.ingestion import fetcher as ingestion_fetcher
from webhub.ingestion.worker import AnalysisSchedule
from webhub.main import create_app
from webhub.media.proxy import FetchedImage, MediaProxy, MediaProxyError
from webhub.providers.targets import ResolvedConnectionTarget

COOKIE_NAME = "webhub_session"
ORIGIN = {"Origin": "http://testserver"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1z"/></svg>'
PROXY = "/api/media/proxy"


@pytest.fixture(autouse=True)
def _no_background_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "webhub.ingestion.worker.schedule_analysis",
        lambda *_args, **_kwargs: AnalysisSchedule(queued=0, already_queued=0, rejected=0),
    )
    monkeypatch.setattr(
        "webhub.ingestion.worker.ensure_auto_backfill",
        lambda *_args, **_kwargs: False,
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'main.sqlite3').as_posix()}"
    settings = Settings(environment="test", database_url=database_url, data_directory=tmp_path)
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/register",
        json={"username": "media-user", "password": "a sufficiently secure password"},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text


class _Fetcher:
    def __init__(self, result: FetchedImage | MediaProxyError) -> None:
        self.result = result
        self.calls: list[str] = []

    async def __call__(self, url: str) -> FetchedImage:
        self.calls.append(url)
        if isinstance(self.result, MediaProxyError):
            raise self.result
        return self.result


def _use_fetcher(client: TestClient, tmp_path: Path, fetcher: _Fetcher) -> None:
    client.app.state.media_proxy = MediaProxy(tmp_path / "media-cache", fetcher=fetcher)


def test_the_proxy_requires_a_login_session(client: TestClient, tmp_path: Path) -> None:
    fetcher = _Fetcher(FetchedImage(PNG, "image/png"))
    _use_fetcher(client, tmp_path, fetcher)
    response = client.get(PROXY, params={"url": "https://cdn.example/a.png"})
    assert response.status_code == 401
    client.cookies.set(COOKIE_NAME, "not-a-real-session")
    assert client.get(PROXY, params={"url": "https://cdn.example/a.png"}).status_code == 401
    assert fetcher.calls == []


def test_an_image_is_served_with_private_caching_and_security_headers(
    client: TestClient, tmp_path: Path
) -> None:
    _login(client)
    fetcher = _Fetcher(FetchedImage(PNG, "image/png"))
    _use_fetcher(client, tmp_path, fetcher)

    response = client.get(PROXY, params={"url": "https://cdn.example/a.png"})
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"].startswith("private, max-age=")
    assert int(response.headers["cache-control"].rsplit("=", 1)[1]) <= 24 * 60 * 60
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert "set-cookie" not in response.headers
    etag = response.headers["etag"]

    cached = client.get(
        PROXY, params={"url": "https://cdn.example/a.png"}, headers={"if-none-match": etag}
    )
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers["etag"] == etag
    weak = client.get(
        PROXY,
        params={"url": "https://cdn.example/a.png"},
        headers={"if-none-match": f'"other", W/{etag}'},
    )
    assert weak.status_code == 304
    assert fetcher.calls == ["https://cdn.example/a.png"]


def test_svg_is_served_as_an_attachment(client: TestClient, tmp_path: Path) -> None:
    _login(client)
    _use_fetcher(client, tmp_path, _Fetcher(FetchedImage(SVG, "image/svg+xml")))
    response = client.get(PROXY, params={"url": "https://cdn.example/a.svg"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.headers["content-disposition"].startswith("attachment")


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("unsafe_target", 403),
        ("not_image", 415),
        ("unsafe_svg", 415),
        ("too_large", 413),
        ("upstream_error", 502),
    ],
)
def test_failures_map_to_fixed_statuses_without_caching(
    client: TestClient, tmp_path: Path, code: str, status: int
) -> None:
    _login(client)
    _use_fetcher(client, tmp_path, _Fetcher(MediaProxyError(code)))  # type: ignore[arg-type]
    response = client.get(PROXY, params={"url": "https://cdn.example/a.png"})
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert int(response.headers["retry-after"]) > 0


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "/api/auth/me", "https://u:p@cdn.example/a.png"],
)
def test_invalid_urls_are_a_bad_request(client: TestClient, tmp_path: Path, url: str) -> None:
    _login(client)
    fetcher = _Fetcher(FetchedImage(PNG, "image/png"))
    _use_fetcher(client, tmp_path, fetcher)
    assert client.get(PROXY, params={"url": url}).status_code == 400
    assert client.get(PROXY).status_code == 422
    assert client.get(PROXY, params={"url": "https://a.example/" + "x" * 5000}).status_code == 422
    assert fetcher.calls == []


def test_browser_cookies_never_reach_the_image_host(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client)
    seen: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            content=PNG,
            headers={"content-type": "image/png", "set-cookie": "tracker=1", "x-upstream": "1"},
        )

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    async def allow(value: str, **_kwargs: object) -> ResolvedConnectionTarget:
        logical = urldefrag(value.strip()).url
        parsed = urlsplit(logical)
        assert parsed.hostname is not None
        return ResolvedConnectionTarget(
            url=logical,
            hostname=parsed.hostname,
            port=parsed.port or 443,
            addresses=(IPv4Address("93.184.216.34"),),
        )

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(ingestion_fetcher, "resolve_resource_target", allow)

    response = client.get(
        PROXY,
        params={"url": "https://cdn.example/a.png"},
        headers={"referer": "http://testserver/library", "authorization": "Bearer x"},
    )
    assert response.status_code == 200, response.text
    assert response.content == PNG
    assert len(seen) == 1
    upstream = seen[0].headers
    assert "cookie" not in upstream
    assert "referer" not in upstream
    assert "authorization" not in upstream
    assert "set-cookie" not in response.headers
    assert "x-upstream" not in response.headers


def test_no_database_connection_is_held_while_the_image_is_fetched(
    client: TestClient, tmp_path: Path
) -> None:
    _login(client)
    checked_out: list[int] = []

    async def fetcher(_url: str) -> FetchedImage:
        checked_out.append(client.app.state.database.engine.pool.checkedout())
        return FetchedImage(PNG, "image/png")

    client.app.state.media_proxy = MediaProxy(tmp_path / "media-cache", fetcher=fetcher)
    response = client.get(PROXY, params={"url": "https://cdn.example/a.png"})
    assert response.status_code == 200
    assert checked_out == [0]
