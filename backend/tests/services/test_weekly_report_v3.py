"""Weekly report SQL scope and independently audited recipient settings."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.api.endpoints.account import put_weekly_report_preferences
from preloop.models import models
from preloop.models.crud.weekly_report import preferences, update_preferences
from preloop.models.crud.weekly_report_data import usage_snapshot
from preloop.schemas.weekly_report import WeeklyReportPreferencesUpdate

START = datetime(2026, 10, 5, tzinfo=UTC)
END = START + timedelta(days=7)


@pytest.mark.parametrize(
    "field", ["members_enabled", "member_policy_findings_enabled", "tracking_enabled"]
)
def test_member_cannot_change_account_report_controls(
    db_session: Session, test_user: models.User, field: str
) -> None:
    account = db_session.get(models.Account, test_user.account_id)
    with patch("preloop.utils.permissions.user_permission_names", return_value=[]):
        with pytest.raises(HTTPException) as error:
            put_weekly_report_preferences(
                WeeklyReportPreferencesUpdate(**{field: True}),
                account,
                test_user,
                db_session,
            )
    assert error.value.status_code == 403


def test_account_controls_are_independent_and_audited(
    db_session: Session, test_user: models.User
) -> None:
    account = db_session.get(models.Account, test_user.account_id)
    result = update_preferences(
        db_session,
        account_id=account.id,
        user=test_user,
        account_enabled=None,
        user_enabled=None,
        members_enabled=True,
        member_policy_findings_enabled=True,
    )
    assert result == {
        "account_enabled": True,
        "tracking_enabled": True,
        "user_enabled": True,
        "members_enabled": True,
        "member_policy_findings_enabled": True,
        "synthesis_enabled": False,
        "synthesis_model_id": None,
    }
    result = update_preferences(
        db_session,
        account_id=account.id,
        user=test_user,
        account_enabled=None,
        user_enabled=None,
        members_enabled=False,
    )
    assert result["account_enabled"] is True
    assert result["members_enabled"] is False
    logs = (
        db_session.query(models.AuditLog)
        .filter_by(account_id=account.id, action="weekly_report_preferences_updated")
        .all()
    )
    assert len(logs) == 3
    assert all(log.resource_type == "account_settings" for log in logs)
    assert preferences(db_session, account=account, user=test_user) == result


def test_usage_is_user_scoped_before_percentiles_and_window_bounds(
    db_session: Session, test_user: models.User
) -> None:
    for moment, user_id, cost, duration in (
        (START, test_user.id, 1.0, 1.0),
        (END - timedelta(seconds=1), test_user.id, 3.0, 3.0),
        (END, test_user.id, 500.0, 500.0),
        (START, None, 900.0, 900.0),
    ):
        usage = models.ApiUsage(
            account_id=test_user.account_id,
            user_id=user_id,
            endpoint="/v1/messages",
            method="POST",
            status_code=200,
            duration=duration,
            timestamp=moment.replace(tzinfo=None),
            action_type="model_gateway",
            estimated_cost=cost,
            model_alias="example-model",
            runtime_principal_type="cli",
            runtime_principal_id="example-agent",
            runtime_principal_name="Example agent",
        )
        db_session.add(usage)
    db_session.flush()
    result = usage_snapshot(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        start=START,
        end=END,
    )
    assert result["request_count"] == 2
    assert result["total_gateway_cost_usd"] == 4
    assert result["p95_duration"] == pytest.approx(2.9)
    assert result["models"][0]["request_count"] == 2
    assert result["agents"][0]["request_count"] == 2
    foreign = usage_snapshot(
        db_session, account_id=UUID(int=999), user_id=test_user.id, start=START, end=END
    )
    assert foreign["request_count"] == 0


def test_member_approvals_include_decisions_but_not_unrelated_requests(
    db_session: Session, test_user: models.User
) -> None:
    from preloop.models.crud.weekly_report_data import report_approvals

    workflow = models.ApprovalWorkflow(
        account_id=test_user.account_id,
        name="Example approvals",
        approval_type="manual",
    )
    db_session.add(workflow)
    db_session.flush()
    tool = models.ToolConfiguration(
        tool_name="example_tool",
        tool_source="builtin",
        account_id=test_user.account_id,
        approval_workflow_id=workflow.id,
    )
    db_session.add(tool)
    db_session.flush()
    for token, responses, automatic in (
        ("member", [{"user_id": str(test_user.id)}], False),
        ("other", [{"user_id": str(UUID(int=99))}], False),
        ("member-auto", [{"user_id": str(test_user.id)}], True),
    ):
        db_session.add(
            models.ApprovalRequest(
                account_id=test_user.account_id,
                tool_configuration_id=tool.id,
                approval_workflow_id=workflow.id,
                tool_name=tool.tool_name,
                tool_args={},
                status="approved",
                requested_at=START.replace(tzinfo=None),
                resolved_at=(START + timedelta(seconds=20)).replace(tzinfo=None),
                approval_token=token,
                responses=responses,
                decided_by_ai=automatic,
            )
        )
    db_session.flush()
    result = report_approvals(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        start=START,
        end=END,
    )
    assert result["requested"] == 1
    assert result["granted"] == 1
    assert result["auto_approved"] == 1
    assert result["median_decision_seconds"] == 20


@pytest.mark.parametrize("run_status", ["completed", "partial"])
def test_member_findings_require_attribution_and_policy_switch(
    db_session: Session, test_user: models.User, run_status: str
) -> None:
    from preloop.models.crud.weekly_report_data import member_findings

    run = models.GovernanceReviewRun(
        account_id=test_user.account_id,
        window_start=START,
        window_end=END,
        started_at=END,
        status=run_status,
        mode_before="on_platform",
        mode_after="on_platform",
    )
    db_session.add(run)
    db_session.flush()
    for index, (kind, owner) in enumerate(
        (("inefficiency", test_user.id), ("violation", test_user.id), ("anomaly", None))
    ):
        db_session.add(
            models.GovernanceFinding(
                account_id=test_user.account_id,
                run_id=run.id,
                kind=kind,
                category="cache_miss",
                severity="low",
                title=f"Example finding {index}",
                suggestion="Review your session.",
                user_id=owner,
                fingerprint=f"example-{index}",
                evidence=[{"excerpt": "Other person's private data"}],
            )
        )
    db_session.flush()
    arguments = dict(
        account_id=test_user.account_id, user_id=test_user.id, start=START, end=END
    )
    result = member_findings(db_session, **arguments, include_policy=False)
    assert result == []
    assert all(not row["evidence"] for row in result)
    result = member_findings(db_session, **arguments, include_policy=True)
    assert {row["kind"] for row in result} == {"inefficiency", "violation"}
    assert "private data" not in str(result)


@pytest.mark.parametrize("newer_status", ["running", "failed", "skipped"])
def test_unfinished_review_does_not_shadow_stored_findings(
    db_session: Session, test_user: models.User, newer_status: str
) -> None:
    from preloop.models.crud.weekly_report_data import member_findings

    old = models.GovernanceReviewRun(
        account_id=test_user.account_id,
        window_start=START,
        window_end=END - timedelta(hours=1),
        started_at=END - timedelta(hours=1),
        status="completed",
    )
    newer = models.GovernanceReviewRun(
        account_id=test_user.account_id,
        window_start=START + timedelta(hours=1),
        window_end=END,
        started_at=END,
        status=newer_status,
    )
    db_session.add_all([old, newer])
    db_session.flush()
    db_session.add(
        models.GovernanceFinding(
            account_id=test_user.account_id,
            run_id=old.id,
            user_id=test_user.id,
            kind="violation",
            category="policy_notice",
            severity="low",
            title="Stored finding",
            suggestion="Use the approved tool.",
            fingerprint="completed-finding",
        )
    )
    db_session.flush()
    rows = member_findings(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        start=START,
        end=END,
        include_policy=True,
    )
    assert [row["title"] for row in rows] == ["Stored finding"]


def test_models_under_distinct_aliases_and_providers_have_unique_identities(
    db_session: Session, test_user: models.User
) -> None:
    model = models.AIModel(
        account_id=test_user.account_id,
        name="Example model",
        provider_name="example",
        model_identifier="example-model",
    )
    db_session.add(model)
    db_session.flush()
    for alias, provider in (
        ("alias-one", "example"),
        ("alias-two", "example"),
        ("alias-one", "other"),
    ):
        db_session.add(
            models.ApiUsage(
                account_id=test_user.account_id,
                user_id=test_user.id,
                ai_model_id=model.id,
                model_alias=alias,
                provider_name=provider,
                endpoint="/v1/messages",
                method="POST",
                status_code=200,
                duration=0.2,
                timestamp=START.replace(tzinfo=None),
                action_type="model_gateway",
            )
        )
    db_session.flush()
    snapshot = usage_snapshot(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        start=START,
        end=END,
    )
    assert len(snapshot["models"]) == 3
    assert len({row["identity"] for row in snapshot["models"]}) == 3
    assert {row["model_alias"] for row in snapshot["models"]} == {
        "alias-one",
        "alias-two",
    }


@pytest.mark.parametrize("kind", ["api_key", "managed_agent"])
def test_approval_credentials_require_member_and_account_ownership(
    db_session: Session, test_user: models.User, kind: str
) -> None:
    from preloop.models.crud import crud_account, crud_user
    from preloop.models.crud.weekly_report_data import report_approvals

    other_user = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "username": "other-member",
            "email": "other@example.com",
            "hashed_password": "example",
            "is_active": True,
            "user_source": "local",
        },
    )
    foreign = crud_account.create(
        db_session, obj_in={"organization_name": "Other account", "is_active": True}
    )
    workflow = models.ApprovalWorkflow(
        account_id=test_user.account_id,
        name="Credential approvals",
        approval_type="manual",
    )
    db_session.add(workflow)
    db_session.flush()
    tool = models.ToolConfiguration(
        tool_name="example_tool",
        tool_source="builtin",
        account_id=test_user.account_id,
        approval_workflow_id=workflow.id,
    )
    db_session.add(tool)
    db_session.flush()
    for label, account_id, owner_id in (
        ("own", test_user.account_id, test_user.id),
        ("other", test_user.account_id, other_user.id),
        ("foreign", foreign.id, test_user.id),
    ):
        if kind == "api_key":
            resource = models.ApiKey(
                account_id=account_id, user_id=owner_id, name=label, scopes=[]
            )
        else:
            resource = models.ManagedAgent(
                account_id=account_id,
                owner_user_id=owner_id,
                agent_kind="claude_code",
                session_source_type="cli",
                session_source_id=label,
                display_name=label,
                lifecycle_updated_at=START.replace(tzinfo=None),
                last_seen_at=START.replace(tzinfo=None),
            )
        db_session.add(resource)
        db_session.flush()
        linkage = {
            "api_key_id" if kind == "api_key" else "managed_agent_id": resource.id
        }
        db_session.add(
            models.ApprovalRequest(
                account_id=test_user.account_id,
                tool_configuration_id=tool.id,
                approval_workflow_id=workflow.id,
                tool_name=tool.tool_name,
                tool_args={},
                status="approved",
                requested_at=START.replace(tzinfo=None),
                resolved_at=(START + timedelta(seconds=20)).replace(tzinfo=None),
                approval_token=f"{kind}-{label}",
                **linkage,
            )
        )
    db_session.flush()
    result = report_approvals(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        start=START,
        end=END,
    )
    assert result["requested"] == 1
    assert result["granted"] == 1
