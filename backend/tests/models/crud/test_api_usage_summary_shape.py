"""Parity of aggregation-first session and day queries against raw rows.

Expected totals are computed in Python from the inserted facts. The queries
must match that oracle for empty windows, UTC day edges, tenant isolation,
priced and unpriced and failed rows, retry and replay handling, native and
legacy attribution, a per-user filter, and breakdown order plus limit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from preloop.models.crud import (
    crud_account,
    crud_api_usage,
    crud_flow,
    crud_flow_execution,
    crud_runtime_session,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate

START = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
END = datetime(2026, 3, 3, 12, 0, tzinfo=UTC)


def _log(db_session, **kwargs: Any):
    when = kwargs.pop("when")
    usage = crud_api_usage.log_gateway_request(db_session, duration=0.1, **kwargs)
    usage.timestamp = when
    db_session.flush()
    return usage


def _oracle(facts: list[dict[str, Any]], *, principal: Optional[str] = None) -> dict:
    """Fold fixture facts the way the usage queries account for them."""
    included = []
    for fact in facts:
        if fact["when"] < START or fact["when"] >= END:
            continue
        if fact.get("replay"):
            continue
        if principal is not None and fact.get("runtime_principal_id") != principal:
            continue
        included.append(fact)

    days: dict[str, dict[str, float]] = {}
    for fact in included:
        day = fact["when"].date().isoformat()
        bucket = days.setdefault(
            day,
            {"request_count": 0, "total_tokens": 0, "estimated_cost": 0.0},
        )
        bucket["request_count"] += 1
        bucket["total_tokens"] += fact["total_tokens"]
        if fact["estimated_cost"] is not None:
            bucket["estimated_cost"] += fact["estimated_cost"]

    sessions: dict[tuple, dict[str, Any]] = {}
    for fact in included:
        if not fact.get("in_session_breakdown"):
            continue
        key = (
            fact.get("runtime_session_id"),
            fact.get("model_alias"),
            fact.get("flow_execution_id"),
            fact.get("resolved_principal_id"),
        )
        group = sessions.setdefault(
            key,
            {
                "request_count": 0,
                "total_tokens": 0,
                "estimated_cost": 0.0,
                "last_request_at": fact["when"],
                "model_alias": fact.get("model_alias"),
                "runtime_session_id": fact.get("runtime_session_id"),
                "session_source_type": fact.get("session_source_type"),
                "resolved_principal_id": fact.get("resolved_principal_id"),
                "flow_name": fact.get("flow_name"),
            },
        )
        group["request_count"] += 1
        group["total_tokens"] += fact["total_tokens"]
        if fact["estimated_cost"] is not None:
            group["estimated_cost"] += fact["estimated_cost"]
        if fact["when"] > group["last_request_at"]:
            group["last_request_at"] = fact["when"]

    ordered = sorted(
        sessions.values(),
        key=lambda row: (row["last_request_at"], row["request_count"]),
        reverse=True,
    )
    totals_all = [fact for fact in included if not fact.get("is_retry")]
    return {
        "days": days,
        "sessions": ordered,
        "request_count": len(included),
        "request_count_without_retries": len(totals_all),
        "error_count": sum(1 for fact in included if fact["status_code"] >= 400),
        "unpriced_requests": sum(
            1
            for fact in included
            if fact["estimated_cost"] is None and fact["total_tokens"] > 0
        ),
        "total_tokens": sum(fact["total_tokens"] for fact in included),
    }


def test_summary_shapes_match_raw_accounting(db_session, test_user) -> None:
    """Session and day aggregates equal a Python fold of the fixture rows."""
    account_id = str(test_user.account_id)
    other = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other tenant", "is_active": True},
    )
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Legacy Flow",
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(
            flow_id=flow.id,
            status="SUCCEEDED",
            agent_session_reference="legacy-ref",
        ),
    )
    native = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="custom",
        session_source_id="native-source",
        session_reference="native-ref",
        runtime_principal_type="custom",
        runtime_principal_id="session-principal",
        runtime_principal_name="Session Principal",
        started_at=START,
        last_activity_at=START,
    )
    native.title = "Native session"
    db_session.flush()
    other_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="custom",
        session_source_id="other-source",
        runtime_principal_type="custom",
        runtime_principal_id="other-principal",
        runtime_principal_name="Other",
        started_at=START,
        last_activity_at=START,
    )
    quiet = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="custom",
        session_source_id="quiet-source",
        runtime_principal_type="custom",
        runtime_principal_id="quiet-principal",
        runtime_principal_name="Quiet",
        started_at=START,
        last_activity_at=START,
    )

    def base(**overrides: Any) -> dict[str, Any]:
        payload = {
            "endpoint": "/v1/chat/completions",
            "method": "POST",
            "status_code": 200,
            "user_id": str(test_user.id),
            "account_id": account_id,
            "model_alias": "example-model",
            "provider_name": "openai",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "estimated_cost": 0.1,
            "in_session_breakdown": True,
        }
        payload.update(overrides)
        return payload

    specs = [
        base(
            when=START,
            runtime_session_id=str(native.id),
            runtime_principal_id="row-principal-a",
            runtime_principal_type="custom",
            resolved_principal_id="session-principal",
            session_source_type="custom",
            total_tokens=15,
        ),
        base(
            when=START + timedelta(hours=1),
            runtime_session_id=str(native.id),
            runtime_principal_id="row-principal-b",
            runtime_principal_type="custom",
            resolved_principal_id="session-principal",
            session_source_type="custom",
            status_code=500,
            estimated_cost=0.2,
            total_tokens=20,
            prompt_tokens=20,
            completion_tokens=0,
        ),
        base(
            when=datetime(2026, 3, 2, 0, 0, tzinfo=UTC),
            runtime_session_id=str(native.id),
            runtime_principal_id="session-principal",
            resolved_principal_id="session-principal",
            session_source_type="custom",
            model_alias="example-unpriced",
            estimated_cost=None,
            total_tokens=7,
            prompt_tokens=7,
            completion_tokens=0,
        ),
        base(
            when=START - timedelta(seconds=1),
            runtime_session_id=str(native.id),
            runtime_principal_id="session-principal",
            resolved_principal_id="session-principal",
            session_source_type="custom",
            total_tokens=99,
        ),
        base(
            when=END,
            runtime_session_id=str(native.id),
            runtime_principal_id="session-principal",
            resolved_principal_id="session-principal",
            session_source_type="custom",
            total_tokens=99,
        ),
        base(
            when=datetime(2026, 3, 2, 8, 0, tzinfo=UTC),
            runtime_session_id=str(native.id),
            runtime_principal_id="session-principal",
            resolved_principal_id="session-principal",
            session_source_type="custom",
            is_retry=True,
            total_tokens=4,
            estimated_cost=0.04,
        ),
        base(
            when=datetime(2026, 3, 2, 9, 0, tzinfo=UTC),
            runtime_session_id=str(native.id),
            runtime_principal_id="session-principal",
            resolved_principal_id="session-principal",
            session_source_type="custom",
            replay=True,
            meta_data={"purpose": "replay_validation"},
            total_tokens=50,
            estimated_cost=1.0,
        ),
        base(
            when=datetime(2026, 3, 2, 10, 0, tzinfo=UTC),
            flow_id=str(flow.id),
            flow_execution_id=str(execution.id),
            runtime_principal_id="legacy-principal",
            resolved_principal_id="legacy-principal",
            session_source_type="flow_execution",
            flow_name="Legacy Flow",
            model_alias="example-legacy",
            total_tokens=11,
            estimated_cost=0.11,
        ),
        base(
            when=datetime(2026, 3, 2, 18, 0, tzinfo=UTC),
            runtime_session_id=str(other_session.id),
            runtime_principal_id="other-principal",
            resolved_principal_id="other-principal",
            session_source_type="custom",
            model_alias="example-other",
            total_tokens=3,
            estimated_cost=0.03,
        ),
        base(
            when=datetime(2026, 3, 1, 13, 0, tzinfo=UTC),
            runtime_session_id=str(quiet.id),
            runtime_principal_id="quiet-principal",
            resolved_principal_id="quiet-principal",
            session_source_type="custom",
            model_alias="example-quiet",
            total_tokens=1,
            estimated_cost=0.01,
        ),
        base(
            when=datetime(2026, 3, 2, 11, 0, tzinfo=UTC),
            in_session_breakdown=False,
            runtime_principal_id="session-principal",
            total_tokens=6,
            estimated_cost=0.06,
            model_alias="example-unscoped",
        ),
        base(
            when=datetime(2026, 3, 2, 12, 0, tzinfo=UTC),
            account_id=str(other.id),
            user_id=None,
            runtime_session_id=None,
            in_session_breakdown=False,
            runtime_principal_id="session-principal",
            total_tokens=1000,
            estimated_cost=9.0,
        ),
    ]
    # The other tenant needs its own session row so a mistaken join cannot
    # pull it in through a shared session id. It stays out of the oracle
    # because the account filter drops it before grouping.
    other_runtime = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=other.id,
        session_source_type="custom",
        session_source_id="foreign-source",
        runtime_principal_type="custom",
        runtime_principal_id="session-principal",
        started_at=START,
        last_activity_at=START,
    )
    specs[-1]["runtime_session_id"] = str(other_runtime.id)
    specs[-1]["in_session_breakdown"] = True
    specs[-1]["resolved_principal_id"] = "session-principal"
    specs[-1]["session_source_type"] = "custom"

    for spec in specs:
        kwargs = {
            key: value
            for key, value in spec.items()
            if key
            not in {
                "in_session_breakdown",
                "resolved_principal_id",
                "session_source_type",
                "flow_name",
                "replay",
            }
        }
        _log(db_session, **kwargs)
    db_session.commit()

    own_specs = [spec for spec in specs if spec["account_id"] == account_id]
    expected = _oracle(own_specs)
    vacant_start = datetime(2020, 1, 1, tzinfo=UTC)
    vacant_end = datetime(2020, 1, 2, tzinfo=UTC)
    empty = crud_api_usage.get_gateway_usage_by_session(
        db_session,
        account_id=account_id,
        start_date=vacant_start,
        end_date=vacant_end,
    )
    assert empty == []
    empty_days = crud_api_usage.get_gateway_usage_timeseries(
        db_session,
        account_id=account_id,
        start_date=vacant_start,
        end_date=vacant_end,
    )
    assert empty_days == []

    sessions = crud_api_usage.get_gateway_usage_by_session(
        db_session,
        account_id=account_id,
        start_date=START,
        end_date=END,
        limit=2,
    )
    assert len(sessions) == 2
    assert [row["request_count"] for row in sessions] == [
        group["request_count"] for group in expected["sessions"][:2]
    ]
    assert [row["total_tokens"] for row in sessions] == [
        group["total_tokens"] for group in expected["sessions"][:2]
    ]

    full_sessions = crud_api_usage.get_gateway_usage_by_session(
        db_session,
        account_id=account_id,
        start_date=START,
        end_date=END,
        limit=20,
    )
    assert len(full_sessions) == len(expected["sessions"])
    native_group = next(
        row
        for row in full_sessions
        if row["runtime_session_id"] == str(native.id)
        and row["model_alias"] == "example-model"
    )
    assert native_group["request_count"] == next(
        group["request_count"]
        for group in expected["sessions"]
        if group["runtime_session_id"] == str(native.id)
        and group["model_alias"] == "example-model"
    )
    assert native_group["runtime_principal_id"] == "session-principal"
    assert native_group["title"] == "Native session"
    assert native_group["session_source_type"] == "custom"
    legacy = next(row for row in full_sessions if row["flow_name"] == "Legacy Flow")
    assert legacy["runtime_session_id"] is None
    assert legacy["session_source_type"] == "flow_execution"
    assert legacy["session_source_id"] == str(execution.id)
    assert legacy["flow_id"] == str(flow.id)
    assert legacy["request_count"] == 1
    assert legacy["total_tokens"] == 11

    days = crud_api_usage.get_gateway_usage_timeseries(
        db_session,
        account_id=account_id,
        start_date=START,
        end_date=END,
    )
    assert [row["date"] for row in days] == sorted(expected["days"])
    for row in days:
        assert row["request_count"] == expected["days"][row["date"]]["request_count"]
        assert row["total_tokens"] == expected["days"][row["date"]]["total_tokens"]
        assert (
            abs(row["estimated_cost"] - expected["days"][row["date"]]["estimated_cost"])
            < 1e-9
        )

    totals = crud_api_usage.get_gateway_usage_summary(
        db_session,
        account_id=account_id,
        start_date=START,
        end_date=END,
    )
    assert totals["request_count"] == expected["request_count"]
    assert totals["error_count"] == expected["error_count"]
    assert totals["unpriced_requests"] == expected["unpriced_requests"]
    assert totals["total_tokens"] == expected["total_tokens"]
    without_retries = crud_api_usage.get_gateway_usage_summary(
        db_session,
        account_id=account_id,
        start_date=START,
        end_date=END,
        exclude_retries=True,
    )
    assert without_retries["request_count"] == expected["request_count_without_retries"]

    per_user = crud_api_usage.get_gateway_usage_by_session(
        db_session,
        account_id=account_id,
        start_date=START,
        end_date=END,
        runtime_principal_id="other-principal",
        limit=20,
    )
    per_user_expected = _oracle(specs, principal="other-principal")
    assert len(per_user) == len(per_user_expected["sessions"])
    assert per_user[0]["runtime_principal_id"] == "other-principal"
    assert (
        per_user[0]["request_count"]
        == per_user_expected["sessions"][0]["request_count"]
    )
    per_user_days = crud_api_usage.get_gateway_usage_timeseries(
        db_session,
        account_id=account_id,
        start_date=START,
        end_date=END,
        runtime_principal_id="other-principal",
    )
    assert (
        sum(row["request_count"] for row in per_user_days)
        == per_user_expected["request_count"]
    )
    foreign = crud_api_usage.get_gateway_usage_summary(
        db_session,
        account_id=str(other.id),
        start_date=START,
        end_date=END,
    )
    assert foreign["request_count"] == 1
    assert foreign["total_tokens"] == 1000
