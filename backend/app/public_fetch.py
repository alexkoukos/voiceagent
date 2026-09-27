"""Bounded public HTTPS fetches; validate redirects and pin DNS results to prevent SSRF."""
import asyncio
import ipaddress
import socket
from urllib.parse import urljoin

import httpx


async def get(url: str, *, limit: int = 2 * 1024 * 1024) -> tuple[bytes, str]:
    async with asyncio.timeout(25):
        async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
            for _ in range(5):
                parsed = httpx.URL(url)
                if parsed.scheme != "https" or not parsed.host or parsed.userinfo or parsed.port not in (None, 443):
                    raise ValueError("A public HTTPS URL is required")
                addresses = await asyncio.get_running_loop().getaddrinfo(parsed.host, 443, type=socket.SOCK_STREAM)
                ips = list(dict.fromkeys(item[4][0] for item in addresses))
                if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
                    raise ValueError("Private network addresses are not allowed")
                pinned = parsed.copy_with(host=ips[0])
                async with client.stream("GET", pinned, headers={"Host": parsed.host, "User-Agent": "VoiceAgent/2.0"},
                                         extensions={"sni_hostname": parsed.host}) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers["location"])
                        continue
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > limit:
                            raise ValueError("Response too large")
                    return bytes(data), url
            raise ValueError("Too many redirects")
