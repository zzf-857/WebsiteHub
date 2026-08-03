from __future__ import annotations

import asyncio
import json
from ipaddress import ip_address

import httpx
import pytest

from webhub.ingestion import public_dns
from webhub.providers.targets import ProviderTargetError

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _dns_response(
    hostname: str,
    record_type: int,
    *,
    addresses: tuple[str, ...] = (),
) -> dict[str, object]:
    answer: list[dict[str, object]] = []
    canonical = "edge.example.net"
    if addresses:
        answer.append(
            {"name": hostname, "type": 5, "TTL": 60, "data": f"{canonical}."}
        )
        answer.extend(
            {
                "name": canonical,
                "type": 1 if ip_address(address).version == 4 else 28,
                "TTL": 60,
                "data": address,
            }
            for address in addresses
        )
    return {
        "Status": 0,
        "Question": [{"name": hostname, "type": record_type}],
        "Answer": answer,
    }


def _install_doh_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: httpx.AsyncBaseTransport,
    client_options: list[dict[str, object]],
) -> None:
    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        client_options.append(dict(kwargs))
        kwargs["transport"] = handler
        return _REAL_ASYNC_CLIENT(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def test_fake_ip_fallback_uses_fixed_tls_doh_and_returns_only_public_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []
    options: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        record_type = int(request.url.params["type"])
        addresses = ("93.184.216.34",) if record_type == 1 else ("2606:2800:220:1::",)
        payload = _dns_response("www.example.com", record_type, addresses=addresses)
        return httpx.Response(
            200,
            content=json.dumps(payload).encode(),
            headers={"content-type": "application/dns-json"},
        )

    _install_doh_transport(monkeypatch, httpx.MockTransport(respond), options)

    addresses = asyncio.run(
        public_dns.resolve_public_hostname("www.example.com", timeout_seconds=1)
    )

    assert addresses == (ip_address("93.184.216.34"), ip_address("2606:2800:220:1::"))
    assert [request.url.host for request in seen] == ["1.1.1.1", "1.1.1.1"]
    assert all(request.headers["host"] == "cloudflare-dns.com" for request in seen)
    assert all(request.extensions["sni_hostname"] == "cloudflare-dns.com" for request in seen)
    assert len(options) == 1
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False


def test_fake_ip_fallback_rejects_the_whole_answer_when_one_address_is_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        record_type = int(request.url.params["type"])
        addresses = (
            ("93.184.216.34", "127.0.0.1") if record_type == 1 else ()
        )
        payload = _dns_response("rebound.example", record_type, addresses=addresses)
        return httpx.Response(
            200,
            content=json.dumps(payload).encode(),
            headers={"content-type": "application/dns-json"},
        )

    _install_doh_transport(monkeypatch, httpx.MockTransport(respond), [])

    with pytest.raises(ProviderTargetError) as raised:
        asyncio.run(
            public_dns.resolve_public_hostname("rebound.example", timeout_seconds=1)
        )

    assert raised.value.code == "unsafe_provider_target"
    assert requests == 2


def test_fake_ip_fallback_never_reinterprets_an_ip_literal() -> None:
    with pytest.raises(ProviderTargetError) as raised:
        asyncio.run(public_dns.resolve_public_hostname("198.18.0.1", timeout_seconds=1))

    assert raised.value.code == "unsafe_provider_target"
