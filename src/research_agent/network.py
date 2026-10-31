from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


class NetworkPolicyError(ValueError):
    pass


async def public_addresses(url: str) -> tuple[str, list[str]]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise NetworkPolicyError("Only public HTTP(S) URLs without credentials are supported")
    if parsed.port not in {None, 80, 443}:
        raise NetworkPolicyError("Nonstandard network ports are disabled")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "metadata.google.internal"} or host.endswith((".local", ".localhost", ".internal")):
        raise NetworkPolicyError("Local network targets are disabled")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [str(literal)]
    except ValueError:
        records = await asyncio.to_thread(socket.getaddrinfo, host, parsed.port or 443,
                                         socket.AF_UNSPEC, socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(r[4][0] for r in records))
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise NetworkPolicyError("URL resolves to a private, reserved or mixed network target")
    return host, addresses


class PublicHTTP:
    """Bounded public fetches. Pin DNS results while preserving Host and TLS SNI."""

    def __init__(self, max_bytes: int = 1_500_000, timeout: float = 25):
        self.max_bytes = max_bytes
        self.timeout = timeout

    async def request(self, method: str, url: str, *, json_body=None,
                      headers: dict | None = None, max_bytes: int | None = None) -> tuple[bytes, str, dict]:
        limit = max_bytes or self.max_bytes
        async with asyncio.timeout(self.timeout):
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False, follow_redirects=False) as client:
                current = url
                for _ in range(6):
                    host, addresses = await public_addresses(current)
                    parsed = urlsplit(current)
                    address = addresses[0]
                    netloc = f"[{address}]" if ":" in address else address
                    if parsed.port:
                        netloc += f":{parsed.port}"
                    pinned = urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
                    request_headers = {"User-Agent": "DailyResearchAgent/0.1 (+read-only research)",
                                       "Accept-Encoding": "identity",
                                       **(headers or {}), "Host": parsed.netloc}
                    async with client.stream(method, pinned, json=json_body, headers=request_headers,
                                             extensions={"sni_hostname": host}) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            target = urljoin(current, response.headers.get("location", ""))
                            if not target or target == current:
                                raise NetworkPolicyError("Invalid redirect")
                            # Forward credentials only to the same origin.
                            if urlsplit(target).netloc != parsed.netloc:
                                headers = {k: v for k, v in (headers or {}).items()
                                           if k.lower() not in {"authorization", "cookie"}}
                            if parsed.scheme == "https" and urlsplit(target).scheme != "https":
                                raise NetworkPolicyError("HTTPS downgrade redirect rejected")
                            current = target
                            if response.status_code == 303:
                                method, json_body = "GET", None
                            continue
                        if response.status_code >= 400:
                            error_chunks = []
                            async for part in response.aiter_bytes(chunk_size=1024):
                                error_chunks.append(part)
                                if sum(len(p) for p in error_chunks) >= 1500:
                                    break
                            body = b"".join(error_chunks)[:1500].decode("utf-8", "replace")
                            raise httpx.HTTPStatusError(f"HTTP {response.status_code}: {body}",
                                                        request=response.request, response=response)
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes(chunk_size=16384):
                            size += len(chunk)
                            if size > limit:
                                raise NetworkPolicyError("Response exceeds the configured size limit")
                            chunks.append(chunk)
                        return b"".join(chunks), current, dict(response.headers)
                raise NetworkPolicyError("Too many redirects")

    async def json(self, method: str, url: str, **kwargs):
        import json
        body, _, _ = await self.request(method, url, **kwargs)
        return json.loads(body)
