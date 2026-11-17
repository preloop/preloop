"""REST traffic is recorded for every credential kind and status.

Success traffic is rolled up per minute; every 4xx/5xx is kept as an event.
Route templates are stored, never raw paths or query values.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import fastapi
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.app import ApiUsageMiddleware
from preloop.api.auth.jwt import get_current_user
from preloop.api.traffic_attribution import attribute_principal, traffic_principal
from preloop.models import models
from preloop.models.crud import crud_api_key, crud_api_traffic
from preloop.models.db.session import get_db_session
from preloop.services import api_traffic_recorder
from preloop.services.api_traffic_recorder import (
    ApiTrafficRecorder,
    TrafficPrincipal,
    TrafficSample,
)
from preloop.services.api_traffic_retention import prune_api_traffic

NOW = datetime(2026, 1, 5, 12, 0, 30, tzinfo=timezone.utc)


def _sample(status: int = 200, **overrides: Any) -> TrafficSample:
    fields: dict[str, Any] = {
        "started_at": NOW,
        "principal": TrafficPrincipal(kind="api_key", account_id=uuid4()),
        "method": "GET",
        "route_template": "/api/v1/flows/{flow_id}",
        "status_code": status,
        "duration_ms": 5,
    }
    fields.update(overrides)
    return TrafficSample(**fields)


class _Capture:
    def __init__(self, fail: int = 0) -> None:
        self.flushes: list[tuple[dict, list]] = []
        self.fail = fail

    def __call__(self, rollups: dict, events: list) -> None:
        if self.fail:
            self.fail -= 1
            raise RuntimeError("database down")
        self.flushes.append((dict(rollups), list(events)))


def _recorder(writer: _Capture, **kwargs: Any) -> ApiTrafficRecorder:
    recorder = ApiTrafficRecorder(writer=writer, flush_seconds=3600, **kwargs)
    recorder._stop.set()  # no background thread in tests
    return recorder


def test_a_polling_client_costs_one_rollup_row_per_minute() -> None:
    """27 reads of one route in one minute become one series, no events."""
    writer = _Capture()
    recorder = _recorder(writer)
    principal = TrafficPrincipal(kind="api_key", account_id=uuid4())
    for second in range(27):
        recorder.record(
            _sample(principal=principal, started_at=NOW + timedelta(seconds=second))
        )

    assert recorder.flush()
    [(rollups, events)] = writer.flushes
    assert events == []
    [(key, counts)] = rollups.items()
    assert key[0] == datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
    assert key[2:] == ("api_key", "GET", "/api/v1/flows/{flow_id}", 2)
    assert counts.request_count == 27


def test_every_error_is_kept_individually_and_counted_in_the_rollup() -> None:
    writer = _Capture()
    recorder = _recorder(writer)
    principal = TrafficPrincipal(kind="ci_identity", account_id=uuid4())
    for status in (404, 404, 500):
        recorder.record(_sample(status, principal=principal))

    recorder.flush()
    [(rollups, events)] = writer.flushes
    assert sorted(e.status_code for e in events) == [404, 404, 500]
    assert {key[5]: c.request_count for key, c in rollups.items()} == {4: 2, 5: 1}


def test_a_failed_flush_keeps_the_data_for_the_next_attempt() -> None:
    writer = _Capture(fail=1)
    recorder = _recorder(writer)
    principal = TrafficPrincipal(kind="session", account_id=uuid4())
    recorder.record(_sample(503, principal=principal))
    assert recorder.flush() is False
    recorder.record(_sample(503, principal=principal))
    assert recorder.flush() is True

    [(rollups, events)] = writer.flushes
    assert len(events) == 2
    assert [c.request_count for c in rollups.values()] == [2]
    assert recorder.stats()["failed_flushes"] == 1


def test_memory_is_bounded_and_overflow_is_counted() -> None:
    writer = _Capture()
    recorder = _recorder(writer, max_rollup_keys=2, max_pending_events=1)
    for index in range(3):
        recorder.record(_sample(400, route_template=f"/api/v1/r{index}"))

    stats = recorder.stats()
    assert stats["pending_series"] == 2
    assert stats["pending_events"] == 1
    assert stats["dropped"] == 3


def test_rollups_are_additive_and_events_prunable(
    db_session: Session, test_user: models.User
) -> None:
    bucket = NOW.replace(second=0)
    row = {
        "bucket_start": bucket,
        "account_id": test_user.account_id,
        "principal_kind": "api_key",
        "method": "GET",
        "route_template": "/api/v1/flows",
        "status_class": 4,
        "request_count": 3,
        "total_duration_ms": 30,
        "max_duration_ms": 20,
    }
    crud_api_traffic.add_rollups(db_session, rows=[row])
    crud_api_traffic.add_rollups(
        db_session, rows=[{**row, "request_count": 2, "max_duration_ms": 50}]
    )
    crud_api_traffic.add_events(
        db_session,
        events=[
            api_traffic_recorder.event_row(
                _sample(
                    404,
                    started_at=NOW - timedelta(days=40),
                    principal=TrafficPrincipal(
                        kind="api_key", account_id=test_user.account_id
                    ),
                )
            )
        ],
    )

    [problem] = crud_api_traffic.route_problems(
        db_session,
        account_id=test_user.account_id,
        since=bucket - timedelta(hours=1),
        until=bucket + timedelta(hours=1),
    )
    assert problem["count"] == 5
    stored = (
        db_session.query(models.ApiTrafficRollup)
        .filter_by(account_id=test_user.account_id)
        .one()
    )
    assert (stored.request_count, stored.max_duration_ms) == (5, 50)

    deleted = prune_api_traffic(db_session, now=NOW)
    assert deleted["events"] >= 1
    assert (
        crud_api_traffic.latest_event(db_session, account_id=test_user.account_id)
        is None
    )


def test_event_retention_cannot_go_below_seven_days(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_TRAFFIC_EVENT_RETENTION_DAYS", "1")
    crud_api_traffic.add_events(
        db_session,
        events=[
            api_traffic_recorder.event_row(
                _sample(
                    500,
                    started_at=NOW - timedelta(days=6),
                    principal=TrafficPrincipal(
                        kind="session", account_id=test_user.account_id
                    ),
                )
            )
        ],
    )
    prune_api_traffic(db_session, now=NOW)
    assert (
        crud_api_traffic.latest_event(db_session, account_id=test_user.account_id)
        is not None
    )


def test_attribution_is_set_once_and_read_from_the_scope() -> None:
    scope: dict[str, Any] = {"type": "http"}
    account_id, first, second = uuid4(), uuid4(), uuid4()
    attribute_principal(
        scope, kind="ci_identity", principal_id=first, account_id=account_id
    )
    attribute_principal(
        scope, kind="session", principal_id=second, account_id=account_id
    )
    assert traffic_principal(scope) == TrafficPrincipal(
        kind="ci_identity", principal_id=first, account_id=account_id
    )
    assert traffic_principal({"type": "http"}).kind == "anonymous"


# -- through the middleware, with real API-key authentication ---------------


@pytest.fixture
def traffic_app(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, list[TrafficSample]]:
    samples: list[TrafficSample] = []

    class _Recorder:
        def record(self, sample: TrafficSample) -> None:
            samples.append(sample)

    monkeypatch.setattr(
        api_traffic_recorder, "get_api_traffic_recorder", lambda: _Recorder()
    )
    monkeypatch.setattr("preloop.api.app.record_api_usage", lambda record: True)

    app = fastapi.FastAPI()
    app.dependency_overrides[get_db_session] = lambda: db_session

    @app.get("/api/v1/things/{thing_id}")
    def read_thing(
        thing_id: str, user: models.User = fastapi.Depends(get_current_user)
    ) -> dict[str, str]:
        if thing_id == "missing":
            raise fastapi.HTTPException(status_code=404, detail="no")
        if thing_id == "crash":
            raise RuntimeError("boom")
        return {"id": thing_id}

    app.add_middleware(ApiUsageMiddleware)
    return TestClient(app, raise_server_exceptions=False), samples


def test_api_key_requests_and_errors_are_attributed_by_template(
    db_session: Session,
    test_user: models.User,
    traffic_app: tuple[TestClient, list[TrafficSample]],
) -> None:
    client, samples = traffic_app
    key, token = crud_api_key.create_runtime_key(
        db_session,
        name="Synthetic key",
        account_id=test_user.account_id,
        user_id=test_user.id,
        key_value=f"key{uuid4().hex}",
    )
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/v1/things/abc?secret=1", headers=headers).status_code == 200
    assert client.get("/api/v1/things/missing", headers=headers).status_code == 404
    assert client.get("/api/v1/things/crash", headers=headers).status_code == 500
    assert (
        client.get(
            "/api/v1/things/abc", headers={"Authorization": "Bearer nope"}
        ).status_code
        == 401
    )
    assert client.get("/api/v1/nowhere/123").status_code == 404

    assert [s.status_code for s in samples] == [200, 404, 500, 401, 404]
    for sample in samples[:3]:
        assert sample.principal == TrafficPrincipal(
            kind="api_key", principal_id=key.id, account_id=test_user.account_id
        )
        assert sample.route_template == "/api/v1/things/{thing_id}"
    assert samples[3].principal.kind == "anonymous"
    assert samples[4].route_template == "<unmatched>"
    assert all(
        "abc" not in s.route_template and "secret" not in s.route_template
        for s in samples
    )


def test_runtime_keys_are_told_apart_from_account_keys(
    db_session: Session,
    test_user: models.User,
    traffic_app: tuple[TestClient, list[TrafficSample]],
) -> None:
    client, samples = traffic_app
    _, token = crud_api_key.create_runtime_key(
        db_session,
        name="Synthetic run key",
        account_id=test_user.account_id,
        user_id=test_user.id,
        key_value=f"key{uuid4().hex}",
        context_data={"flow_execution_id": str(uuid4())},
    )
    client.get("/api/v1/things/abc", headers={"Authorization": f"Bearer {token}"})
    assert samples[0].principal.kind == "runtime_key"


def test_usage_middleware_wraps_the_ci_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recording sits outside CI auth so it sees CI rejections too."""
    from preloop.api.app import create_app
    from preloop.api.middleware.ci_auth import RestrictedCiAuthMiddleware

    monkeypatch.setenv("TESTING", "false")
    app = create_app()
    order = [entry.cls for entry in app.user_middleware]
    assert order.index(ApiUsageMiddleware) < order.index(RestrictedCiAuthMiddleware)


