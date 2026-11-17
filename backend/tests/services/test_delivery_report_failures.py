"""Scheduled report failures remain visible and notify recipients once."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from preloop.agents.codex import CodexAgent
from preloop.agents.errors import AgentStartError
from preloop.services import delivery_report as dr
from preloop.services import delivery_report_email as email
from preloop.services import delivery_report_views as views
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.utils.execve_limits import MAX_LAUNCH_TOTAL_BYTES, CHUNK_BYTES


def failed_execution() -> Any:
    """A scheduled report refused before it produced a result document."""
    return SimpleNamespace(
        id=uuid4(),
        flow_id=uuid4(),
        flow=SimpleNamespace(name="Delivery Report"),
        status="FAILED",
        start_time=None,
        end_time=None,
        result=None,
        trigger_event_details={
            "payload": {
                "window": {
                    "from": "2026-01-05T07:00:00+00:00",
                    "to": "2026-01-12T07:00:00+00:00",
                }
            }
        },
        failure_category="launch_payload_too_large",
        error_message="Repository setup for 500 repositories is 1800000 bytes; limit 1048576 bytes.",
    )


def test_launch_refusal_has_specific_category_and_sizes() -> None:
    """A known launch limit must never collapse into generic runner_error."""
    agent = CodexAgent({})
    with pytest.raises(AgentStartError) as error:
        agent._guard_launch_payload(
            command=None,
            args=None,
            env={f"DATA_{i}": "x" * CHUNK_BYTES for i in range(12)},
            what="Delivery Report (500 repositories)",
        )
    assert error.value.category == "launch_payload_too_large"
    assert "500 repositories" in str(error.value)
    assert str(MAX_LAUNCH_TOTAL_BYTES) in str(error.value)
    assert "bytes" in str(error.value)


def test_failed_report_without_result_has_visible_reason() -> None:
    """The detail page can explain a failure that happened before agent startup."""
    execution = failed_execution()
    with patch.object(views, "rerun_projects", return_value=[]):
        detail = views.report_detail(
            MagicMock(), account_id=uuid4(), execution=execution
        )
    assert detail.report_status == "error"
    assert "500 repositories" in detail.report["reason"]
    assert detail.window.start == "2026-01-05T07:00:00+00:00"


@pytest.mark.asyncio
async def test_report_failure_emails_recipients_once_without_flow_notifications() -> (
    None
):
    """Retries of terminal handling do not send a second notice for the run."""
    execution = failed_execution()
    flow = SimpleNamespace(
        account_id=uuid4(),
        notifications=None,
        agent_config={"report_kind": "delivery_report"},
    )
    recipient = SimpleNamespace(id=uuid4(), email="jane@example.com")
    with (
        patch(
            "preloop.models.crud.crud_delivery_report_policy.get_overrides",
            return_value=None,
        ),
        patch(
            "preloop.models.crud.crud_delivery_report_policy.report_recipients",
            return_value=[recipient],
        ),
        patch(
            "preloop.models.crud.crud_flow_execution.claim_report_failure_notice",
            create=True,
            side_effect=[True, False],
        ),
        patch(
            "preloop.models.crud.crud_flow_execution.finish_report_failure_notice",
            create=True,
        ),
        patch("preloop.utils.email.send_email", return_value=True) as send,
    ):
        for _ in range(2):
            await email.distribute_failure(MagicMock(), flow=flow, execution=execution)
    send.assert_called_once()
    assert send.call_args.args[0] == "jane@example.com"
    assert "could not" in send.call_args.args[2].lower()
    assert "500 repositories" in send.call_args.args[2]
    assert str(execution.id) in send.call_args.args[2]


@pytest.mark.parametrize("active_indexes", [[], [0, 7, 499]])
def test_report_uses_activity_to_select_from_500_repositories(
    active_indexes: list[int],
) -> None:
    """Inactive checkouts disappear while the stored scope remains unchanged."""
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.db = MagicMock()
    orchestrator.execution_log = failed_execution()
    orchestrator.trigger_event_data = orchestrator.execution_log.trigger_event_details
    orchestrator.flow = SimpleNamespace(
        account_id=uuid4(),
        agent_config={"report_kind": "delivery_report"},
        git_clone_config={"enabled": True, "repositories": []},
    )
    orchestrator._cached_payload_identity = lambda: None
    orchestrator._delivery_report_run = dr.PolicyRun(
        decision="onboarding", policy=dr.DeliveryReportPolicy()
    )
    repositories = [
        (f"example/project-{i}", str(uuid4()), str(uuid4()), "main") for i in range(500)
    ]
    active = {repositories[i][1] for i in active_indexes}
    with (
        patch(
            "preloop.models.crud.crud_delivery_report_policy.code_repositories",
            return_value=repositories,
        ),
        patch(
            "preloop.models.crud.crud_delivery_report_policy.active_report_projects",
            create=True,
            return_value=active,
        ) as activity,
    ):
        config = orchestrator._git_clone_config_for_trigger()
    assert {r["project_id"] for r in config["repositories"]} == active
    assert config["enabled"] is bool(active_indexes)
    assert orchestrator.flow.git_clone_config["repositories"] == []
    assert activity.call_args.kwargs["account_id"] == orchestrator.flow.account_id


@pytest.mark.asyncio
async def test_failure_notice_persists_one_delivery_in_postgres(
    db_session: Any,
    test_user: Any,
) -> None:
    """Repeated callbacks use the durable marker and the actual policy recipients."""
    from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
    from preloop.models.schemas.flow import FlowCreate
    from preloop.models.schemas.flow_execution import FlowExecutionCreate

    account = crud_account.get(db_session, id=test_user.account_id)
    account.primary_user_id = test_user.id
    db_session.flush()
    flow = crud_flow.create(
        db_session,
        account_id=account.id,
        flow_in=FlowCreate(
            name="Weekly Delivery Report",
            account_id=account.id,
            agent_type="codex",
            agent_config={"report_kind": "delivery_report"},
            prompt_template="Report",
            trigger_event_source="schedule",
            trigger_event_types=["schedule"],
        ),
    )
    execution = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id,
            status="FAILED",
        ),
    )
    execution.error_message = failed_execution().error_message
    execution.failure_category = "launch_payload_too_large"
    db_session.commit()
    with patch("preloop.utils.email.send_email", return_value=True) as send:
        await email.distribute_failure(db_session, flow=flow, execution=execution)
        db_session.expire_all()
        await email.distribute_failure(db_session, flow=flow, execution=execution)
    send.assert_called_once()
    assert send.call_args.args[0] == test_user.email
    db_session.refresh(execution)
    assert execution.result["failure_notice"]["status"] == "sent"
    assert execution.status == "FAILED"
    assert not crud_flow_execution.claim_report_failure_notice(
        db_session, execution_id=execution.id, account_id=uuid4()
    )


def test_synced_activity_keeps_merges_touched_issues_and_unknown_projects() -> None:
    """Five hundred repos are filtered without treating missing data as inactive."""
    from datetime import datetime, timezone
    from preloop.models.crud import crud_delivery_report_policy

    ids = [uuid4() for _ in range(500)]
    rows = [
        (
            identifier,
            f"example/project-{i}",
            {"updated_at": "2025-12-01T00:00:00Z"},
            "github",
        )
        for i, identifier in enumerate(ids)
    ]
    rows[1] = (
        ids[1],
        "example/project-1",
        {"updated_at": "2026-01-08T00:00:00Z"},
        "github",
    )
    rows[2] = (ids[2], "example/project-2", {}, "github")
    rows[3] = (ids[3], "example/project-3", {"updated_at": "bad"}, "github")
    rows[4] = (
        ids[4],
        "example/project-4",
        {"updated_at": "2025-12-01T00:00:00Z"},
        "gitlab",
    )
    db = MagicMock()
    db.execute.return_value.all.return_value = rows
    db.scalars.side_effect = [
        MagicMock(all=MagicMock(return_value=[ids[7]])),
        MagicMock(
            all=MagicMock(
                return_value=["https://github.com/example/project-499/pull/1"]
            )
        ),
    ]
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    end = datetime(2026, 1, 12, tzinfo=timezone.utc)
    selected = crud_delivery_report_policy.active_report_projects(
        db,
        account_id=uuid4(),
        start=start,
        end=end,
    )
    assert selected == {str(ids[i]) for i in (1, 2, 3, 4, 7, 499)}
    for call in db.scalars.call_args_list:
        assert "account_id" in str(call.args[0])
        assert "LIMIT" not in str(call.args[0])


def test_concurrent_failure_notice_claims_choose_one_worker(db_engine: Any) -> None:
    """Two real database sessions cannot both send the same run's notice."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.orm import Session
    from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
    from preloop.models.schemas.flow import FlowCreate
    from preloop.models.schemas.flow_execution import FlowExecutionCreate

    with Session(db_engine) as setup:
        account = crud_account.create(
            setup, obj_in={"organization_name": "Report claim test"}
        )
        account_id = account.id
        flow = crud_flow.create(
            setup,
            account_id=account_id,
            flow_in=FlowCreate(
                name="Delivery Report",
                account_id=account_id,
                agent_type="codex",
                agent_config={"report_kind": "delivery_report"},
                prompt_template="Report",
                trigger_event_source="schedule",
                trigger_event_types=["schedule"],
            ),
        )
        execution = crud_flow_execution.create(
            setup,
            obj_in=FlowExecutionCreate(
                flow_id=flow.id,
                status="FAILED",
            ),
        )
        execution_id = execution.id
        setup.commit()
    barrier = Barrier(2)

    def claim() -> bool:
        """Claim through the production CRUD boundary after both workers start."""
        with Session(db_engine) as worker:
            barrier.wait(timeout=10)
            return crud_flow_execution.claim_report_failure_notice(
                worker,
                execution_id=execution_id,
                account_id=account_id,
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(claim) for _ in range(2)]
            assert sorted(future.result(timeout=15) for future in futures) == [
                False,
                True,
            ]
    finally:
        with Session(db_engine) as cleanup:
            crud_account.delete(cleanup, id=account_id)
            cleanup.commit()


