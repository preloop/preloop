"""Fleet alert reservation contracts, with independent clients and a fake broker."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nats.js.errors import BucketNotFoundError, KeyWrongLastSequenceError

from preloop.services import gateway_error_alerts as alerts


class _Broker:
    """Implement the server's atomic create and expiry, separate from clients."""

    def __init__(self) -> None:
        self.now = 0.0
        self.config = None
        self.keys: dict[str, float] = {}
        self.clients: list[SimpleNamespace] = []

    def connect(self, *args: object, **kwargs: object) -> SimpleNamespace:
        # Every call gets a distinct connection and KV handle. No client cache
        # or Python module state coordinates the competing reservations.
        store = SimpleNamespace(create=AsyncMock(side_effect=self.create))

        async def lookup(bucket: str) -> SimpleNamespace:
            if self.config is None:
                raise BucketNotFoundError
            return store

        async def create_bucket(config: object) -> SimpleNamespace:
            self.config = config
            await asyncio.sleep(0)
            return store

        jetstream = SimpleNamespace(
            key_value=AsyncMock(side_effect=lookup),
            create_key_value=AsyncMock(side_effect=create_bucket),
        )
        client = SimpleNamespace(
            connect=AsyncMock(),
            jetstream=MagicMock(return_value=jetstream),
            close=AsyncMock(),
            is_connected=True,
        )
        self.clients.append(client)
        return client

    async def create(self, key: str, value: bytes) -> int:
        await asyncio.sleep(0)
        if self.keys.get(key, 0) > self.now:
            raise KeyWrongLastSequenceError
        self.keys[key] = self.now + self.config.ttl
        return len(self.keys)


@pytest.fixture(autouse=True)
def isolate_local_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    alerts.reset_alert_state_for_tests()
    worker = alerts._SharedAlertWorker()
    monkeypatch.setattr(alerts, "_SHARED_WORKER", worker)
    yield
    worker.close()


@pytest.mark.asyncio
async def test_independent_clients_compete_once_and_expiry_allows_reminder() -> None:
    broker = _Broker()
    with patch("nats.NATS", side_effect=broker.connect):
        results = await asyncio.gather(
            *(alerts._reserve_shared_alert("incident", 300.0) for _ in range(8))
        )
        assert results.count(True) == 1
        assert results.count(False) == 7
        assert len({id(client) for client in broker.clients}) == 8
        broker.now = 299.0
        assert not await alerts._reserve_shared_alert("incident", 300.0)
        broker.now = 300.0
        assert await alerts._reserve_shared_alert("incident", 300.0)
    assert broker.config.ttl == 300.0
    assert broker.config.max_bytes == alerts._SHARED_MAX_BYTES
    assert broker.config.history == 1
    for client in broker.clients:
        client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_different_incidents_have_separate_shared_windows() -> None:
    broker = _Broker()
    first = alerts.gateway_alert_key("openai", 502, account_id="account-a")
    second = alerts.gateway_alert_key("openai", 502, account_id="account-b")
    with patch("nats.NATS", side_effect=broker.connect):
        assert await alerts._reserve_shared_alert(first, 60.0)
        assert await alerts._reserve_shared_alert(second, 60.0)
        assert not await alerts._reserve_shared_alert(first, 60.0)


@pytest.mark.parametrize(
    "changed",
    [
        {"account_id": "account-b"},
        {"upstream_provider": "other-provider"},
        {"model": "other-model"},
        {"model_id": "different-configured-upstream"},
        {"error_class": "timeout"},
        {"upstream_status": 503},
    ],
)
def test_incident_key_includes_identity_without_plaintext(changed: dict) -> None:
    context = dict(
        account_id="account-a",
        upstream_provider="example-provider",
        model="example-model",
        model_id="configured-upstream",
        error_class="upstream_error",
        upstream_status=500,
    )
    original = alerts.gateway_alert_key("openai", 502, **context)
    assert len(original) == 64
    assert alerts.gateway_alert_key("openai", 502, **context) == original
    assert alerts.gateway_alert_key("anthropic", 502, **context) != original
    assert alerts.gateway_alert_key("openai", 503, **context) != original
    assert alerts.gateway_alert_key("openai", 502, **(context | changed)) != original


def _deliver_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = MagicMock()
    executor.submit.side_effect = lambda deliver: deliver()
    monkeypatch.setattr(alerts, "_ALERT_EXECUTOR", executor)
    monkeypatch.setattr(alerts, "_ALERT_PENDING", threading.BoundedSemaphore(32))


