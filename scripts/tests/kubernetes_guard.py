"""Fail-closed Kubernetes client selection for disposable integration tests."""

from typing import Any
from urllib.parse import urlparse

from kubernetes_asyncio import config


async def disposable_client(
    *, config_file: str, context: str, expected_host: str
) -> Any:
    """Check explicit identity and actual client host before exposing a client."""
    if not config_file or not context.startswith("kind-"):
        raise ValueError("explicit disposable config and kind context required")
    if urlparse(expected_host).hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("disposable API must be loopback")
    client = await config.new_client_from_config(
        config_file=config_file, context=context
    )
    if client.configuration.host != expected_host:
        await client.close()
        raise ValueError("Kubernetes client host does not match disposable endpoint")
    return client
