"""Bind guarded webhook attempts to checked addresses without changing identity."""

import asyncio
import ssl
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from preloop.config import settings
from preloop.services.event_webhooks.targets import checked_target_addresses

# Failover across a couple of checked addresses is enough; a long DNS answer
# must not multiply the per-attempt timeout.
MAX_ADDRESS_ATTEMPTS = 3


class WebhookTransport(httpx.AsyncBaseTransport):
    """Resolve and validate each attempt, preserving Host and TLS verification.

    Pools are separated by the original origin so two hostnames resolving to
    the same IP cannot reuse one another's TLS connections. The worker owns
    this transport for one bounded delivery batch. Environment proxies are
    disabled because their DNS resolution would bypass the destination check.

    Resolution runs on a bounded executor owned by the transport, so a slow
    resolver that outlives its timeout cannot occupy the process-wide default
    executor. The connect timeout bounds resolution plus every connect try.
    """

    def __init__(self, *, verify: bool | ssl.SSLContext = True) -> None:
        self.verify = verify
        self.resolver = ThreadPoolExecutor(
            max_workers=max(1, settings.webhook_delivery_concurrency),
            thread_name_prefix="webhook-resolve",
        )
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
        timeouts = request.extensions.get("timeout", {})
        budget = timeouts.get("connect")
        deadline = None if budget is None else time.monotonic() + budget
        loop = asyncio.get_running_loop()
        try:
            addresses = await asyncio.wait_for(
                loop.run_in_executor(
                    self.resolver, checked_target_addresses, request.url.host
                ),
                timeout=budget,
            )
        except TimeoutError as exc:
            raise httpx.ConnectTimeout(
                "Webhook target resolution timed out", request=request
            ) from exc
        except ValueError as exc:
            raise httpx.ConnectError(
                f"Webhook target blocked: {exc}", request=request
            ) from exc
        headers = request.headers.copy()
        headers["Host"] = request.url.netloc.decode("ascii")
        addresses = addresses[:MAX_ADDRESS_ATTEMPTS]
        for index, address in enumerate(addresses):
            extensions = {**request.extensions, "sni_hostname": request.url.host}
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise httpx.ConnectTimeout(
                        "Webhook connect budget exhausted", request=request
                    )
                extensions["timeout"] = {**timeouts, "connect": remaining}
            pinned = httpx.Request(
                request.method,
                request.url.copy_with(host=address),
                headers=headers,
                stream=request.stream,
                extensions=extensions,
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
        self.resolver.shutdown(wait=False, cancel_futures=True)
        for transport in self.transports.values():
            await transport.aclose()
