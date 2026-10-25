"""Operator-local discovery, model visibility and pricing regression coverage."""

import ipaddress
from uuid import uuid4

import httpx
import pytest

from preloop.config import settings
from preloop.models import models
from preloop.services import ai_model_provider, ai_model_pricing, model_pricing
from preloop.services.built_in_models import (
    is_available_built_in_model,
    public_built_in_model,
)
from preloop.services.discovery_transport import DiscoveryTransport


@pytest.fixture
def allow_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "model_endpoint_allowed_cidrs", "192.168.50.0/24")


def model(**kwargs: object) -> models.AIModel:
    values = dict(
        id=uuid4(),
        name="Local inference",
        provider_name="openai-compatible",
        model_identifier="example-local-model",
        api_endpoint="http://192.168.50.5:11434/v1",
        account_id=None,
        is_default=True,
        model_parameters={},
        meta_data={"gateway": {"enabled": True, "model_alias": "local/example"}},
    )
    values.update(kwargs)
    return models.AIModel(**values)


def estimate(row: models.AIModel, **kwargs: object) -> model_pricing.CostEstimate:
    return model_pricing.estimate_ai_model_usage_cost_detailed(
        row, prompt_tokens=1000, completion_tokens=1000, total_tokens=2000, **kwargs
    )


def test_allowed_private_discovery_and_deny_unlisted(allow_local: None) -> None:
    assert ai_model_provider.validate_discovery_endpoint("http://192.168.50.5/v1")
    with pytest.raises(ValueError, match="private address"):
        ai_model_provider.validate_discovery_endpoint("http://192.168.51.5/v1")


@pytest.mark.parametrize("host", ["169.254.169.254", "0.0.0.0", "224.0.0.1"])
def test_unsafe_addresses_never_become_allowed(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "model_endpoint_allowed_cidrs", "0.0.0.0/0")
    with pytest.raises(ValueError):
        ai_model_provider.validate_discovery_endpoint(f"http://{host}/v1")