def test_shared_outage_falls_back_to_local_cooldown_and_reminder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _deliver_inline(monkeypatch)
    key = alerts.gateway_alert_key("openai", 502, model="example-model")
    with (
        patch("nats.NATS", side_effect=OSError("broker unavailable")) as connect,
        patch("preloop.sync.tasks.notify_admins") as notify,
    ):
        for now in [0.0, 1.0, 10.0, 299.0, 300.0]:
            send, count = alerts.reserve_gateway_5xx_alert(
                "openai", 502, now=now, incident_key=key
            )
            if send:
                alerts.enqueue_gateway_5xx_alert(
                    subject="Failure",
                    message=f"Local suppressed: {count}",
                    incident_key=key,
                )
        assert connect.call_count == 2
        assert notify.call_count == 2
        assert notify.call_args.kwargs["message"] == "Local suppressed: 3"


def test_losing_replica_does_not_notify_but_keeps_local_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _deliver_inline(monkeypatch)
    key = "incident"
    with (
        patch.object(alerts, "_reserve_shared_or_fallback", return_value=False),
        patch("preloop.sync.tasks.notify_admins") as notify,
    ):
        assert alerts.reserve_gateway_5xx_alert("openai", 502, now=0, incident_key=key)[
            0
        ]
        alerts.enqueue_gateway_5xx_alert(
            subject="Failure", message="Detail", incident_key=key
        )
        notify.assert_not_called()
        assert not alerts.reserve_gateway_5xx_alert(
            "openai", 502, now=1, incident_key=key
        )[0]


@pytest.mark.asyncio
async def test_shared_deadline_cancels_slow_broker_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def blocked_lookup(bucket: str) -> None:
        await asyncio.Event().wait()

    client = SimpleNamespace(
        connect=AsyncMock(),
        jetstream=MagicMock(return_value=SimpleNamespace(key_value=blocked_lookup)),
        close=AsyncMock(),
    )
    monkeypatch.setattr(alerts, "_SHARED_TIMEOUT_SECONDS", 0.01)
    with patch("nats.NATS", return_value=client):
        with pytest.raises(TimeoutError):
            await alerts._reserve_shared_alert("incident", 300.0)
    client.close.assert_awaited_once()


def test_local_incident_cardinality_is_bounded_and_expired_slots_reclaimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(alerts, "_MAX_LOCAL_WINDOWS", 2)
    assert alerts.reserve_gateway_5xx_alert("openai", 502, now=0, incident_key="a")[0]
    assert alerts.reserve_gateway_5xx_alert("openai", 502, now=0, incident_key="b")[0]
    assert not alerts.reserve_gateway_5xx_alert("openai", 502, now=1, incident_key="c")[
        0
    ]
    assert not alerts.reserve_gateway_5xx_alert("openai", 502, now=2, incident_key="a")[
        0
    ]
    assert len(alerts._state) == 2
    assert alerts.reserve_gateway_5xx_alert("openai", 502, now=300, incident_key="c")[0]
    assert len(alerts._state) <= 2


@pytest.mark.asyncio
async def test_close_failure_does_not_turn_lost_reservation_into_send() -> None:
    store = SimpleNamespace(create=AsyncMock(side_effect=KeyWrongLastSequenceError))
    client = SimpleNamespace(
        connect=AsyncMock(),
        jetstream=MagicMock(
            return_value=SimpleNamespace(key_value=AsyncMock(return_value=store))
        ),
        close=AsyncMock(side_effect=OSError("close failed")),
    )
    with patch("nats.NATS", return_value=client):
        assert not await alerts._reserve_shared_alert("incident", 300.0)