def test_a_long_method_is_truncated_and_flushes(
    db_session: Session, test_user: models.User
) -> None:
    """An 11-character method must not make every flush fail."""
    captured: list[tuple[dict, list]] = []

    def _write(rollups: dict, events: list) -> None:
        crud_api_traffic.add_rollups(
            db_session,
            rows=[
                {
                    "bucket_start": key[0],
                    "account_id": key[1],
                    "principal_kind": key[2],
                    "method": key[3],
                    "route_template": key[4],
                    "status_class": key[5],
                    "request_count": c.request_count,
                    "total_duration_ms": c.total_duration_ms,
                    "max_duration_ms": c.max_duration_ms,
                }
                for key, c in rollups.items()
            ],
        )
        crud_api_traffic.add_events(
            db_session, events=[api_traffic_recorder.event_row(e) for e in events]
        )
        captured.append((rollups, events))

    recorder = _recorder(_write)  # type: ignore[arg-type]
    recorder.record(
        _sample(
            405,
            method="UNSUBSCRIBE",
            principal=TrafficPrincipal(kind="session", account_id=test_user.account_id),
        )
    )
    assert recorder.flush() is True
    event = crud_api_traffic.latest_event(db_session, account_id=test_user.account_id)
    assert event is not None and event.method == "UNSUBSCRIB"


def test_a_batch_that_keeps_failing_is_dropped_not_retried_forever() -> None:
    writer = _Capture(fail=3)
    recorder = _recorder(writer)
    recorder.record(_sample(500))
    assert [recorder.flush() for _ in range(3)] == [False, False, False]
    assert recorder.stats()["dropped"] == 2  # one event, one rollup request
    recorder.record(_sample(200))
    assert recorder.flush() is True
    [(rollups, events)] = writer.flushes
    assert events == [] and [c.request_count for c in rollups.values()] == [1]
