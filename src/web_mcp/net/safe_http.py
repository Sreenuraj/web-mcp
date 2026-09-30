"""SSRF-safe HTTP fetching.

Defences:
  * http/https only, no embedded credentials, port allow-list
  * DNS is resolved by us, EVERY resolved address must be public; we then connect to the validated IP
    (Host header + TLS SNI/cert verification still use the hostname) => no DNS-rebinding window
  * redirects are followed manually and every hop is re-validated
  * response size (after decompression), content-type and time are capped
  * env proxies are ignored (trust_env=False) so they cannot be used to bypass the checks
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from ..config import FetchCfg

ALLOWED_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown", "application/pdf",
                 "application/json", "text/xml", "application/xml", "application/rss+xml", "application/atom+xml")

Resolver = Callable[[str, int], Awaitable[list[str]]]


class FetchError(Exception):
    """Fetch failed for a reason we can report to the agent."""


class BlockedError(FetchError):
    """Blocked by a safety rule (SSRF etc.)."""


@dataclass
class HttpResponse:
    url: str
    status: int
    content_type: str
    body: bytes
    headers: dict[str, str]
    truncated: bool = False


def ip_is_public(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip_is_public(str(ip.ipv4_mapped))
        if ip in ipaddress.ip_network("64:ff9b::/96"):  # NAT64
            return ip_is_public(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
        if ip in ipaddress.ip_network("2002::/16"):  # 6to4
            return ip_is_public(str(ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)))
        if ip in ipaddress.ip_network("fd00:ec2::/32"):  # AWS IMDS v6
            return False
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or not ip.is_global):
        return False
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return False
    return True


async def default_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"DNS lookup failed for {host}") from e
    ips: list[str] = []
    for fam, _, _, _, sockaddr in infos:
        if fam in (socket.AF_INET, socket.AF_INET6) and sockaddr[0] not in ips:
            ips.append(sockaddr[0])
    if not ips:
        raise FetchError(f"No address for {host}")
    return ips


def _fmt_host(ip: str) -> str:
    return f"[{ip}]" if ":" in ip else ip


class SafeHttp:
    def __init__(self, cfg: FetchCfg, resolver: Resolver | None = None):
        self.cfg = cfg
        self.resolver = resolver or default_resolver
        self._client: httpx.AsyncClient | None = None
        self._robots: dict[str, tuple[float, RobotFileParser | None]] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.cfg.timeout_seconds, connect=min(8.0, self.cfg.timeout_seconds)),
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": self.cfg.user_agent, "Accept-Language": "en;q=0.9",
                         "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain,application/json;q=0.9,*/*;q=0.1"},
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=0),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- validation -----------------------------------------------------------------------
    async def validate(self, url: str) -> tuple[str, int, list[str]]:
        try:
            p = urlsplit(url)
            port = p.port
        except ValueError as e:
            raise BlockedError(f"Malformed URL: {e}") from e
        if p.scheme not in ("http", "https"):
            raise BlockedError(f"Scheme '{p.scheme}' is not allowed (http/https only)")
        if not p.hostname:
            raise BlockedError("URL has no host")
        if p.username or p.password:
            raise BlockedError("URLs with embedded credentials are not allowed")
        port = port or (443 if p.scheme == "https" else 80)
        host = p.hostname.rstrip(".")
        if not self.cfg.unsafe_allow_private and port not in self.cfg.allowed_ports:
            raise BlockedError(f"Port {port} is not allowed")
        ips = await self.resolver(host, port)
        if not self.cfg.unsafe_allow_private:
            bad = [ip for ip in ips if not ip_is_public(ip)]
            if bad:
                raise BlockedError(f"{host} resolves to a non-public address ({bad[0]}); blocked to prevent SSRF")
        return host, port, ips

    # ---- fetching -------------------------------------------------------------------------
    async def get(self, url: str, *, max_bytes: int | None = None, allowed_types: tuple[str, ...] = ALLOWED_TYPES,
                  accept_any_type: bool = False) -> HttpResponse:
        client = await self._get_client()
        max_bytes = max_bytes or self.cfg.max_bytes
        current = url
        deadline = time.monotonic() + self.cfg.timeout_seconds * 2
        for _hop in range(self.cfg.max_redirects + 1):
            host, port, ips = await self.validate(current)
            p = urlsplit(current)
            last_err: Exception | None = None
            resp_info = None
            for ip in ips[:3]:
                pinned = p._replace(netloc=f"{_fmt_host(ip)}:{port}").geturl()
                default_port = 443 if p.scheme == "https" else 80
                host_header = host if port == default_port else f"{host}:{port}"
                try:
                    resp_info = await self._request(client, pinned, host, host_header, max_bytes, allowed_types,
                                                    accept_any_type, current, deadline)
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                    last_err = e
                    continue
                except httpx.TimeoutException as e:
                    raise FetchError("Timed out") from e
                except httpx.HTTPError as e:
                    raise FetchError(f"Network error: {type(e).__name__}") from e
            if resp_info is None:
                raise FetchError(f"Could not connect to {host}" + (f" ({type(last_err).__name__})" if last_err else ""))
            if isinstance(resp_info, str):  # redirect target
                current = urljoin(current, resp_info)
                continue
            return resp_info
        raise FetchError(f"Too many redirects (>{self.cfg.max_redirects})")

    async def _request(self, client, pinned, host, host_header, max_bytes, allowed_types, accept_any_type,
                       display_url, deadline):
        ext = {"sni_hostname": host} if pinned.startswith("https") else {}
        async with client.stream("GET", pinned, headers={"Host": host_header}, extensions=ext) as r:
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("location")
                if not loc:
                    raise FetchError(f"Redirect ({r.status_code}) without Location")
                return loc
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if r.status_code >= 400:
                raise FetchError(f"HTTP {r.status_code}")
            if not accept_any_type and ctype and not any(ctype == t or ctype.startswith(t) for t in allowed_types):
                raise FetchError(f"Unsupported content type '{ctype}'")
            declared = r.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes * 4 and "content-encoding" not in r.headers:
                raise FetchError(f"Response too large ({int(declared)//1000} KB)")
            buf = bytearray()
            truncated = False
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    truncated = True
                    del buf[max_bytes:]
                    break
                if time.monotonic() > deadline:
                    truncated = True
                    break
            return HttpResponse(display_url, r.status_code, ctype or "text/html", bytes(buf), dict(r.headers), truncated)

    # ---- robots.txt -----------------------------------------------------------------------
    async def robots_allows(self, url: str) -> bool:
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        now = time.time()
        cached = self._robots.get(origin)
        if cached is None or cached[0] < now:
            rp: RobotFileParser | None = None
            try:
                resp = await self.get(origin + "/robots.txt", max_bytes=200_000, accept_any_type=True)
                rp = RobotFileParser()
                rp.parse(resp.body.decode("utf-8", "replace").splitlines())
            except FetchError:
                rp = None  # missing/unreachable robots.txt => allowed
            except Exception:
                rp = None
            self._robots[origin] = (now + 3600, rp)
            cached = self._robots[origin]
        rp = cached[1]
        if rp is None:
            return True
        return rp.can_fetch("web-mcp", url) or rp.can_fetch("*", url)
