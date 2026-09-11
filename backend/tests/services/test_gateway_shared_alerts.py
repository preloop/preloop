"""Fleet alert reservation contracts, with independent clients and a fake broker."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
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

    async def connect(self, *args: object, **kwargs: object) -> SimpleNamespace:
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
            jetstream=MagicMock(return_value=jetstream), close=AsyncMock()
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
def isolate_local_state() -> None:
    alerts.reset_alert_state_for_tests()


@pytest.mark.asyncio
async def test_independent_clients_compete_once_and_expiry_allows_reminder() -> None:
    broker = _Broker()
    with patch("nats.connect", side_effect=broker.connect):
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
    with patch("nats.connect", side_effect=broker.connect):
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
        patch("nats.connect", side_effect=OSError("broker unavailable")) as connect,
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
        jetstream=MagicMock(return_value=SimpleNamespace(key_value=blocked_lookup)),
        close=AsyncMock(),
    )
    monkeypatch.setattr(alerts, "_SHARED_TIMEOUT_SECONDS", 0.01)
    with patch("nats.connect", return_value=client):
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
        jetstream=MagicMock(
            return_value=SimpleNamespace(key_value=AsyncMock(return_value=store))
        ),
        close=AsyncMock(side_effect=OSError("close failed")),
    )
    with patch("nats.connect", return_value=client):
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