@pytest.mark.asyncio
async def test_report_failure_streak_keeps_push_and_skips_only_email() -> None:
    """A report flow keeps its streak safety net; only the duplicate email goes."""
    from unittest.mock import AsyncMock

    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.flow = SimpleNamespace(
        id=uuid4(), agent_config={"report_kind": "delivery_report"}
    )
    orchestrator.execution_log = failed_execution()
    with patch(
        "preloop.services.db_executor.run_db_async", new=AsyncMock(return_value=None)
    ) as run:
        await orchestrator._notify_failure_streak("FAILED", skip_email=True)
    run.assert_awaited_once()

    from preloop.services import flow_failure_alerts as alerts

    owner = SimpleNamespace(id=uuid4(), email="jane@example.com")
    streak = alerts.FailureStreak(
        count=2,
        execution_ids=["a", "b"],
        last_error="x",
        streak_start_execution_id="b",
        already_alerted=False,
    )
    with (
        patch(
            "preloop.services.policy_notice_delivery.policy_owners",
            return_value=[owner],
        ),
        patch(
            "preloop.services.policy_notice_delivery.send_push_to_owners",
            return_value=1,
        ) as push,
        patch.object(alerts, "_send_emails", return_value=1) as mail,
    ):
        counts = alerts.deliver_failure_alert(
            MagicMock(),
            flow=orchestrator.flow,
            streak=streak,
            threshold=2,
            skip_email=True,
        )
        alerts.deliver_failure_alert(
            MagicMock(), flow=orchestrator.flow, streak=streak, threshold=2
        )
    assert counts["push"] == 1 and counts["email"] == 0
    assert push.call_count == 2
    mail.assert_called_once()


