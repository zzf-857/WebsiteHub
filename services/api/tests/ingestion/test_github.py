from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address
from urllib.parse import urldefrag, urlsplit

import httpx
import pytest

from webhub.ingestion import github
from webhub.ingestion.github import (
    GitHubCredential,
    GitHubFetchResult,
    GitHubRepoRef,
    GitHubRepositoryEnricher,
    github_meta_update,
    github_repo_ref,
    readme_excerpt,
    repository_evidence,
    stats_are_fresh,
    stored_etag,
)
from webhub.providers.targets import ResolvedConnectionTarget

_REAL_ASYNC_CLIENT = httpx.AsyncClient
REF = GitHubRepoRef(owner="langchain-ai", repo="langgraph")
NOW = datetime(2026, 10, 22, 8, 0, tzinfo=UTC)

REPOSITORY = {
    "full_name": "langchain-ai/langgraph",
    "stargazers_count": 12_345,
    "forks_count": 2_100,
    "language": "Python",
    "topics": ["agents", "llm", "Not A Topic", "agents"],
    "license": {"spdx_id": "MIT", "name": "MIT License"},
    "pushed_at": "2026-10-20T09:30:00Z",
    "archived": False,
    "description": "  Build resilient   language agents as graphs. ",
    "homepage": "https://langchain-ai.github.io/langgraph/",
    "owner": {"login": "langchain-ai", "token": "never-copied"},
}


@pytest.fixture(autouse=True)
def _fresh_backoff() -> Iterator[None]:
    github.reset_rate_limit_state()
    yield
    github.reset_rate_limit_state()


def _install(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(record)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    async def allow(value: str, **kwargs: object) -> ResolvedConnectionTarget:
        # The pinned public path never allows private targets.
        assert kwargs.get("allow_private") is False
        logical_url = urldefrag(value.strip()).url
        parsed = urlsplit(logical_url)
        assert parsed.hostname is not None
        return ResolvedConnectionTarget(
            url=logical_url,
            hostname=parsed.hostname,
            port=parsed.port or 443,
            addresses=(IPv4Address("140.82.112.6"),),
        )

    monkeypatch.setattr("webhub.ingestion.fetcher.resolve_resource_target", allow)
    return seen


def _fetch(
    *,
    credential: GitHubCredential | None = None,
    etag: str | None = None,
    include_stats: bool = True,
    include_readme: bool = False,
    ref: GitHubRepoRef = REF,
) -> GitHubFetchResult:
    return asyncio.run(
        GitHubRepositoryEnricher().fetch(
            ref,
            credential=credential,
            etag=etag,
            include_stats=include_stats,
            include_readme=include_readme,
        )
    )


def _json(payload: object, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload).encode(),
        headers={"content-type": "application/json", **headers},
    )


def _host(request: httpx.Request) -> str:
    return request.headers["host"]


def test_repository_fields_map_to_bounded_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install(monkeypatch, lambda _request: _json(REPOSITORY, etag='W/"abc123"'))

    result = _fetch()

    assert result.status == "ok"
    assert len(seen) == 1
    request = seen[0]
    # Pinned to the resolved address with the logical host kept.
    assert request.url.host == "140.82.112.6"
    assert _host(request) == "api.github.com"
    assert request.url.path == "/repos/langchain-ai/langgraph"
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["x-github-api-version"] == "2022-11-28"
    assert request.headers["user-agent"].startswith("WebHub/")
    assert "authorization" not in request.headers
    assert "if-none-match" not in request.headers

    meta = github_meta_update(result, None, NOW)
    assert meta == {
        "stars": 12_345,
        "forks": 2_100,
        "language": "Python",
        "topics": ["agents", "llm"],
        "license": "MIT",
        "pushed_at": "2026-10-20T09:30:00Z",
        "archived": False,
        "description": "Build resilient language agents as graphs.",
        "homepage": "https://langchain-ai.github.io/langgraph/",
        "fetched_at": "2026-10-22T08:00:00Z",
        "etag": 'W/"abc123"',
    }


