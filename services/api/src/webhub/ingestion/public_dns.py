"""Recover real public DNS answers when a transparent proxy returns Fake-IP.

The normal ingestion path uses system DNS and pins the approved address. Clash
and Mihomo Fake-IP mode deliberately replaces that answer with 198.18.0.0/15,
which cannot be approved as an Internet destination. In that one situation we
query a fixed, TLS-authenticated DNS-over-HTTPS endpoint and apply the same
public-address-only policy to every returned A and AAAA record.

This module never makes a user-supplied address dialable. The DoH connection is
to a fixed public IP, its certificate is verified for the fixed logical host,
and callers still pin the independently verified result before fetching.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from dataclasses import dataclass

import httpx

from webhub.providers.targets import ProviderTargetError

_MAX_DOH_BODY_BYTES = 64 * 1024
_DNS_RECORD_TYPES = (1, 28)
_MAX_CNAME_HOPS = 16


@dataclass(frozen=True, slots=True)
class _DoHEndpoint:
    connection_url: str
    hostname: str


_DOH_ENDPOINTS = (
    _DoHEndpoint("https://1.1.1.1/dns-query", "cloudflare-dns.com"),
    _DoHEndpoint("https://1.0.0.1/dns-query", "cloudflare-dns.com"),
)


class _DoHResponseError(ValueError):
    pass


class _UnsafeDNSAnswer(ValueError):
    pass


def _dns_name(value: object) -> str:
    if not isinstance(value, str):
        raise _DoHResponseError("DNS name is not text")
    try:
        normalized = value.rstrip(".").encode("idna").decode("ascii").casefold()
    except UnicodeError as error:
        raise _DoHResponseError("DNS name is invalid") from error
    labels = normalized.split(".")
    if (
        not normalized
        or len(normalized) > 253
        or any(not label or len(label) > 63 for label in labels)
    ):
        raise _DoHResponseError("DNS name is invalid")
    return normalized


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _DoHResponseError("DNS field is not an integer")
    return value


def _addresses_from_payload(
    payload: object,
    *,
    hostname: str,
    question_type: int,
) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    if not isinstance(payload, dict):
        raise _DoHResponseError("DNS response is not an object")
    status = _integer(payload.get("Status"))
    if status not in {0, 3}:
        raise _DoHResponseError("DNS resolver returned a transient failure")

    questions = payload.get("Question")
    if not isinstance(questions, list) or len(questions) != 1:
        raise _DoHResponseError("DNS response question is missing")
    question = questions[0]
    if (
        not isinstance(question, dict)
        or _dns_name(question.get("name")) != hostname
        or _integer(question.get("type")) != question_type
    ):
        raise _DoHResponseError("DNS response does not match its query")
    if status == 3:
        return set()

    answers = payload.get("Answer", [])
    if not isinstance(answers, list):
        raise _DoHResponseError("DNS answer is not a list")

    cnames: dict[str, set[str]] = {}
    address_records: list[
        tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address]
    ] = []
    for answer in answers:
        if not isinstance(answer, dict):
            raise _DoHResponseError("DNS answer item is invalid")
        record_type = _integer(answer.get("type"))
        if record_type not in {1, 5, 28}:
            continue
        owner = _dns_name(answer.get("name"))
        data = answer.get("data")
        if record_type == 5:
            cnames.setdefault(owner, set()).add(_dns_name(data))
            continue
        if not isinstance(data, str):
            raise _DoHResponseError("DNS address is invalid")
        try:
            address = ipaddress.ip_address(data)
        except ValueError as error:
            raise _DoHResponseError("DNS address is invalid") from error
        if (record_type == 1 and address.version != 4) or (
            record_type == 28 and address.version != 6
        ):
            raise _DoHResponseError("DNS address type is inconsistent")
        address_records.append((owner, address))

    reachable_names = {hostname}
    frontier = {hostname}
    for _hop in range(_MAX_CNAME_HOPS):
        next_frontier: set[str] = set()
        for owner in frontier:
            targets = cnames.get(owner, set())
            if len(targets) > 1:
                raise _DoHResponseError("DNS CNAME answer is ambiguous")
            next_frontier.update(targets - reachable_names)
        if not next_frontier:
            break
        reachable_names.update(next_frontier)
        frontier = next_frontier
    else:
        raise _DoHResponseError("DNS CNAME chain is too deep")

    addresses = {
        address for owner, address in address_records if owner in reachable_names
    }
    if any(not address.is_global for address in addresses):
        raise _UnsafeDNSAnswer("DNS answer contains a non-public address")
    return addresses


async def _query(
    client: httpx.AsyncClient,
    endpoint: _DoHEndpoint,
    *,
    hostname: str,
    record_type: int,
) -> object:
    async with client.stream(
        "GET",
        endpoint.connection_url,
        params={"name": hostname, "type": str(record_type)},
        headers={
            "host": endpoint.hostname,
            "accept": "application/dns-json",
            "user-agent": "WebHub/0.1",
        },
        extensions={"sni_hostname": endpoint.hostname},
    ) as response:
        if response.status_code != 200:
            raise _DoHResponseError("DNS endpoint returned an HTTP error")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type.casefold() != "application/dns-json":
            raise _DoHResponseError("DNS endpoint returned an unexpected content type")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > _MAX_DOH_BODY_BYTES:
                raise _DoHResponseError("DNS response is too large")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _DoHResponseError("DNS response is invalid JSON") from error


async def _resolve_from_endpoint(
    endpoint: _DoHEndpoint,
    *,
    hostname: str,
    timeout_seconds: int,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    limits = httpx.Limits(max_connections=1, max_keepalive_connections=0)
    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        follow_redirects=False,
        trust_env=False,
        limits=limits,
    ) as client:
        payloads = []
        for record_type in _DNS_RECORD_TYPES:
            payloads.append(
                await _query(
                    client,
                    endpoint,
                    hostname=hostname,
                    record_type=record_type,
                )
            )
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for record_type, payload in zip(_DNS_RECORD_TYPES, payloads, strict=True):
        addresses.update(
            _addresses_from_payload(
                payload,
                hostname=hostname,
                question_type=record_type,
            )
        )
    if not addresses:
        raise _DoHResponseError("DNS response contains no Internet address")
    return tuple(sorted(addresses, key=lambda address: (address.version, int(address))))


async def resolve_public_hostname(
    hostname: str,
    *,
    timeout_seconds: int,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    """Resolve one hostname through fixed DoH endpoints and require public IPs."""

    normalized = _dns_name(hostname)
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        raise ProviderTargetError(
            "unsafe_provider_target",
            "Fake-IP 回退只允许解析域名，不允许重新解释 IP 地址",
        )

    async def resolve() -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
        for endpoint in _DOH_ENDPOINTS:
            try:
                return await _resolve_from_endpoint(
                    endpoint,
                    hostname=normalized,
                    timeout_seconds=timeout_seconds,
                )
            except _UnsafeDNSAnswer as error:
                raise ProviderTargetError(
                    "unsafe_provider_target",
                    "资源域名的真实 DNS 结果包含本机、私网或其他不安全地址",
                ) from error
            except (httpx.HTTPError, OSError, _DoHResponseError):
                continue
        raise ProviderTargetError(
            "resource_public_dns_unreachable",
            "无法独立验证代理 Fake-IP 对应的真实公网地址",
        )

    try:
        async with asyncio.timeout(timeout_seconds):
            return await resolve()
    except TimeoutError as error:
        raise ProviderTargetError(
            "resource_public_dns_unreachable",
            "独立公网 DNS 验证超时",
        ) from error


__all__ = ["resolve_public_hostname"]
