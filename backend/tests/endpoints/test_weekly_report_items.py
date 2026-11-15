"""Report filters must not widen account, permission or personal scope."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterator, get_args
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.endpoints import cost
from preloop.schemas.weekly_report_items import ReportKind

ACCOUNT = UUID(int=100)
USER = UUID(int=101)
START = "2026-11-02T00:00:00.123456Z"
END = "2026-11-09T00:00:00.123456Z"


@pytest.fixture
def report_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Mock]]:
    """Mount the real router with a collector that records its exact scope."""
    account = SimpleNamespace(id=ACCOUNT)
    user = SimpleNamespace(id=USER, account_id=ACCOUNT, is_superuser=False)
    monkeypatch.setattr(cost, "get_account_or_404", lambda *args: account)
    monkeypatch.setattr(cost, "user_holds_permission", lambda *args: True)

    def collect(db: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "account_id": kwargs["account"].id,
            "kind": kwargs["kind"],
            "title": "Report items",
            "start_date": kwargs["start"],
            "end_date": kwargs["end"],
            "total": 1,
            "items": [{"label": "Example item"}],
        }

    collector = Mock(side_effect=collect)
    monkeypatch.setattr(
        cost,
        "get_plugin_manager",
        lambda: SimpleNamespace(
            get_service=lambda name: collector,
        ),
    )
    app = FastAPI()
    app.include_router(cost.router, prefix="/api/v1")
    app.dependency_overrides[cost.get_db_session] = lambda: object()
    app.dependency_overrides[cost.get_current_active_user] = lambda: user
    with TestClient(app) as client:
        yield client, collector


def query(**kwargs: Any) -> dict[str, Any]:
    """Return a generic synthetic report link's parameters."""
    return dict(
        kind="cache", account_id=str(ACCOUNT), start_date=START, end_date=END, **kwargs
    )


@pytest.mark.parametrize("kind", get_args(ReportKind))
def test_every_section_preserves_the_exact_window(
    report_client: tuple[TestClient, Mock],
    kind: str,
) -> None:
    client, collector = report_client
    params = query()
    params["kind"] = kind
    response = client.get("/api/v1/cost/report-items", params=params)
    assert response.status_code == 200
    assert response.json()["total"] == 1
    call = collector.call_args.kwargs
    assert call["start"] == datetime(
        2026, 11, 2, microsecond=123456, tzinfo=timezone.utc
    )
    assert call["end"] == datetime(2026, 11, 9, microsecond=123456, tzinfo=timezone.utc)
    assert call["account"].id == ACCOUNT


@pytest.mark.parametrize(
    ("field", "value", "status"),
    [
        ("account_id", str(UUID(int=999)), 409),
        ("user_id", str(UUID(int=999)), 403),
        ("kind", "unknown", 422),
        ("start_date", "2026-11-02T00:00:00", 422),
        ("end_date", START, 422),
        ("offset", "-1", 422),
        ("limit", "101", 422),
    ],
)
def test_invalid_or_cross_account_links_never_reach_collector(
    report_client: tuple[TestClient, Mock],
    field: str,
    value: str,
    status: int,
) -> None:
    client, collector = report_client
    params = {**query(), field: value}
    response = client.get("/api/v1/cost/report-items", params=params)
    assert response.status_code == status
    collector.assert_not_called()


def test_permission_denial_never_reads_report(
    report_client: tuple[TestClient, Mock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, collector = report_client
    monkeypatch.setattr(cost, "user_holds_permission", lambda *args: False)
    assert client.get("/api/v1/cost/report-items", params=query()).status_code == 403
    collector.assert_not_called()


def test_filters_and_personal_scope_reach_collector(
    report_client: tuple[TestClient, Mock],
) -> None:
    client, collector = report_client
    params = query(
        user_id=str(USER),
        model="example/model",
        agent="Example agent",
        flow="example-flow",
        status="pending",
        item="example-item",
        offset=50,
    )
    assert client.get("/api/v1/cost/report-items", params=params).status_code == 200
    call = collector.call_args.kwargs
    for name in ("model", "agent", "flow", "status", "item", "offset"):
        assert call[name] == params[name]
    assert call["user_id"] == USER


def test_missing_plugin_returns_explicit_unavailable(
    report_client: tuple[TestClient, Mock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, collector = report_client
    monkeypatch.setattr(
        cost,
        "get_plugin_manager",
        lambda: SimpleNamespace(get_service=lambda name: None),
    )
    assert client.get("/api/v1/cost/report-items", params=query()).status_code == 404
    collector.assert_not_called()