def test_license_without_spdx_and_unsafe_homepage(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        **REPOSITORY,
        "license": {"spdx_id": "NOASSERTION", "name": "Other"},
        "homepage": "javascript:alert(1)",
        "language": None,
        "archived": True,
        "pushed_at": "not a date",
    }
    _install(monkeypatch, lambda _request: _json(payload))

    meta = github_meta_update(_fetch(), None, NOW)

    assert meta is not None
    assert meta["license"] == "Other"
    assert meta["homepage"] is None
    assert meta["language"] is None
    assert meta["archived"] is True
    assert meta["pushed_at"] is None
    assert meta["etag"] is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"{not json"),
        _json({"stargazers_count": "many", "forks_count": 1}),
        _json(["not", "an", "object"]),
        _json({}, status=500),
    ],
)
def test_unusable_responses_fail_without_meta(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
) -> None:
    _install(monkeypatch, lambda _request: response)

    result = _fetch(include_readme=True)

    assert result.status == "failed"
    assert github_meta_update(result, None, NOW) is None


def test_token_is_sent_only_as_a_bearer_header(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install(monkeypatch, lambda _request: _json(REPOSITORY))
    credential = GitHubCredential(config_id="config-1", token="github_pat_secret")

    assert _fetch(credential=credential).status == "ok"

    assert seen[0].headers["authorization"] == "Bearer github_pat_secret"
    assert "github_pat_secret" not in repr(credential)
    assert "github_pat_secret" not in str(seen[0].url)


def test_rejected_token_falls_back_to_one_anonymous_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "authorization" in request.headers:
            return httpx.Response(401, content=b'{"message": "Bad credentials"}')
        if request.url.path.endswith("/readme"):
            return httpx.Response(200, content=b"# LangGraph\n\nGraphs for agents.")
        return _json(REPOSITORY)

    seen = _install(monkeypatch, handler)

    result = _fetch(
        credential=GitHubCredential(config_id="expired", token="expired-token"),
        include_readme=True,
    )

    assert result.status == "ok"
    assert [("authorization" in request.headers) for request in seen] == [True, False, False]
    assert result.readme_excerpt == "# LangGraph\nGraphs for agents."


def test_etag_conditional_request_and_304_keep_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = github_meta_update(
        GitHubFetchResult(
            "ok",
            snapshot=github.parse_repository(REPOSITORY),
            etag='"etag-1"',
        ),
        None,
        NOW - timedelta(days=9),
    )
    assert previous is not None
    seen = _install(monkeypatch, lambda _request: httpx.Response(304))

    result = _fetch(etag=stored_etag(previous))

    assert seen[0].headers["if-none-match"] == '"etag-1"'
    assert result.status == "not_modified"
    updated = github_meta_update(result, previous, NOW)
    assert updated == {**previous, "fetched_at": "2026-10-22T08:00:00Z", "etag": '"etag-1"'}


def test_a_damaged_stored_etag_is_never_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install(monkeypatch, lambda _request: _json(REPOSITORY))

    _fetch(etag='"x"\r\nAuthorization: Bearer stolen')

    assert "if-none-match" not in seen[0].headers
    assert stored_etag({"stars": 1, "forks": 0, "etag": "unquoted"}) is None
    # Without stored stats a 304 would leave nothing to keep, so no ETag.
    assert stored_etag({"etag": '"orphan"'}) is None


def test_rate_limit_backs_off_and_skips_further_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset = int(time.time()) + 600
    seen = _install(
        monkeypatch,
        lambda _request: httpx.Response(
            403,
            content=b'{"message": "API rate limit exceeded for 1.2.3.4"}',
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
        ),
    )

    first = _fetch(include_readme=True)
    second = _fetch(include_readme=True)

    assert first.status == "rate_limited"
    assert second.status == "rate_limited"
    # The README is not attempted after the limit, and the second analysis
    # sends nothing at all while the window lasts.
    assert len(seen) == 1
    assert 500 < github.backoff_remaining(github.ANONYMOUS_CREDENTIAL_KEY) <= 600

    previous = {
        "stars": 10,
        "forks": 1,
        "language": "Go",
        "topics": [],
        "fetched_at": "2026-10-01T00:00:00Z",
        "etag": '"keep"',
    }
    marked = github_meta_update(first, previous, NOW)
    assert marked is not None
    assert marked["stars"] == 10
    assert marked["etag"] == '"keep"'
    assert marked["fetched_at"] == "2026-10-01T00:00:00Z"
    assert marked["rate_limited_at"] == "2026-10-22T08:00:00Z"
    assert github_meta_update(first, None, NOW) == {"rate_limited_at": "2026-10-22T08:00:00Z"}


def test_429_retry_after_backs_off_only_that_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "authorization" in request.headers:
            return _json(REPOSITORY, **{"x-ratelimit-remaining": "4999"})
        return httpx.Response(429, headers={"retry-after": "30"})

    seen = _install(monkeypatch, handler)

    assert _fetch().status == "rate_limited"
    assert 25 < github.backoff_remaining(github.ANONYMOUS_CREDENTIAL_KEY) <= 30
    # An account token has its own quota and is not held back.
    token = GitHubCredential(config_id="config-2", token="token")
    assert _fetch(credential=token).status == "ok"
    assert github.backoff_remaining(token.key) == 0
    assert len(seen) == 2


def test_secondary_limit_without_headers_waits_a_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, lambda _request: httpx.Response(403))

    assert _fetch().status == "rate_limited"
    assert 55 < github.backoff_remaining(github.ANONYMOUS_CREDENTIAL_KEY) <= 60


