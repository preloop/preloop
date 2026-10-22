"""Bind guarded webhook attempts to checked addresses without changing identity."""

import asyncio
import ssl

import httpx

from preloop.config import settings
from preloop.services.event_webhooks.targets import checked_target_addresses


class WebhookTransport(httpx.AsyncBaseTransport):
    """Resolve and validate each attempt, preserving Host and TLS verification.

    Pools are separated by the original origin so two hostnames resolving to
    the same IP cannot reuse one another's TLS connections. The worker owns
    this transport for one bounded delivery batch. Environment proxies are
    disabled because their DNS resolution would bypass the destination check.
    """

    def __init__(self, *, verify: bool | ssl.SSLContext = True) -> None:
        self.verify = verify
        self.transports: dict[
            tuple[str, str, int | None], httpx.AsyncHTTPTransport
        ] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        origin = (request.url.scheme, request.url.host, request.url.port)
        if request.url.scheme not in {"http", "https"}:
            raise httpx.UnsupportedProtocol("Webhook URLs must use http(s)")
        transport = self.transports.get(origin)
        if transport is None:
            transport = httpx.AsyncHTTPTransport(verify=self.verify, trust_env=False)
            self.transports[origin] = transport
        if not settings.webhook_block_private_targets:
            return await transport.handle_async_request(request)
        try:
            addresses = await asyncio.to_thread(
                checked_target_addresses, request.url.host
            )
        except ValueError as exc:
            raise httpx.ConnectError(
                f"Webhook target blocked: {exc}", request=request
            ) from exc
        headers = request.headers.copy()
        headers["Host"] = request.url.netloc.decode("ascii")
        for index, address in enumerate(addresses):
            pinned = httpx.Request(
                request.method,
                request.url.copy_with(host=address),
                headers=headers,
                stream=request.stream,
                extensions={**request.extensions, "sni_hostname": request.url.host},
            )
            try:
                return await transport.handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # Try another checked address only before the HTTP request is
                # sent. Read/write failures must not duplicate a delivery.
                if index == len(addresses) - 1:
                    raise
        raise AssertionError("Target validation returned no addresses")

    async def aclose(self) -> None:
        for transport in self.transports.values():
            await transport.aclose()
