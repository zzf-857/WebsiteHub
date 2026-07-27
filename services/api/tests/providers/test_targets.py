from __future__ import annotations

import asyncio
from ipaddress import ip_address

import pytest

from webhub.providers import targets
from webhub.providers.targets import (
    ProviderTargetError,
    resolve_resource_target,
    validate_connection_target,
)


def test_resource_target_keeps_query_drops_fragment_and_exposes_a_pinned_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    def resolve(hostname: str, port: int):
        calls.append((hostname, port))
        return {ip_address("93.184.216.34")}

    monkeypatch.setattr(targets, "_resolve", resolve)

    resolved = asyncio.run(
        resolve_resource_target(
            "https://Icons.Example:8443/favicon.png?v=2#dark",
            allow_private=False,
            timeout_seconds=1,
        )
    )

    assert calls == [("icons.example", 8443)]
    assert resolved.url == "https://icons.example:8443/favicon.png?v=2"
    assert resolved.host_header == "icons.example:8443"
    assert resolved.connection_url() == "https://93.184.216.34:8443/favicon.png?v=2"


def test_resource_target_rejects_the_whole_dns_answer_if_any_address_is_unsafe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        targets,
        "_resolve",
        lambda _hostname, _port: {
            ip_address("93.184.216.34"),
            ip_address("127.0.0.1"),
        },
    )

    with pytest.raises(ProviderTargetError) as raised:
        asyncio.run(
            resolve_resource_target(
                "https://rebound.example/icon.png?v=2",
                allow_private=False,
                timeout_seconds=1,
            )
        )

    assert raised.value.code == "unsafe_provider_target"


def test_provider_base_url_parser_remains_strict_while_resource_parser_is_permissive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        targets,
        "_resolve",
        lambda _hostname, _port: {ip_address("93.184.216.34")},
    )

    with pytest.raises(ProviderTargetError) as raised:
        asyncio.run(
            validate_connection_target(
                "https://provider.example/v1?tenant=alice",
                allow_private=False,
                timeout_seconds=1,
            )
        )
    assert raised.value.code == "invalid_base_url"

    resolved = asyncio.run(
        resolve_resource_target(
            "https://provider.example/favicon.ico?tenant=alice#light",
            allow_private=False,
            timeout_seconds=1,
        )
    )
    assert resolved.url.endswith("/favicon.ico?tenant=alice")


@pytest.mark.parametrize(
    "url",
    (
        "file:///etc/passwd",
        "https://user:password@example.com/icon.png",
        "https:///missing-host.png",
    ),
    ids=("non-http", "credentials", "missing-host"),
)
def test_resource_target_rejects_non_http_credentials_and_missing_hosts(url: str) -> None:
    with pytest.raises(ProviderTargetError) as raised:
        asyncio.run(
            resolve_resource_target(
                url,
                allow_private=False,
                timeout_seconds=1,
            )
        )
    assert raised.value.code == "invalid_resource_url"
