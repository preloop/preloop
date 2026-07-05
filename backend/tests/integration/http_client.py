"""Shared httpx defaults for deployed-environment integration tests."""

from __future__ import annotations

import ssl
import time

import certifi
import httpx

# Review hosts on *.test.preloop.ai share an ingress IP. HTTP/2 connection
# coalescing or TLS session reuse across branch hostnames can surface 421 from
# nginx; keep HTTP/1.1, disable pooling, and avoid TLS session tickets.
_INTEGRATION_LIMITS = httpx.Limits(max_keepalive_connections=0, max_connections=1)


def integration_ssl_context() -> ssl.SSLContext:
    """Build a TLS context with certifi roots and no session tickets."""
    context = ssl.create_default_context(cafile=certifi.where())
    if hasattr(ssl, "OP_NO_TICKET"):
        context.options |= ssl.OP_NO_TICKET
    return context


def create_integration_sync_client(
    *,
    base_url: str,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> httpx.Client:
    """Return a sync httpx client safe for shared review ingress hosts."""
    merged_headers = {"Connection": "close"}
    if headers:
        merged_headers.update(headers)
    transport = httpx.HTTPTransport(
        http2=False,
        limits=_INTEGRATION_LIMITS,
        retries=0,
        verify=integration_ssl_context(),
    )
    return httpx.Client(
        transport=transport,
        base_url=base_url,
        headers=merged_headers,
        timeout=timeout,
        follow_redirects=False,
    )


def wait_for_preloop_ready(
    base_url: str,
    *,
    timeout: float = 180.0,
    interval: float = 5.0,
) -> None:
    """Block until the deployed instance passes DB-backed health checks."""
    deadline = time.monotonic() + timeout
    last_error = "unknown"

    while time.monotonic() < deadline:
        try:
            with create_integration_sync_client(
                base_url=base_url, timeout=15.0
            ) as client:
                response = client.get("/api/v1/health")
            if response.status_code != 200:
                last_error = (
                    f"health HTTP {response.status_code}: {response.text[:200]}"
                )
            else:
                body = response.json()
                if (
                    body.get("status") == "healthy"
                    and body.get("database") == "connected"
                    and body.get("mcp_server") == "available"
                ):
                    return
                last_error = f"health not ready: {body}"
        except httpx.HTTPError as exc:
            last_error = str(exc)

        time.sleep(interval)

    raise RuntimeError(
        f"Preloop not ready at {base_url} after {timeout:.0f}s: {last_error}"
    )
