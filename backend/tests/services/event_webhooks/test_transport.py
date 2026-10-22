"""Synthetic DNS and local sockets exercise the delivery boundary."""

import asyncio
import socket
from dataclasses import replace
from typing import Any

import httpx
import pytest

from preloop.config import settings
from preloop.services.event_webhooks import targets, worker
from preloop.services.event_webhooks.transport import WebhookTransport


@pytest.fixture
def blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "webhook_block_private_targets", True)


def delivery(url: str) -> worker.PreparedDelivery:
    return worker.PreparedDelivery(
        delivery_id="example-delivery",
        endpoint_id="example-endpoint",
        url=url,
        secret="example-secret",
        event_id="example-event",
        event_type="example.test",
        attempt=1,
        body=b'{"example":true}',
    )


def dns_answers(monkeypatch: pytest.MonkeyPatch, addresses: list[str]) -> list[str]:
    calls: list[str] = []

    def resolve(host: str, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        calls.append(host)
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, port or 0),
            )
            for address in addresses
        ]

    monkeypatch.setattr(targets.socket, "getaddrinfo", resolve)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "10.0.0.1",
        "169.254.1.1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "100.64.0.1",
    ],
)
async def test_blocked_destinations_never_reach_http_transport(
    blocking: None,
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    dns_answers(monkeypatch, ["93.184.216.34", address])

    async def unexpected(*args: Any, **kwargs: Any) -> httpx.Response:
        pytest.fail("A blocked DNS answer reached the socket transport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", unexpected)
    async with httpx.AsyncClient(transport=WebhookTransport()) as client:
        outcome = await worker.post_delivery(
            client, delivery("https://collector.example/hook")
        )
    assert not outcome.success
    assert "Webhook target blocked" in outcome.error


@pytest.mark.asyncio
async def test_each_retry_resolves_and_pins_host_and_sni(
    blocking: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    addresses = ["93.184.216.34"]
    calls = dns_answers(monkeypatch, addresses)
    sent: list[httpx.Request] = []

    async def capture(self: Any, request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(503)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", capture)
    item = delivery("https://collector.example:8443/hook?example=yes")
    async with httpx.AsyncClient(transport=WebhookTransport()) as client:
        first = await worker.post_delivery(client, item)
        addresses[:] = ["127.0.0.1"]
        retry = await worker.post_delivery(client, replace(item, attempt=2))
    assert first.response_status == 503
    assert not retry.success
    assert "loopback" in retry.error
    assert calls == ["collector.example", "collector.example"]
    assert len(sent) == 1
    assert str(sent[0].url) == "https://93.184.216.34:8443/hook?example=yes"
    assert sent[0].headers["Host"] == "collector.example:8443"
    assert sent[0].extensions["sni_hostname"] == "collector.example"


@pytest.mark.asyncio
async def test_distinct_hostnames_do_not_share_a_pinned_tls_pool(
    blocking: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns_answers(monkeypatch, ["93.184.216.34"])
    pools: list[Any] = []

    async def capture(self: Any, request: httpx.Request) -> httpx.Response:
        pools.append(self)
        return httpx.Response(204)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", capture)
    async with httpx.AsyncClient(transport=WebhookTransport()) as client:
        for host in ("a.example", "b.example", "a.example"):
            assert (
                await worker.post_delivery(client, delivery(f"https://{host}/hook"))
            ).success
    assert pools[0] is pools[2]
    assert pools[0] is not pools[1]


@pytest.mark.asyncio
async def test_registration_then_rebinding_cannot_reach_local_receiver(
    blocking: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[bytes] = []

    async def receive(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        received.append(await reader.read(4096))
        writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(receive, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        addresses = ["93.184.216.34"]
        dns_answers(monkeypatch, addresses)
        url = f"http://collector.example:{port}/hook"
        assert targets.blocked_target_reason(url) is None
        addresses[:] = ["127.0.0.1"]
        async with httpx.AsyncClient(transport=WebhookTransport()) as client:
            assert not (await worker.post_delivery(client, delivery(url))).success
        assert received == []
        # The same local collector is intentionally supported with policy off.
        monkeypatch.setattr(settings, "webhook_block_private_targets", False)
        async with httpx.AsyncClient(transport=WebhookTransport()) as client:
            assert (await worker.post_delivery(client, delivery(url))).success
        assert len(received) == 1
        assert f"Host: collector.example:{port}".encode() in received[0]


@pytest.mark.asyncio
async def test_redirect_is_a_failure_without_following_target(
    blocking: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns_answers(monkeypatch, ["93.184.216.34"])
    sent: list[httpx.Request] = []

    async def redirect(self: Any, request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/hook"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", redirect)
    async with httpx.AsyncClient(
        transport=WebhookTransport(), follow_redirects=False
    ) as client:
        result = await worker.post_delivery(
            client, delivery("https://collector.example/hook")
        )
    assert result.response_status == 302
    assert not result.success
    assert len(sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("blocking_enabled", [True, False])
@pytest.mark.parametrize("certificate_host", ["collector.example", "wrong.example"])
async def test_real_tls_keeps_hostname_verification_and_sni(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    blocking_enabled: bool,
    certificate_host: str,
) -> None:
    """Route a synthetic checked public IP to our TLS fixture, never the network."""
    import ssl
    from datetime import datetime, timedelta, timezone

    import anyio
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    monkeypatch.setattr(settings, "webhook_block_private_targets", blocking_enabled)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, certificate_host)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(certificate_host)]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.create_default_context(cafile=str(cert_path))
    server_names: list[str] = []
    server_context.set_servername_callback(
        lambda sock, host, context: server_names.append(host)
    )
    received: list[bytes] = []

    async def receive(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        received.append(await reader.read(4096))
        writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(receive, "127.0.0.1", 0, ssl=server_context)
    real_connect = anyio.connect_tcp
    connected_hosts: list[str] = []

    async def local_connect(*args: Any, **kwargs: Any) -> Any:
        host = kwargs["remote_host"]
        connected_hosts.append(host)
        assert host == ("93.184.216.34" if blocking_enabled else "collector.example")
        kwargs["remote_host"] = "127.0.0.1"
        return await real_connect(*args, **kwargs)

    monkeypatch.setattr(anyio, "connect_tcp", local_connect)
    dns_calls = dns_answers(monkeypatch, ["93.184.216.34"])
    async with server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(
            transport=WebhookTransport(verify=client_context)
        ) as client:
            outcome = await worker.post_delivery(
                client, delivery(f"https://collector.example:{port}/hook")
            )
    assert len(connected_hosts) == 1
    assert dns_calls == (["collector.example"] if blocking_enabled else [])
    assert server_names == ["collector.example"]
    assert outcome.success is (certificate_host == "collector.example")
    if outcome.success:
        assert f"Host: collector.example:{port}".encode() in received[0]
    else:
        assert "CERTIFICATE_VERIFY_FAILED" in outcome.error
        assert received == []


@pytest.mark.asyncio
async def test_connection_failure_can_try_another_checked_address(
    blocking: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns_answers(monkeypatch, ["2606:4700:4700::1111", "93.184.216.34"])
    hosts: list[str] = []

    async def connect(self: Any, request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if len(hosts) == 1:
            raise httpx.ConnectError("Synthetic unavailable address")
        return httpx.Response(204)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", connect)
    async with httpx.AsyncClient(transport=WebhookTransport()) as client:
        assert (
            await worker.post_delivery(
                client, delivery("https://collector.example/hook")
            )
        ).success
    assert hosts == ["2606:4700:4700::1111", "93.184.216.34"]


@pytest.mark.asyncio
async def test_read_failure_does_not_resend_to_another_address(
    blocking: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns_answers(monkeypatch, ["93.184.216.34", "93.184.216.35"])
    hosts: list[str] = []

    async def read_failure(self: Any, request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        raise httpx.ReadTimeout("Synthetic receiver timeout")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", read_failure)
    async with httpx.AsyncClient(transport=WebhookTransport()) as client:
        assert not (
            await worker.post_delivery(
                client, delivery("https://collector.example/hook")
            )
        ).success
    assert hosts == ["93.184.216.34"]