def test_reservation_runs_off_request_thread_with_bounded_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    entered = threading.Event()
    release = threading.Event()
    request_thread = threading.get_ident()

    def blocked_reservation(key: str) -> bool:
        assert threading.get_ident() != request_thread
        entered.set()
        assert release.wait(5)
        return True

    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(alerts, "_ALERT_EXECUTOR", executor)
    monkeypatch.setattr(alerts, "_ALERT_PENDING", threading.BoundedSemaphore(1))
    try:
        with (
            patch.object(
                alerts, "_reserve_shared_or_fallback", side_effect=blocked_reservation
            ) as reserve,
            patch("preloop.sync.tasks.notify_admins") as notify,
        ):
            alerts.enqueue_gateway_5xx_alert(
                subject="Failure", message="Detail", incident_key="a"
            )
            assert entered.wait(5)
            alerts.enqueue_gateway_5xx_alert(
                subject="Other", message="Detail", incident_key="b"
            )
            assert reserve.call_count == 1
            notify.assert_not_called()
            release.set()
            executor.shutdown(wait=True)
            notify.assert_called_once()
    finally:
        release.set()
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_store_reuses_connection_and_bucket_until_disconnect() -> None:
    broker = _Broker()
    store = alerts._SharedAlertStore()
    with patch("nats.NATS", side_effect=broker.connect) as connect:
        assert await store.reserve("first", 300.0)
        assert await store.reserve("second", 300.0)
        assert not await store.reserve("first", 300.0)
        assert connect.call_count == 1
        broker.clients[0].jetstream.assert_called_once()
        broker.clients[0].is_connected = False
        assert await store.reserve("third", 300.0)
        assert connect.call_count == 2
        broker.clients[0].close.assert_awaited_once()
        await store.close()
        broker.clients[1].close.assert_awaited_once()


def test_worker_runs_loop_while_idle_and_rejects_late_work() -> None:
    worker = alerts._SharedAlertWorker()
    broker = _Broker()
    heartbeat = threading.Event()
    with patch("nats.NATS", side_effect=broker.connect) as connect:
        try:
            assert worker.reserve("first", 300.0)
            worker._loop.call_soon_threadsafe(
                worker._loop.call_later, 0.01, heartbeat.set
            )
            assert heartbeat.wait(2), "idle worker stopped servicing its event loop"
            assert worker.reserve("second", 300.0)
            assert connect.call_count == 1
        finally:
            worker.close()
        assert worker._loop.is_closed()
        assert not worker._thread.is_alive()
        broker.clients[0].close.assert_awaited_once()
        with pytest.raises(RuntimeError, match="shut down"):
            worker.reserve("late", 300.0)
        assert connect.call_count == 1


def test_shutdown_cancels_reservation_and_finishes_close_before_stopping() -> None:
    worker = alerts._SharedAlertWorker()
    entered = threading.Event()
    closed = threading.Event()

    async def blocked_create(*args: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    async def close() -> None:
        await asyncio.sleep(0.01)
        closed.set()

    store = SimpleNamespace(create=blocked_create)
    client = SimpleNamespace(
        connect=AsyncMock(),
        is_connected=True,
        jetstream=lambda **kwargs: SimpleNamespace(
            key_value=AsyncMock(return_value=store)
        ),
        close=close,
    )
    results: list[BaseException] = []

    def reserve() -> None:
        try:
            worker.reserve("incident", 300.0)
        except BaseException as error:
            results.append(error)

    with patch("nats.NATS", return_value=client):
        thread = threading.Thread(target=reserve)
        thread.start()
        assert entered.wait(2)
        worker.close()
        thread.join(timeout=2)
    assert closed.is_set()
    assert worker._loop.is_closed()
    assert not thread.is_alive()
    assert results


def test_enqueue_after_shutdown_does_not_start_broker_or_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alerts._SHARED_WORKER.close()
    executor = MagicMock()
    monkeypatch.setattr(alerts, "_ALERT_EXECUTOR", executor)
    alerts.enqueue_gateway_5xx_alert(
        subject="Failure", message="Detail", incident_key="late"
    )
    executor.submit.assert_not_called()
    assert alerts._SHARED_WORKER._loop is None


@pytest.mark.asyncio
async def test_handshake_timeout_closes_owned_client_and_tcp_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nats
    from nats.aio.client import Client

    from preloop.config import settings

    peer_closed = asyncio.Event()

    async def stalled_handshake(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            writer.write(
                b'INFO {"server_id":"fixture","version":"2.11.8","proto":1,"max_payload":1048576}\r\n'
            )
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            peer_closed.set()

    server = await asyncio.start_server(stalled_handshake, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = Client()
    store = alerts._SharedAlertStore()
    monkeypatch.setattr(settings, "nats_url", f"nats://127.0.0.1:{port}")
    monkeypatch.setattr(alerts, "_SHARED_TIMEOUT_SECONDS", 0.05)
    try:
        with patch.object(nats, "NATS", return_value=client):
            with pytest.raises(TimeoutError):
                await store.reserve("incident", 300.0)
        assert store.connection is None
        assert client.is_closed
        await asyncio.wait_for(peer_closed.wait(), timeout=1.0)
    finally:
        await store.close()
        await client.close()
        server.close()
        await server.wait_closed()
