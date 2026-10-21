"""Recognize local inference without treating unknown remote APIs as free."""

from typing import Any
from urllib.parse import urlsplit

from preloop.services.ai_model_provider import (
    _allowed_endpoint_networks,
    _endpoint_address_error,
    _parse_ip_literal,
)


def uses_local_inference(model: Any) -> bool:
    """True for local-provider defaults or explicitly allowed private endpoints.

    No DNS lookup occurs in pricing. Hostname-based deployments can opt in with
    ``meta_data.local_inference = true``; that is an explicit pricing decision,
    just like configuring a zero-dollar price. Public custom endpoints remain
    unpriced unless the operator supplies prices or explicitly opts in.
    """
    metadata = getattr(model, "meta_data", None)
    if isinstance(metadata, dict) and metadata.get("local_inference") is True:
        return True
    endpoint = (getattr(model, "api_endpoint", None) or "").strip()
    provider = (getattr(model, "provider_name", "") or "").strip().lower()
    if not endpoint:
        return provider in {"ollama", "ollama_chat", "lmstudio"}
    try:
        url = urlsplit(endpoint)
        host = (url.hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    if url.scheme not in {"http", "https"}:
        return False
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        host = "127.0.0.1"
    address = _parse_ip_literal(host)
    return bool(
        address is not None
        and (address.is_private or address.is_loopback)
        and _endpoint_address_error(address, _allowed_endpoint_networks()) is None
    )
