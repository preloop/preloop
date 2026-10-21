"""PostgreSQL-backed Fleet Map usage totals and authorization."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints.fleet_map import router
from preloop.models import models
from preloop.models.db.session import get_db_session
from preloop.services import fleet_usage
from preloop.services.fleet_map import build_fleet_map
from preloop.services.fleet_usage import build_fleet_usage

NOW = datetime(2026, 10, 20, 10, tzinfo=UTC)


def _model(
    db: Session, account_id: Any, identifier: str, provider: str
) -> models.AIModel:
    row = models.AIModel(
        account_id=account_id,
        name="Example model configuration",
        model_identifier=identifier,
        provider_name=provider,
    )
    db.add(row)
    db.flush()
    return row


def _usage(db: Session, account_id: Any, model: models.AIModel, **changes: Any) -> None:
    values: dict[str, Any] = {
        "account_id": account_id,
        "ai_model_id": model.id,
        "endpoint": "/v1/chat/completions",
        "method": "POST",
        "status_code": 200,
        "duration": 0.1,
        "action_type": "model_gateway",
        "timestamp": NOW.replace(tzinfo=None) - timedelta(minutes=1),
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "cache_read_tokens": 10,
        "cache_creation_tokens": 5,
        "estimated_cost": 0.01,
        "provider_name": "display alias must not be a graph key",
        "model_alias": "display model alias",
    }
    db.add(models.ApiUsage(**(values | changes)))
    db.flush()


@pytest.fixture
def fleet(db_session: Session, test_user: models.User) -> dict[str, Any]:
    """Multiple configurations and providers of the same canonical model."""
    test_user.is_superuser = True
    a = _model(
        db_session, test_user.account_id, "vendor/example-model-20261001", "openai"
    )
    b = _model(db_session, test_user.account_id, "example-model:latest", "openai")
    c = _model(db_session, test_user.account_id, "example-model", "openrouter")
    d = _model(db_session, test_user.account_id, "another-model", "openai")
    return {"user": test_user, "a": a, "b": b, "c": c, "d": d}


def _read(db: Session, user: models.User, minutes: int | None = 60) -> Any:
    return build_fleet_usage(db, user=user, active_within_minutes=minutes, now=NOW)


def test_totals_per_canonical_model_and_provider(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    for key in ("a", "b", "c", "d"):
        _usage(db_session, fleet["user"].account_id, fleet[key])
    result = _read(db_session, fleet["user"])
    assert set(result.models) == {"model:example-model", "model:another-model"}
    assert set(result.providers) == {"provider:openai", "provider:openrouter"}
    assert result.models["model:example-model"].model_dump() == {
        "request_count": 3,
        "input_tokens": 300,
        "output_tokens": 60,
        "cache_read_tokens": 30,
        "cache_write_tokens": 15,
        "estimated_cost": 0.03,
        "cost_is_estimate": True,
        "currency": "USD",
        "partial": False,
    }
    assert result.providers["provider:openai"].request_count == 3
    assert result.providers["provider:openai"].input_tokens == 300
    assert result.providers["provider:openrouter"].request_count == 1
    assert result.active_within_minutes == 60
    assert result.since == NOW - timedelta(hours=1)
    assert result.until == NOW
    assert not result.truncated and not result.partial
    graph = build_fleet_map(db_session, user=fleet["user"])
    graph_ids = {node.id for node in graph.nodes}
    assert set(result.models) | set(result.providers) <= graph_ids


def test_window_boundaries_and_replay_exclusion(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    account, model = fleet["user"].account_id, fleet["a"]
    for stamp in (NOW - timedelta(hours=1), NOW):
        _usage(db_session, account, model, timestamp=stamp.replace(tzinfo=None))
    for stamp in (
        NOW - timedelta(hours=1, microseconds=1),
        NOW + timedelta(microseconds=1),
    ):
        _usage(db_session, account, model, timestamp=stamp.replace(tzinfo=None))
    _usage(db_session, account, model, meta_data={"purpose": "replay_validation"})
    _usage(db_session, account, model, action_type="imported_usage")
    _usage(db_session, account, model, meta_data={"purpose": None})
    assert (
        _read(db_session, fleet["user"]).models["model:example-model"].request_count
        == 3
    )


@pytest.mark.parametrize("minutes", [None, 129600, 527040])
def test_any_and_large_windows_are_bounded_to_90_days(
    db_session: Session, fleet: dict[str, Any], minutes: int | None
) -> None:
    for days in (89, 91):
        _usage(
            db_session,
            fleet["user"].account_id,
            fleet["a"],
            timestamp=(NOW - timedelta(days=days)).replace(tzinfo=None),
        )
    result = _read(db_session, fleet["user"], minutes)
    assert result.active_within_minutes == minutes
    assert result.since == NOW - timedelta(days=90)
    assert result.models["model:example-model"].request_count == 1


def test_account_isolation(db_session: Session, fleet: dict[str, Any]) -> None:
    other = models.Account(
        organization_name="Other example organization", is_active=True
    )
    db_session.add(other)
    db_session.flush()
    foreign = _model(db_session, other.id, "example-model", "openai")
    _usage(db_session, other.id, fleet["a"])
    # A malformed cross-account reference must not make a foreign model visible.
    _usage(db_session, fleet["user"].account_id, foreign)
    assert _read(db_session, fleet["user"]).models == {}
    assert _read(db_session, fleet["user"]).providers == {}


def test_empty_window(db_session: Session, fleet: dict[str, Any]) -> None:
    result = _read(db_session, fleet["user"])
    assert result.models == {} and result.providers == {}
    assert not result.partial and not result.truncated


def test_unknown_values_and_mixed_currencies_are_honest(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    _usage(db_session, fleet["user"].account_id, fleet["a"])
    _usage(
        db_session,
        fleet["user"].account_id,
        fleet["b"],
        prompt_tokens=None,
        completion_tokens=None,
        estimated_cost=None,
        cache_read_tokens=None,
        cache_creation_tokens=None,
    )
    _usage(db_session, fleet["user"].account_id, fleet["c"], currency="EUR")
    result = _read(db_session, fleet["user"])
    assert result.partial
    assert result.models["model:example-model"].estimated_cost is None
    assert result.models["model:example-model"].currency is None
    assert result.providers["provider:openai"].estimated_cost == 0.01
    assert result.providers["provider:openai"].partial
    assert result.providers["provider:openrouter"].currency == "EUR"
    assert not result.providers["provider:openrouter"].partial


def test_cap_marks_every_total_partial(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    for key in ("a", "b", "c", "d"):
        _usage(db_session, fleet["user"].account_id, fleet[key])
    with patch.object(fleet_usage, "MODEL_LIMIT", 1):
        result = _read(db_session, fleet["user"])
    assert result.truncated and result.partial
    assert all(
        total.partial for total in [*result.models.values(), *result.providers.values()]
    )
    assert sum(total.request_count for total in result.models.values()) == 1


def test_model_visibility_applies_before_aggregation(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    _usage(db_session, fleet["user"].account_id, fleet["a"])
    _usage(db_session, fleet["user"].account_id, fleet["c"])
    with patch.object(fleet_usage, "filter_viewable", return_value=[fleet["a"]]):
        result = _read(db_session, fleet["user"])
    assert result.models["model:example-model"].request_count == 1
    assert set(result.providers) == {"provider:openai"}


def test_one_sql_aggregate_per_grouping(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    _usage(db_session, fleet["user"].account_id, fleet["a"])
    statements: list[str] = []

    def record(
        conn: Any, cursor: Any, sql: str, params: Any, ctx: Any, many: bool
    ) -> None:
        if "api_usage" in sql:
            statements.append(sql)

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", record)
    try:
        _read(db_session, fleet["user"])
    finally:
        event.remove(connection, "before_cursor_execute", record)
    assert len(statements) == 2
    assert all("GROUP BY" in sql for sql in statements)
    assert all(
        "'model_gateway'" in sql and "'replay_validation'" in sql for sql in statements
    )


def test_route_window_validation_and_permission(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_current_active_user] = lambda: fleet["user"]
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as client:
        for window in ("0", "527041", "any", "-1"):
            assert (
                client.get(
                    f"/api/v1/fleet/usage?active_within_minutes={window}"
                ).status_code
                == 422
            )
        with patch.object(fleet_usage, "_allowed", return_value=False):
            assert client.get("/api/v1/fleet/usage").status_code == 403
        response = client.get("/api/v1/fleet/usage?active_within_minutes=5")
        assert response.status_code == 200
        assert response.json()["active_within_minutes"] == 5


def test_report_only_credentials_are_denied(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    fleet["user"]._auth_api_key = MagicMock(context_data={"device_report": True})
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_current_active_user] = lambda: fleet["user"]
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as client:
        assert client.get("/api/v1/fleet/usage").status_code == 403


def test_missing_values_stay_null_and_failed_requests_count(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    _usage(
        db_session,
        fleet["user"].account_id,
        fleet["a"],
        status_code=502,
        prompt_tokens=None,
        completion_tokens=None,
        estimated_cost=None,
        cache_read_tokens=None,
        cache_creation_tokens=None,
    )
    result = _read(db_session, fleet["user"])
    total = result.models["model:example-model"]
    assert total.request_count == 1
    assert total.input_tokens is None and total.output_tokens is None
    assert total.cache_read_tokens is None and total.cache_write_tokens is None
    assert total.estimated_cost is None
    assert total.partial and result.partial


def test_without_model_read_permission_totals_are_empty(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    _usage(db_session, fleet["user"].account_id, fleet["a"])
    with patch.object(
        fleet_usage,
        "_allowed",
        side_effect=lambda db, user, permission, owner: (
            permission == "view_device_fleet"
        ),
    ):
        result = _read(db_session, fleet["user"])
    assert result.models == {} and result.providers == {}


def test_built_in_models_match_graph_visibility_and_tenant_usage(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    hosted = _model(db_session, None, "example-hosted-model", "hosted")
    hosted.api_key = "example-operator-secret"
    hosted.meta_data = {
        "gateway": {"enabled": True, "model_alias": "example/operator-model"}
    }
    _usage(db_session, fleet["user"].account_id, hosted)
    with patch.object(fleet_usage, "built_in_models_enabled", return_value=False):
        assert _read(db_session, fleet["user"]).models == {}
    with (
        patch.object(fleet_usage, "built_in_models_enabled", return_value=True),
        patch.object(fleet_usage, "filter_viewable", return_value=[]),
    ):
        result = _read(db_session, fleet["user"])
    assert set(result.models) == {"model:example-hosted-model"}
    assert set(result.providers) == {"provider:hosted"}
    assert "example-operator-secret" not in result.model_dump_json()


def test_provider_account_sharing_does_not_duplicate_ledger_rows(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    secret = models.SecretReference(
        account_id=fleet["user"].account_id,
        name="Example shared credential",
        backend_type="local",
        secret_kind="ai_model_credentials",
        encrypted_value="example-encrypted-value",
    )
    db_session.add(secret)
    db_session.flush()
    for key in ("a", "b"):
        fleet[key].credentials_secret_id = secret.id
        _usage(db_session, fleet["user"].account_id, fleet[key])
    result = _read(db_session, fleet["user"])
    assert result.models["model:example-model"].request_count == 2
    assert result.providers["provider:openai"].request_count == 2


def test_gateway_indexes_support_both_aggregate_queries(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    """The literal gateway/replay predicates permit indexed window scans."""
    from sqlalchemy import text

    _usage(db_session, fleet["user"].account_id, fleet["a"])
    queries: list[tuple[str, Any]] = []

    def record(
        conn: Any, cursor: Any, sql: str, params: Any, ctx: Any, many: bool
    ) -> None:
        if "api_usage" in sql:
            queries.append((sql, params))

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", record)
    try:
        _read(db_session, fleet["user"])
    finally:
        event.remove(connection, "before_cursor_execute", record)
    db_session.execute(text("SET LOCAL enable_seqscan = off"))
    for sql, params in queries:
        plan = "\n".join(connection.exec_driver_sql("EXPLAIN " + sql, params).scalars())
        # Small fixtures can favor the narrower gateway model/time index over
        # a totals index. Require indexed tenant/window bounds, not one name.
        assert "Index Scan" in plan, plan
        assert any(
            "Index Cond:" in line and "account_id" in line and "timestamp" in line
            for line in plan.splitlines()
        ), plan


@pytest.mark.parametrize(
    "pricing",
    [
        {"cost_source": "unpriced"},
        {"meta_data": {"budget": {"pricing_available": False}}},
    ],
)
def test_explicitly_unpriced_legacy_zero_cost_is_partial(
    db_session: Session, fleet: dict[str, Any], pricing: dict[str, Any]
) -> None:
    _usage(
        db_session, fleet["user"].account_id, fleet["a"], estimated_cost=0.0, **pricing
    )
    result = _read(db_session, fleet["user"])
    for total in (
        result.models["model:example-model"],
        result.providers["provider:openai"],
    ):
        assert total.partial
        assert total.estimated_cost is None
        assert total.request_count == 1
        assert total.input_tokens == 100
    assert result.partial
    # Keep the known cost when priced and explicitly unpriced rows mix.
    _usage(db_session, fleet["user"].account_id, fleet["b"])
    result = _read(db_session, fleet["user"])
    for total in (
        result.models["model:example-model"],
        result.providers["provider:openai"],
    ):
        assert total.partial
        assert total.estimated_cost == 0.01
        assert total.request_count == 2


@pytest.mark.parametrize(
    ("cost_source", "cost"),
    [
        ("provider", 0.02),
        ("provider", 0.0),
        ("reconciled", 0.02),
        ("imported", 0.02),
        ("catalog", 0.02),
        ("override", 0.0),
        (None, 0.02),
    ],
)
def test_resolved_cost_overrides_stale_preflight_pricing_marker(
    db_session: Session,
    fleet: dict[str, Any],
    cost_source: str | None,
    cost: float,
) -> None:
    _usage(
        db_session,
        fleet["user"].account_id,
        fleet["a"],
        cost_source=cost_source,
        estimated_cost=cost,
        meta_data={"budget": {"pricing_available": False}},
    )
    result = _read(db_session, fleet["user"])
    for total in (
        result.models["model:example-model"],
        result.providers["provider:openai"],
    ):
        assert total.estimated_cost == cost
        assert not total.partial
    assert not result.partial
