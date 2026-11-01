"""Weekly report evidence and delivery preference regressions."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.weekly_report import preferences, update_preferences
from preloop.models.crud.weekly_report_evidence import usage_rows
from preloop.services.weekly_report_evidence import spend_evidence, tool_evidence
from preloop.schemas.weekly_report import WeeklyReportPreferencesUpdate
from preloop.api.endpoints.account import put_weekly_report_preferences

START = datetime(2026, 10, 5, tzinfo=UTC)
END = START + timedelta(days=7)


def row(**values: object) -> SimpleNamespace:
    """A metadata projection with no transcript."""
    return SimpleNamespace(
        **(
            dict(
                runtime_session_id=UUID(int=1),
                user_id=UUID(int=2),
                timestamp=START + timedelta(days=2),
                estimated_cost=22.39,
                ai_model_id=None,
                model_alias="example-model",
                prompt_tokens=1000,
                cost_source="subscription",
                tools_meta=[
                    {"name": "mcp__tracker__search", "schema_tokens_estimate": 100}
                ],
                title=None,
                started_at=START,
                agent_id=UUID(int=3),
                agent_name="Review agent",
                runtime_principal_name=None,
                flow_id=UUID(int=4),
                flow_name="Review",
            )
            | values
        )
    )


def test_daily_series_has_zero_days_and_linked_contributors() -> None:
    evidence = spend_evidence(
        [row(), row(estimated_cost=1.0), row(user_id=UUID(int=99), estimated_cost=999)],
        day="2026-10-07",
        user_id=str(UUID(int=2)),
        start=START,
        end=END,
        details={"spend_usd": 23.39, "median_usd": 2.64, "multiple": 8.86},
    )
    assert len(evidence["daily_series"]) == 7
    assert sum(d["is_outlier"] for d in evidence["daily_series"]) == 1
    assert evidence["daily_series"][0]["cost"] == 0
    session = evidence["contributing_sessions"][0]
    assert session["cost"] == 23.39
    assert session["title"].startswith("Review agent")
    assert session["href"].endswith(f"sessionId={UUID(int=1)}")
    assert session["agent_href"].endswith(str(UUID(int=3)))
    assert session["flow_href"].endswith(str(UUID(int=4)))
    assert session["flow_name"] == "Review"


def test_tool_cost_is_per_tool_and_unknown_stays_unknown() -> None:
    tools = tool_evidence(
        None,
        account_id=UUID(int=9),
        rows=[row(), row(), row(tools_meta=[{"name": "mcp__tracker__create"}])],
    )
    assert set(tools) == {"mcp__tracker__search", "mcp__tracker__create"}
    search = tools["mcp__tracker__search"]
    assert search["estimated_weekly_tokens"] == 200
    assert search["schema_tokens_per_request"] == 100
    assert search["estimated_weekly_savings"] == 0
    assert tools["mcp__tracker__create"]["estimated_weekly_tokens"] is None
    assert tools["mcp__tracker__create"]["estimated_weekly_savings"] is None
    unknown = tool_evidence(None, account_id=UUID(int=9), rows=[row(cost_source=None)])
    assert unknown["mcp__tracker__search"]["estimated_weekly_savings"] is None


def test_preferences_default_on_and_changes_are_audited(
    db_session: Session, test_user: models.User
) -> None:
    account = db_session.get(models.Account, test_user.account_id)
    assert preferences(db_session, account=account, user=test_user) == {
        "account_enabled": True,
        "tracking_enabled": True,
        "user_enabled": True,
        "members_enabled": False,
        "member_policy_findings_enabled": False,
        "synthesis_enabled": False,
        "synthesis_model_id": None,
    }
    assert update_preferences(
        db_session,
        account_id=account.id,
        user=test_user,
        account_enabled=False,
        user_enabled=False,
    ) == {
        "account_enabled": False,
        "tracking_enabled": True,
        "user_enabled": False,
        "members_enabled": False,
        "member_policy_findings_enabled": False,
        "synthesis_enabled": False,
        "synthesis_model_id": None,
    }
    logs = (
        db_session.query(models.AuditLog)
        .filter_by(account_id=account.id, action="weekly_report_preferences_updated")
        .all()
    )
    assert len(logs) == 2
    assert {log.details["field"] for log in logs} == {"account_enabled", "user_enabled"}
    update_preferences(
        db_session,
        account_id=account.id,
        user=test_user,
        account_enabled=False,
        user_enabled=None,
    )
    assert (
        db_session.query(models.AuditLog)
        .filter_by(account_id=account.id, action="weekly_report_preferences_updated")
        .count()
        == 2
    )


def test_member_can_unsubscribe_but_cannot_disable_account(
    db_session: Session, test_user: models.User
) -> None:
    account = db_session.get(models.Account, test_user.account_id)
    with patch("preloop.utils.permissions.user_permission_names", return_value=[]):
        with pytest.raises(HTTPException) as error:
            put_weekly_report_preferences(
                WeeklyReportPreferencesUpdate(account_enabled=False),
                account,
                test_user,
                db_session,
            )
        assert error.value.status_code == 403
        result = put_weekly_report_preferences(
            WeeklyReportPreferencesUpdate(user_enabled=False),
            account,
            test_user,
            db_session,
        )
    assert result.account_enabled is True and result.user_enabled is False


def test_projection_scope_and_window(
    db_session: Session, test_user: models.User
) -> None:
    for timestamp in (START, END):
        db_session.add(
            models.ApiUsage(
                account_id=test_user.account_id,
                user_id=test_user.id,
                timestamp=timestamp.replace(tzinfo=None),
                endpoint="/v1/messages",
                method="POST",
                status_code=200,
                duration=1,
                action_type="model_gateway",
                estimated_cost=2,
                meta_data={"tools_meta": []},
            )
        )
    db_session.flush()
    rows = usage_rows(db_session, account_id=test_user.account_id, start=START, end=END)
    assert len(rows) == 1 and rows[0].estimated_cost == 2
    assert usage_rows(db_session, account_id=UUID(int=42), start=START, end=END) == []


def test_imported_costs_are_included_without_inventing_sessions() -> None:
    imported = SimpleNamespace(
        user_id=UUID(int=2), day=(START + timedelta(days=2)).date(), cost_usd=3
    )
    evidence = spend_evidence(
        [row()],
        day="2026-10-07",
        user_id=str(UUID(int=2)),
        start=START,
        end=END,
        details={"spend_usd": 25.39},
        imported_rows=[imported],
        imported_complete=False,
    )
    assert evidence["daily_series"][2]["cost"] == 25.39
    assert evidence["daily_series"][2]["imported_cost"] == 3
    assert evidence["contributing_sessions"][0]["cost"] == 22.39
    assert evidence["imported_complete"] is False


def test_imported_chart_rows_follow_evaluator_validation() -> None:
    from preloop.services.spend_outliers import (
        ImportedSpendRow,
        imported_spend_for_report,
    )

    good = ImportedSpendRow(
        user_id=UUID(int=1),
        day=START.date(),
        model="example-model",
        cost_usd=1,
        source="example",
    )
    rows = [
        good,
        ImportedSpendRow(
            user_id=good.user_id,
            day=good.day,
            model=good.model,
            cost_usd=-1,
            source="example",
        ),
        ImportedSpendRow(
            user_id=good.user_id,
            day=good.day,
            model=good.model,
            cost_usd=float("nan"),
            source="example",
        ),
        ImportedSpendRow(
            user_id=good.user_id,
            day=END.date(),
            model=good.model,
            cost_usd=1,
            source="example",
        ),
    ]
    with patch(
        "preloop.services.spend_outliers._imported_rows", return_value=(rows, False)
    ):
        selected, complete = imported_spend_for_report(
            None, account_id=UUID(int=1), start_day=START.date(), end_day=START.date()
        )
    assert selected == [good] and complete is False


def test_contributor_prefers_explicit_agent_over_legacy_session_association(
    db_session: Session, test_user: models.User
) -> None:
    session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="test",
        session_source_id="report-attribution",
        started_at=START.replace(tzinfo=None),
    )
    db_session.add(session)
    db_session.flush()
    for identifier, source, linked in [
        (UUID(int=10), "legacy", session.id),
        (UUID(int=20), "direct", None),
    ]:
        db_session.add(
            models.ManagedAgent(
                id=identifier,
                account_id=test_user.account_id,
                agent_kind="codex",
                session_source_type="test",
                session_source_id=source,
                display_name=source,
                runtime_session_id=linked,
                lifecycle_updated_at=START.replace(tzinfo=None),
                last_seen_at=START.replace(tzinfo=None),
            )
        )
    for minute in range(2):
        db_session.add(
            models.ApiUsage(
                account_id=test_user.account_id,
                runtime_session_id=session.id,
                runtime_principal_type="managed_agent",
                runtime_principal_id=str(UUID(int=20)),
                timestamp=(START + timedelta(minutes=minute)).replace(tzinfo=None),
                endpoint="/v1/messages",
                method="POST",
                status_code=200,
                duration=1,
                action_type="model_gateway",
                estimated_cost=2,
                cost_source="subscription",
                meta_data={
                    "tools_meta": [
                        {"name": "mcp__tracker__search", "schema_tokens_estimate": 100}
                    ]
                },
            )
        )
    db_session.flush()
    projected = usage_rows(
        db_session, account_id=test_user.account_id, start=START, end=END
    )
    assert len(projected) == 1
    assert projected[0].agent_id == UUID(int=20)
    assert projected[0].agent_name == "direct"
    assert projected[0].request_count == 2
    assert projected[0].estimated_cost == 4
    tool = tool_evidence(db_session, account_id=test_user.account_id, rows=projected)[
        "mcp__tracker__search"
    ]
    assert tool["request_count"] == 2
    assert tool["estimated_weekly_tokens"] == 200


def test_aggregated_tool_requests_keep_measured_tokens_and_cost() -> None:
    tools = tool_evidence(None, account_id=UUID(int=9), rows=[row(request_count=17)])
    search = tools["mcp__tracker__search"]
    assert search["request_count"] == 17
    assert search["estimated_weekly_tokens"] == 1700
    assert search["schema_tokens_per_request"] == 100
    assert search["estimated_weekly_savings"] == 0