def _operator_failure_patches(claims: list[bool]) -> Any:
    """Patch recipient lookup and the durable claim for operator alert tests."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch(
            "preloop.models.crud.crud_delivery_report_policy.get_overrides",
            return_value=None,
        )
    )
    stack.enter_context(
        patch(
            "preloop.models.crud.crud_delivery_report_policy.report_recipients",
            return_value=[],
        )
    )
    stack.enter_context(
        patch(
            "preloop.models.crud.crud_flow_execution.claim_report_failure_notice",
            side_effect=claims,
        )
    )
    stack.enter_context(
        patch("preloop.models.crud.crud_flow_execution.finish_report_failure_notice")
    )
    return stack


@pytest.mark.asyncio
@pytest.mark.parametrize("operator_account", [True, False])
async def test_operator_account_failure_reaches_admin_channel_once(
    operator_account: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """Only the operator's own accounts page the admins, once per failed run."""
    from preloop.config import settings

    execution = failed_execution()
    flow = SimpleNamespace(
        account_id=uuid4(), agent_config={"report_kind": "delivery_report"}
    )
    listed = [str(flow.account_id)] if operator_account else [str(uuid4())]
    with (
        _operator_failure_patches([True, False]),
        patch.object(settings, "operator_account_ids", listed),
        patch("preloop.sync.tasks.notify_admins") as admins,
    ):
        first = await email.distribute_failure(
            MagicMock(), flow=flow, execution=execution
        )
        second = await email.distribute_failure(
            MagicMock(), flow=flow, execution=execution
        )
    assert second is None
    if not operator_account:
        admins.assert_not_called()
        assert "operator_alerted" not in first
        return
    admins.assert_called_once()
    subject, text = admins.call_args.args[:2]
    assert subject == "Delivery Report failed"
    assert "repository setup was too large" in text
    assert "500 repositories" in text
    assert str(execution.id) in text
    assert first["operator_alerted"] is True
    assert "ALERT delivery_report_failed" in caplog.text


@pytest.mark.asyncio
async def test_operator_alert_runs_off_loop_and_records_a_failed_send() -> None:
    """A blocking or failing admin send never claims an alert was delivered."""
    from preloop.config import settings

    flow = SimpleNamespace(
        account_id=uuid4(), agent_config={"report_kind": "delivery_report"}
    )
    with (
        _operator_failure_patches([True]),
        patch.object(settings, "operator_account_ids", [str(flow.account_id)]),
        patch(
            "preloop.sync.tasks.notify_admins", side_effect=RuntimeError("smtp down")
        ) as admins,
        patch.object(
            email.asyncio, "to_thread", wraps=email.asyncio.to_thread
        ) as to_thread,
    ):
        record = await email.distribute_failure(
            MagicMock(), flow=flow, execution=failed_execution()
        )
    assert record["operator_alerted"] is False
    assert any(call.args[0] is admins for call in to_thread.call_args_list)