def test_last_request_of_the_window_starts_backoff_before_a_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset = int(time.time()) + 120
    seen = _install(
        monkeypatch,
        lambda _request: _json(
            REPOSITORY,
            **{"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
        ),
    )

    assert _fetch(include_readme=True).status == "ok"
    # The README would certainly be refused, so it is not sent.
    assert len(seen) == 1
    assert github.backoff_remaining(github.ANONYMOUS_CREDENTIAL_KEY) > 100


def test_missing_or_private_repository_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install(monkeypatch, lambda _request: httpx.Response(404))

    result = _fetch(include_readme=True)

    assert result.status == "not_found"
    assert len(seen) == 1
    previous = {"stars": 99, "forks": 1, "fetched_at": "2026-01-01T00:00:00Z"}
    assert github_meta_update(result, previous, NOW) == {
        "not_found": True,
        "fetched_at": "2026-10-22T08:00:00Z",
    }


def test_renamed_repository_follows_one_api_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/langchain-ai/langgraph":
            return httpx.Response(
                301,
                headers={"location": "https://api.github.com/repositories/42"},
            )
        return _json(REPOSITORY)

    seen = _install(monkeypatch, handler)

    assert _fetch().status == "ok"
    assert [request.url.path for request in seen] == [
        "/repos/langchain-ai/langgraph",
        "/repositories/42",
    ]


def test_redirect_off_the_api_origin_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install(
        monkeypatch,
        lambda _request: httpx.Response(
            302,
            headers={"location": "http://169.254.169.254/latest/meta-data"},
        ),
    )

    assert _fetch().status == "failed"
    assert len(seen) == 1


def test_readme_is_read_raw_bounded_and_cleaned(monkeypatch: pytest.MonkeyPatch) -> None:
    readme = (
        "<!-- hidden note -->\n"
        "# LangGraph\n\n"
        "[![CI](https://img.shields.io/badge/ci-green.svg)](https://ci.example)\n"
        '<p align="center"><img src="logo.png"></p>\n'
        "Build [stateful agents](https://docs.example) with graphs.\n"
        + "x" * 20_000
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/readme"):
            return httpx.Response(200, content=readme.encode())
        return _json(REPOSITORY)

    seen = _install(monkeypatch, handler)

    result = _fetch(include_readme=True)

    assert seen[1].url.path == "/repos/langchain-ai/langgraph/readme"
    assert seen[1].headers["accept"] == "application/vnd.github.raw"
    excerpt = result.readme_excerpt
    assert excerpt.startswith("# LangGraph\nBuild stateful agents with graphs.\nxxx")
    assert len(excerpt.encode()) <= github.README_EXCERPT_BYTES
    assert "shields.io" not in excerpt
    assert "<img" not in excerpt
    assert "hidden note" not in excerpt


def test_readme_only_when_stats_are_still_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install(monkeypatch, lambda _request: httpx.Response(200, content=b"Hello README"))

    result = _fetch(include_stats=False, include_readme=True)

    assert result.status == "skipped"
    assert result.readme_excerpt == "Hello README"
    assert [request.url.path for request in seen] == ["/repos/langchain-ai/langgraph/readme"]
    assert github_meta_update(result, None, NOW) is None


def test_transport_failure_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _install(monkeypatch, handler)

    assert _fetch(include_readme=True).status == "failed"


def test_utf8_cut_in_the_middle_of_a_character_is_dropped() -> None:
    raw = ("说明" * 2_000).encode()
    excerpt = readme_excerpt(raw)
    assert excerpt
    assert "�" not in excerpt
    assert len(excerpt.encode()) <= github.README_EXCERPT_BYTES


@pytest.mark.parametrize(
    ("kind", "meta", "expected"),
    [
        ("repo", {"host": "github.com", "owner": "a", "repo": "b"}, GitHubRepoRef("a", "b")),
        (
            "repo",
            {"host": "www.github.com", "owner": "a-b", "repo": "c.d"},
            GitHubRepoRef("a-b", "c.d"),
        ),
        ("article", {"host": "github.com", "owner": "a", "repo": "b"}, None),
        ("repo", {"host": "gitlab.com", "owner": "a", "repo": "b"}, None),
        ("repo", {"host": "github.com", "owner": "a/b", "repo": "c"}, None),
        ("repo", {"host": "github.com", "owner": "a", "repo": ".."}, None),
        ("repo", {"host": "github.com", "owner": "a"}, None),
        ("repo", None, None),
    ],
)
def test_github_repo_ref(kind: str, meta: dict[str, str] | None, expected: object) -> None:
    assert github_repo_ref(kind, meta) == expected


def test_stats_freshness_window() -> None:
    stats = {"stars": 1, "forks": 0}
    assert stats_are_fresh({**stats, "fetched_at": "2026-10-16T08:00:01Z"}, NOW)
    assert not stats_are_fresh({**stats, "fetched_at": "2026-10-15T08:00:00Z"}, NOW)
    assert not stats_are_fresh({**stats, "fetched_at": "2026-10-23T08:00:00Z"}, NOW)
    assert not stats_are_fresh({**stats}, NOW)
    assert stats_are_fresh({"not_found": True, "fetched_at": "2026-10-21T00:00:00Z"}, NOW)
    assert not stats_are_fresh({"rate_limited_at": "2026-10-22T00:00:00Z"}, NOW)
    assert not stats_are_fresh("damaged", NOW)


def test_repository_evidence_text() -> None:
    stats = github.stored_stats(
        {
            "stars": 7,
            "forks": 0,
            "language": "Rust",
            "topics": ["cli"],
            "license": "Apache-2.0",
            "archived": True,
            "description": "A fast tool",
        }
    )

    text = repository_evidence(GitHubRepoRef("o", "r"), stats, "README body")

    assert text.splitlines() == [
        "GitHub 仓库：o/r",
        "仓库简介：A fast tool",
        "主要语言：Rust",
        "主题标签：cli",
        "许可证：Apache-2.0",
        "星标数：7",
        "状态：已归档（只读）",
        "README 摘录：",
        "README body",
    ]
    assert repository_evidence(GitHubRepoRef("o", "r"), None, "") == ""
