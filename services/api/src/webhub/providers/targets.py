from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit

_PRIVATE_IPV4_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_PRIVATE_IPV6_NETWORKS = (ipaddress.ip_network("fc00::/7"),)


class ProviderTargetError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ResolvedConnectionTarget:
    """One URL and the exact addresses approved for its next connection."""

    url: str
    hostname: str
    port: int
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]

    @property
    def host_header(self) -> str:
        parsed = urlsplit(self.url)
        host = f"[{self.hostname}]" if ":" in self.hostname else self.hostname
        default_port = (parsed.scheme == "https" and self.port == 443) or (
            parsed.scheme == "http" and self.port == 80
        )
        return host if default_port else f"{host}:{self.port}"

    def connection_url(
        self,
        address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None,
    ) -> str:
        """Replace only the authority used for TCP; path and query stay intact."""

        selected = address or self.addresses[0]
        parsed = urlsplit(self.url)
        host = f"[{selected}]" if selected.version == 6 else str(selected)
        default_port = (parsed.scheme == "https" and self.port == 443) or (
            parsed.scheme == "http" and self.port == 80
        )
        netloc = host if default_port else f"{host}:{self.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))


def _parsed_base_url(value: str) -> tuple[SplitResult, str, int]:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as error:
        raise ProviderTargetError("invalid_base_url", "Base URL 无效") from error
    hostname = parsed.hostname
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderTargetError(
            "invalid_base_url",
            "Base URL 必须是不含凭据、查询参数和片段的绝对 HTTP(S) URL",
        )
    try:
        ascii_hostname = hostname.rstrip(".").encode("idna").decode("ascii").casefold()
    except UnicodeError as error:
        raise ProviderTargetError("invalid_base_url", "Base URL 主机名无效") from error
    if not ascii_hostname:
        raise ProviderTargetError("invalid_base_url", "Base URL 主机名无效")
    return parsed, ascii_hostname, port or (443 if parsed.scheme.casefold() == "https" else 80)


def _parsed_resource_url(value: str) -> tuple[SplitResult, str, int]:
    """Parse a fetchable HTTP URL while allowing resource query/fragment suffixes."""

    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as error:
        raise ProviderTargetError("invalid_resource_url", "资源 URL 无效") from error
    hostname = parsed.hostname
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ProviderTargetError(
            "invalid_resource_url",
            "资源 URL 必须是不含凭据的绝对 HTTP(S) URL",
        )
    try:
        ascii_hostname = hostname.rstrip(".").encode("idna").decode("ascii").casefold()
    except UnicodeError as error:
        raise ProviderTargetError("invalid_resource_url", "资源 URL 主机名无效") from error
    if not ascii_hostname:
        raise ProviderTargetError("invalid_resource_url", "资源 URL 主机名无效")
    return parsed, ascii_hostname, port or (443 if parsed.scheme.casefold() == "https" else 80)


def _address(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(hostname.partition("%")[0])
    except ValueError:
        return None


def _is_explicitly_local(hostname: str) -> bool:
    return hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal"))


def _allowed_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_private: bool,
) -> bool:
    if (
        address.is_unspecified
        or address.is_multicast
        or address.is_link_local
        or address.is_reserved
    ):
        return False
    if allow_private:
        if address.is_loopback:
            return True
        private_networks = (
            _PRIVATE_IPV4_NETWORKS if address.version == 4 else _PRIVATE_IPV6_NETWORKS
        )
        return any(address in network for network in private_networks)
    return address.is_global


def normalize_base_url(value: str | None, *, allow_private: bool) -> str | None:
    if value is None or not value.strip():
        return None
    parsed, hostname, _ = _parsed_base_url(value)
    scheme = parsed.scheme.casefold()
    if not allow_private and scheme != "https":
        raise ProviderTargetError(
            "insecure_base_url",
            "除 Ollama 外的 Provider Base URL 必须使用 HTTPS",
        )
    direct_address = _address(hostname)
    if _is_explicitly_local(hostname) and not allow_private:
        raise ProviderTargetError(
            "unsafe_provider_target",
            "该 Provider 不允许访问本机或局域网地址",
        )
    if direct_address is not None and not _allowed_address(
        direct_address,
        allow_private=allow_private,
    ):
        raise ProviderTargetError(
            "unsafe_provider_target",
            "Provider Base URL 指向不允许访问的地址",
        )

    host_display = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = host_display if port is None or default_port else f"{host_display}:{port}"
    return urlunsplit((scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _resolve(hostname: str, port: int) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM):
        addresses.add(ipaddress.ip_address(item[4][0].partition("%")[0]))
    return addresses


async def _validated_addresses(
    hostname: str,
    port: int,
    *,
    allow_private: bool,
    timeout_seconds: int,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    direct_address = _address(hostname)
    if direct_address is not None:
        addresses = {direct_address}
    else:
        try:
            addresses = await asyncio.wait_for(
                asyncio.to_thread(_resolve, hostname, port),
                timeout=timeout_seconds,
            )
        except TimeoutError as error:
            raise ProviderTargetError(
                "provider_target_timeout",
                "Provider 地址解析超时",
            ) from error
        except OSError as error:
            raise ProviderTargetError(
                "provider_target_unreachable",
                "Provider 地址无法解析",
            ) from error
    if not addresses or any(
        not _allowed_address(address, allow_private=allow_private) for address in addresses
    ):
        raise ProviderTargetError(
            "unsafe_provider_target",
            "Provider 地址解析到本机、私网、保留或其他不安全目标",
        )
    return tuple(sorted(addresses, key=lambda address: (address.version, int(address))))


async def resolve_resource_target(
    value: str,
    *,
    allow_private: bool,
    timeout_seconds: int,
) -> ResolvedConnectionTarget:
    """Resolve a web resource once and return the addresses that may be dialled.

    The returned URL keeps its query but drops its fragment, which is a
    client-side identifier and must never be included in an HTTP request.
    """

    parsed, hostname, port = _parsed_resource_url(value)
    addresses = await _validated_addresses(
        hostname,
        port,
        allow_private=allow_private,
        timeout_seconds=timeout_seconds,
    )
    scheme = parsed.scheme.casefold()
    host_display = f"[{hostname}]" if ":" in hostname else hostname
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = host_display if default_port else f"{host_display}:{port}"
    request_url = urlunsplit((scheme, netloc, parsed.path, parsed.query, ""))
    return ResolvedConnectionTarget(
        url=request_url,
        hostname=hostname,
        port=port,
        addresses=addresses,
    )


async def validate_connection_target(
    value: str,
    *,
    allow_private: bool,
    timeout_seconds: int,
) -> None:
    _, hostname, port = _parsed_base_url(value)
    await _validated_addresses(
        hostname,
        port,
        allow_private=allow_private,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "ProviderTargetError",
    "ResolvedConnectionTarget",
    "normalize_base_url",
    "resolve_resource_target",
    "validate_connection_target",
]