def test_activity_query_filters_500_projects_with_account_isolation(
    db_session: Any,
    test_user: Any,
) -> None:
    """Actual tracker and ledger rows select active scope without a row ceiling."""
    from datetime import datetime, timezone
    from preloop.models import models
    from preloop.models.crud import crud_account, crud_delivery_report_policy

    tracker = models.Tracker(
        name="Example tracker", tracker_type="github", account_id=test_user.account_id
    )
    db_session.add(tracker)
    db_session.flush()
    organization = models.Organization(
        name="Example", identifier="example", tracker_id=tracker.id
    )
    db_session.add(organization)
    db_session.flush()
    projects = [
        models.Project(
            name=f"Project {i}",
            identifier=str(i),
            slug=f"example/project-{i}",
            organization_id=organization.id,
            meta_data={"updated_at": "2025-12-01T00:00:00Z"},
        )
        for i in range(500)
    ]
    projects[0].meta_data = {"updated_at": "2026-01-08T00:00:00Z"}
    projects[3].meta_data = {}
    db_session.add_all(projects)
    db_session.flush()
    db_session.add(
        models.Issue(
            title="Completed work",
            external_id="1",
            project_id=projects[1].id,
            tracker_id=tracker.id,
            last_updated_external=datetime(2026, 1, 9),
        )
    )
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other account"}
    )
    for account_id, index in ((test_user.account_id, 2), (other.id, 4)):
        db_session.add(
            models.IssueCostPullRequest(
                account_id=account_id,
                pr_key=f"https://github.com/example/project-{index}/pull/1",
                merged_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
                state="merged",
            )
        )
    db_session.flush()
    active = crud_delivery_report_policy.active_report_projects(
        db_session,
        account_id=test_user.account_id,
        start=datetime(2026, 1, 5, tzinfo=timezone.utc),
        end=datetime(2026, 1, 12, tzinfo=timezone.utc),
    )
    assert active == {str(projects[i].id) for i in (0, 1, 2, 3)}


@pytest.mark.parametrize(
    ("history", "flagged"),
    [
        # Newest first: (status, scheduled?)
        ([("FAILED", True), ("FAILED", True)], 2),
        ([("FAILED", True), ("FAILED", False), ("TIMEOUT", True)], 2),
        ([("FAILED", True), ("SUCCEEDED", True), ("FAILED", True)], 0),
        ([("FAILED", True), ("FAILED", False)], 0),
        ([("RUNNING", True), ("FAILED", True), ("FAILED", True)], 2),
    ],
)
def test_two_failed_scheduled_runs_flag_the_report_page(
    db_session: Any,
    test_user: Any,
    history: list[tuple[str, bool]],
    flagged: int,
) -> None:
    """Only consecutive failed scheduled runs flag a report flow."""
    from datetime import datetime, timedelta, timezone
    from preloop.models.crud import crud_flow, crud_flow_execution
    from preloop.models.schemas.flow import FlowCreate
    from preloop.models.schemas.flow_execution import FlowExecutionCreate

    flow = crud_flow.create(
        db_session,
        account_id=test_user.account_id,
        flow_in=FlowCreate(
            name="Weekly Delivery Report",
            account_id=test_user.account_id,
            agent_type="codex",
            agent_config={"report_kind": "delivery_report"},
            prompt_template="Report",
            trigger_event_source="schedule",
            trigger_event_types=["schedule"],
        ),
    )
    now = datetime(2026, 1, 12, 7, tzinfo=timezone.utc)
    newest = None
    for index, (status, scheduled) in enumerate(history):
        payload = {"scheduled_at": now.isoformat()} if scheduled else {}
        execution = crud_flow_execution.create(
            db_session,
            obj_in=FlowExecutionCreate(
                flow_id=flow.id,
                status=status,
                trigger_event_details={"source": "schedule", "payload": payload},
            ),
        )
        execution.start_time = now - timedelta(days=7 * index)
        execution.failure_category = "launch_payload_too_large"
        if newest is None and status != "RUNNING":
            newest = execution
    db_session.flush()

    flows = views.failing_report_flows(db_session, account_id=str(test_user.account_id))

    if not flagged:
        assert flows == []
        return
    assert len(flows) == 1
    assert flows[0].flow_id == flow.id
    assert flows[0].consecutive_failures == flagged
    assert flows[0].latest_execution_id == newest.id
    assert "repository setup was too large" in flows[0].reason