@pytest.mark.asyncio
async def test_discovery_pins_validated_dns_and_preserves_tls_identity(
    allow_local: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions = []

    def resolve(host: str) -> list:
        resolutions.append(host)
        return [ipaddress.ip_address("192.168.50.5")]

    monkeypatch.setattr(ai_model_provider, "_resolve_endpoint_host_uncached", resolve)
    requests = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    transport = DiscoveryTransport()
    await transport.transport.aclose()
    transport.transport = httpx.MockTransport(upstream)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        await client.get("https://local.example.test:11434/v1/models")
    assert resolutions == ["local.example.test"]
    assert requests[0].url.host == "192.168.50.5"
    assert requests[0].headers["host"] == "local.example.test:11434"
    assert requests[0].extensions["sni_hostname"] == "local.example.test"


@pytest.mark.asyncio
async def test_discovery_rechecks_dns_and_refuses_redirect_and_mixed_answers(
    allow_local: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    addresses = [ipaddress.ip_address("192.168.50.5")]
    monkeypatch.setattr(
        ai_model_provider, "_resolve_endpoint_host_uncached", lambda host: addresses
    )
    requests = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    client = ai_model_provider._discovery_http_client(protect_endpoint=True)
    assert client is not None and client.follow_redirects is False
    transport = client._transport
    await transport.transport.aclose()
    transport.transport = httpx.MockTransport(upstream)
    async with client:
        response = await client.get("http://model.example.test/v1/models")
        assert response.status_code == 302 and len(requests) == 1
        addresses.append(ipaddress.ip_address("169.254.169.254"))
        with pytest.raises(ValueError):
            await client.get("http://model.example.test/v1/models")
        assert len(requests) == 1


@pytest.mark.parametrize(
    "alias", ["deepseek/deepseek-v4.1-flash", "local/example", None]
)
def test_every_enabled_global_model_is_builtin_without_leaking_endpoint(
    alias: str | None,
) -> None:
    row = model(meta_data={"gateway": {"enabled": True, "model_alias": alias}})
    assert is_available_built_in_model(row)
    public = public_built_in_model(row)
    assert public.is_built_in
    assert public.meta_data["gateway"]["model_alias"] == (
        alias or "openai-compatible/example-local-model"
    )
    assert not public.api_endpoint and "api_key" not in public.model_dump()
    row.account_id = uuid4()
    assert not is_available_built_in_model(row)
    row.account_id = None
    row.meta_data["retired"] = True
    assert not is_available_built_in_model(row)


def test_local_price_default_matches_ui_and_usage(allow_local: None) -> None:
    row = model()
    assert estimate(row).cost == 0.0
    assert estimate(row).source != "unpriced"
    price = ai_model_pricing._pricing_response(row, None)
    assert price.source != "none"
    assert price.price.input_per_1m == price.price.output_per_1m == 0.0


def test_local_explicit_prices_and_overrides_win(allow_local: None) -> None:
    row = model(
        meta_data={"pricing": {"input_price_per_1k": 2, "output_price_per_1k": 3}}
    )
    assert estimate(row).cost == 5.0
    assert estimate(row, pricing_override={"discount_percent": 50}).cost == 2.5
    assert (
        estimate(
            row, pricing_override={"input_price_per_1k": 0, "output_price_per_1k": 0}
        ).cost
        == 0.0
    )
    assert estimate(row, pricing_override={"request_price": 0.25}).cost == 0.25
    assert ai_model_pricing._pricing_response(row, None).price.input_per_1m == 2000


def test_unknown_remote_is_not_free(monkeypatch: pytest.MonkeyPatch) -> None:
    row = model(api_endpoint="https://api.example.test/v1")
    monkeypatch.setattr(
        model_pricing, "_estimate_litellm_cost", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(ai_model_pricing, "_catalog_entry", lambda *args: None)
    assert estimate(row).cost is None
    assert estimate(row).source == "unpriced"
    assert ai_model_pricing._pricing_response(row, None).source == "none"


def test_explicit_local_hostname_pricing_needs_no_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ai_model_provider,
        "_resolve_endpoint_host",
        lambda *args: pytest.fail("pricing must not resolve DNS"),
    )
    row = model(
        api_endpoint="http://ollama.example.test/v1",
        meta_data={"local_inference": True},
    )
    assert estimate(row).cost == 0.0


@pytest.mark.asyncio
async def test_discovery_transport_dials_real_allowed_local_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise actual HTTPX/httpcore dispatch, not only a mocked transport."""
    import asyncio

    monkeypatch.setattr(settings, "model_endpoint_allowed_cidrs", "127.0.0.1/32")
    received = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{"data":[]}'
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with ai_model_provider._discovery_http_client(
            protect_endpoint=True
        ) as client:
            result = await client.get(f"http://127.0.0.1:{port}/v1/models")
            assert result.json() == {"data": []}
        assert b"GET /v1/models HTTP/1.1" in received[0]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_discovery_pagination_cannot_forward_credentials_to_another_origin(
    allow_local: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ai_model_provider,
        "_resolve_endpoint_host_uncached",
        lambda host: [ipaddress.ip_address("192.168.50.5")],
    )
    sent = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"data": []})

    transport = DiscoveryTransport()
    await transport.transport.aclose()
    transport.transport = httpx.MockTransport(upstream)
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get(
            "https://model.example.test/models",
            headers={"Authorization": "Bearer example"},
        )
        with pytest.raises(ValueError, match="same origin"):
            await client.get(
                "https://other.example.test/models",
                headers={"Authorization": "Bearer example"},
            )
    assert len(sent) == 1


def test_runner_local_model_is_free_without_server_cidr_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "model_endpoint_allowed_cidrs", "")
    row = model(
        provider_name="openai",
        endpoint_type="runner",
        api_endpoint="http://localhost:11434/v1",
        meta_data={},
    )
    assert estimate(row).cost == 0.0
    assert ai_model_pricing._pricing_response(row, None).price.input_per_1m == 0.0
    assert estimate(row, pricing_override={"request_price": 0.25}).cost == 0.25


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["qwen", "openai-compatible"])
async def test_discovery_validation_offloads_dns_and_catalog_blocks_redirects(
    monkeypatch, provider
):
    import threading

    main_thread = threading.get_ident()
    dns_threads = []

    def resolve(host):
        dns_threads.append(threading.get_ident())
        return [ipaddress.ip_address("8.8.8.8")]

    monkeypatch.setattr(ai_model_provider, "_resolve_endpoint_host", resolve)
    monkeypatch.setattr(ai_model_provider, "_resolve_endpoint_host_uncached", resolve)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    requests = []
    clients = []
    factory = ai_model_provider._discovery_http_client

    async def upstream(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    def client_factory(*args, **kwargs):
        client = factory(*args, **kwargs)
        assert isinstance(client._transport, DiscoveryTransport)
        assert client.follow_redirects is False and client._trust_env is False
        client._transport.transport = httpx.MockTransport(upstream)
        clients.append(client)
        return client

    monkeypatch.setattr(ai_model_provider, "_discovery_http_client", client_factory)
    endpoint = "https://workspace.maas.aliyuncs.com/v1"
    if provider == "qwen":
        result = await ai_model_provider._get_qwen_models("test-key", endpoint)
    else:
        result = await ai_model_provider._get_openai_compatible_models(
            endpoint, "test-key"
        )
    assert result.source != "live"
    assert clients and all(client.is_closed for client in clients)
    assert len(requests) == 1
    assert requests[0].url.host == "8.8.8.8"
    assert requests[0].headers["host"] == "workspace.maas.aliyuncs.com"
    assert dns_threads and main_thread not in dns_threads
