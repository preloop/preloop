"""The pre-check gate in the trigger service (#2541), against the database.

A fake reader replaces the tracker; dispatch, claim and runner lease are
mocks that must not be called on a skipped or failed tick.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
from preloop.models.models import Account, Flow
from preloop.models.models.organization import Organization
from preloop.models.models.project import Project
from preloop.models.schemas.flow import CronSchedule, FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services import flow_precheck as engine
from preloop.services.flow_failure_alerts import classify_execution
from preloop.services.flow_precheck_reader import PrecheckBudget, PrecheckError
from preloop.services.flow_trigger_service import FlowTriggerService
from tests.services.test_flow_precheck_engine import FakeReader, pr

pytestmark = pytest.mark.asyncio

REPO = "acme/app"
REPO_URL = "https://github.com/acme/docs"
PR_CHECK = {"checks": [{"id": "stale_prs", "type": "open_pull_requests"}]}


@pytest.fixture
def account(db_session: Session) -> Account:
    return crud_account.create(
        db_session,
        obj_in={"organization_name": f"Org {uuid4().hex[:8]}", "is_active": True},
    )


@pytest.fixture
def project(db_session: Session, account: Account) -> Project:
    from preloop.models.crud import crud_tracker
    from preloop.models.schemas.tracker import TrackerCreate

    tracker = crud_tracker.create(
        db_session,
        obj_in=TrackerCreate(
            name="GitHub",
            tracker_type="github",
            url="https://github.com/acme",
            api_key="test_key",
            account_id=account.id,
            is_active=True,
        ).model_dump(),
    )
    org = Organization(name="acme", identifier="acme", tracker_id=tracker.id)
    db_session.add(org)
    db_session.flush()
    project = Project(name="app", identifier="app", slug=REPO, organization_id=org.id)
    db_session.add(project)
    db_session.flush()
    return project


def make_flow(
    db: Session, account: Account, project: Project, precheck: Optional[dict]
) -> Flow:
    return crud_flow.create(
        db=db,
        flow_in=FlowCreate(
            name=f"Shepherd {uuid4().hex[:8]}",
            trigger_event_source="schedule",
            trigger_event_types=["schedule"],
            trigger_project_ids=[project.id],
            schedule_config=CronSchedule(expr="*/10 * * * *", timezone="UTC"),
            prompt_template="Work only on {{trigger_event.payload.precheck}}",
            agent_type="openhands",
            agent_config={},
            precheck=precheck,
        ),
        account_id=account.id,
    )


@pytest.fixture
def reader(monkeypatch: pytest.MonkeyPatch) -> Dict[str, FakeReader]:
    """The fake reader every evaluation gets; tests swap ``holder['r']``."""
    holder = {"r": FakeReader()}

    async def factory(tracker: Any, budget: PrecheckBudget) -> FakeReader:
        return holder["r"]

    monkeypatch.setattr(engine, "build_reader", factory)
    return holder


@pytest.fixture
def downstream() -> Any:
    """Dispatch, claim and runner lease, all of which a skip must not reach."""
    with (
        patch.object(
            FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock
        ) as start,
        patch(
            "preloop.services.flow_trigger_service.get_nats_client",
            new_callable=AsyncMock,
        ) as nats,
        patch.object(
            type(crud_flow_execution), "claim_execution", MagicMock()
        ) as claim,
        patch("preloop.services.runner_service.lease_job", MagicMock()) as lease,
    ):
        yield MagicMock(start=start, nats=nats, claim=claim, lease=lease)


def executions(db: Session, flow: Flow) -> List[Any]:
    return crud_flow_execution.get_by_flow(db, flow_id=flow.id) or []


def _assert_nothing_dispatched(downstream: Any) -> None:
    downstream.start.assert_not_called()
    downstream.nats.assert_not_called()
    downstream.claim.assert_not_called()
    downstream.lease.assert_not_called()


class TestScheduledTick:
    async def test_nothing_to_do_records_a_skip_and_dispatches_nothing(
        self, db_session, account, project, reader, downstream
    ) -> None:
        flow = make_flow(db_session, account, project, PR_CHECK)

        outcome = await FlowTriggerService(db_session).run_scheduled_tick(flow.id)

        assert outcome == "skipped_precheck"
        _assert_nothing_dispatched(downstream)
        [row] = executions(db_session, flow)
        assert row.status == "SKIPPED"
        assert row.start_time is not None and row.end_time is not None
        assert row.model_output_summary == "No open pull requests need attention"
        document = row.trigger_event_details["payload"]["precheck"]
        assert document["decision"] == "skip"
        assert document["reason"] == "nothing_to_do"
        assert document["skip_count"] == 1
        # The schedule payload is kept beside the decision.
        assert row.trigger_event_details["payload"]["scheduled_at"]
        assert not row.total_tokens and not row.estimated_cost

    async def test_consecutive_skips_fold_and_a_run_starts_a_new_row(
        self, db_session, account, project, reader, downstream
    ) -> None:
        flow = make_flow(db_session, account, project, PR_CHECK)
        service = FlowTriggerService(db_session)

        await service.run_scheduled_tick(flow.id)
        await service.run_scheduled_tick(flow.id)
        await service.run_scheduled_tick(flow.id)
        [row] = executions(db_session, flow)
        document = row.trigger_event_details["payload"]["precheck"]
        assert document["skip_count"] == 3
        assert document["first_skipped_at"] < document["last_skipped_at"]

        crud_flow_execution.create(
            db_session,
            obj_in=FlowExecutionCreate(
                flow_id=flow.id,
                status="SUCCEEDED",
                start_time=datetime.now(timezone.utc) + timedelta(seconds=1),
                trigger_event_details={"source": "schedule", "payload": {}},
            ),
        )
        db_session.commit()
        await service.run_scheduled_tick(flow.id)
        statuses = sorted(r.status for r in executions(db_session, flow))
        assert statuses == ["SKIPPED", "SKIPPED", "SUCCEEDED"]

    async def test_a_match_runs_with_the_items_in_the_payload(
        self, db_session, account, project, reader, downstream
    ) -> None:
        reader["r"] = FakeReader(prs=[pr(101)])
        flow = make_flow(db_session, account, project, PR_CHECK)

        outcome = await FlowTriggerService(db_session).run_scheduled_tick(flow.id)

        assert outcome == "triggered"
        downstream.start.assert_awaited_once()
        event_data = downstream.start.await_args.kwargs["event_data"]
        document = event_data["payload"]["precheck"]
        assert document["decision"] == "run"
        assert document["checks"]["stale_prs"]["items"][0]["number"] == 101
        assert event_data["payload"]["scheduled_at"]
        assert executions(db_session, flow) == []

        from preloop.services.prompt_resolvers.base import ResolverContext
        from preloop.services.prompt_resolvers.trigger_event import (
            TriggerEventResolver,
        )

        resolved = await TriggerEventResolver().resolve(
            "payload.precheck",
            ResolverContext(
                db=db_session,
                trigger_event_data=event_data,
                flow_id=str(flow.id),
                execution_id="x",
            ),
        )
        assert resolved["checks"]["stale_prs"]["count"] == 1

    async def test_a_tracker_error_fails_visibly_and_starts_nothing(
        self, db_session, account, project, reader, downstream
    ) -> None:
        reader["r"] = FakeReader(error=PrecheckError("server_error", "GitHub 502"))
        flow = make_flow(db_session, account, project, PR_CHECK)

        async def inline(operation: Any) -> Any:
            return operation(db_session)

        with (
            patch(
                "preloop.services.flow_failure_alerts.evaluate_failure_streak"
            ) as streak,
            patch("preloop.services.db_executor.run_db_async", inline),
        ):
            outcome = await FlowTriggerService(db_session).run_scheduled_tick(flow.id)

        assert outcome == "precheck_failed"
        _assert_nothing_dispatched(downstream)
        [row] = executions(db_session, flow)
        assert row.status == "FAILED"
        assert row.failure_category == "precheck_failed"
        assert "stale_prs" in row.error_message
        assert "test_key" not in row.error_message
        streak.assert_called_once()

    async def test_on_error_run_starts_and_names_the_error(
        self, db_session, account, project, reader, downstream
    ) -> None:
        reader["r"] = FakeReader(error=PrecheckError("rate_limited", "quota"))
        flow = make_flow(db_session, account, project, {**PR_CHECK, "on_error": "run"})

        assert await FlowTriggerService(db_session).run_scheduled_tick(flow.id) == (
            "triggered"
        )
        event_data = downstream.start.await_args.kwargs["event_data"]
        assert event_data["payload"]["precheck"]["precheck_error"] == {
            "check": "stale_prs",
            "cause": "rate_limited",
        }

    async def test_the_kill_switch_restores_every_tick(
        self, db_session, account, project, reader, downstream, monkeypatch
    ) -> None:
        monkeypatch.setenv("PRELOOP_FLOW_PRECHECK_ENABLED", "false")
        flow = make_flow(db_session, account, project, PR_CHECK)

        assert await FlowTriggerService(db_session).run_scheduled_tick(flow.id) == (
            "triggered"
        )
        event_data = downstream.start.await_args.kwargs["event_data"]
        assert "precheck" not in event_data["payload"]

    async def test_a_flow_without_a_precheck_is_unchanged(
        self, db_session, account, project, reader, downstream
    ) -> None:
        flow = make_flow(db_session, account, project, None)
        assert await FlowTriggerService(db_session).run_scheduled_tick(flow.id) == (
            "triggered"
        )
        assert (
            "precheck"
            not in downstream.start.await_args.kwargs["event_data"]["payload"]
        )

    async def test_overlap_still_wins_before_the_precheck(
        self, db_session, account, project, reader, downstream, monkeypatch
    ) -> None:
        flow = make_flow(db_session, account, project, PR_CHECK)
        crud_flow_execution.create(
            db_session,
            obj_in=FlowExecutionCreate(flow_id=flow.id, status="RUNNING"),
        )
        db_session.commit()
        evaluate = AsyncMock()
        monkeypatch.setattr(engine, "evaluate_precheck", evaluate)

        outcome = await FlowTriggerService(db_session).run_scheduled_tick(flow.id)
        assert outcome == "skipped_overlap"
        evaluate.assert_not_called()

    async def test_since_last_run_ignores_skipped_rows(
        self, db_session, account, project, reader, downstream
    ) -> None:
        flow = make_flow(
            db_session,
            account,
            project,
            {"checks": [{"id": "new_failures", "type": "failed_ci_runs"}]},
        )
        crud_flow_execution.create(
            db_session,
            obj_in=FlowExecutionCreate(
                flow_id=flow.id,
                status="SUCCEEDED",
                start_time=datetime(2026, 1, 1, 6, tzinfo=timezone.utc),
                trigger_event_details={
                    "source": "schedule",
                    "payload": {"scheduled_at": "2026-01-01T06:00:00+00:00"},
                },
            ),
        )
        db_session.commit()
        fake = FakeReader()
        reader["r"] = fake
        service = FlowTriggerService(db_session)
        await service.run_scheduled_tick(flow.id)  # SKIPPED
        await service.run_scheduled_tick(flow.id)  # folded
        expected = datetime(2026, 1, 1, 6, tzinfo=timezone.utc)
        assert [call["since"] for call in fake.calls] == [expected, expected]


class TestStatusNeutrality:
    async def test_a_skip_is_neutral_and_not_a_success(
        self, db_session, account, project, reader, downstream
    ) -> None:
        flow = make_flow(db_session, account, project, PR_CHECK)
        await FlowTriggerService(db_session).run_scheduled_tick(flow.id)
        [row] = executions(db_session, flow)

        assert classify_execution(row) == "neutral"
        assert (
            crud_flow_execution.last_successful_scheduled_at(
                db_session, flow_id=flow.id
            )
            is None
        )
        assert "SKIPPED" in crud_flow_execution.TERMINAL_EXECUTION_STATUSES

    async def test_window_counts_skips_apart_from_runs(
        self, db_session, account, project, reader, downstream
    ) -> None:
        flow = make_flow(db_session, account, project, PR_CHECK)
        service = FlowTriggerService(db_session)
        for _ in range(3):
            await service.run_scheduled_tick(flow.id)
        crud_flow_execution.create(
            db_session,
            obj_in=FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED"),
        )
        db_session.commit()

        [stat] = crud_flow_execution.get_execution_stats_for_flows(
            db_session,
            [flow.id],
            start_date=datetime.now(timezone.utc) - timedelta(days=1),
            include_lifetime_usage=False,
        )
        assert stat.runs == 1
        assert stat.skipped_count == 3
        assert stat.last_skipped_at is not None


class TestManualTrigger:
    async def test_manual_runs_evaluate_only_when_opted_in(
        self, db_session, account, project, reader, downstream
    ) -> None:
        flow = make_flow(
            db_session, account, project, {**PR_CHECK, "applies_to": ["manual"]}
        )
        service = FlowTriggerService(db_session)
        with patch(
            "preloop.services.flow_trigger_service.prepare_execution_routing"
        ) as routing:
            result = await service.trigger_flow(flow.id, evaluate_precheck=True)
            routing.assert_not_called()

        assert result["status"] == "SKIPPED"
        assert result["precheck"]["decision"] == "skip"
        _assert_nothing_dispatched(downstream)

    async def test_retries_and_children_never_evaluate(
        self, db_session, account, project, reader, downstream, monkeypatch
    ) -> None:
        flow = make_flow(
            db_session, account, project, {**PR_CHECK, "applies_to": ["manual"]}
        )
        evaluate = AsyncMock(side_effect=AssertionError("must not evaluate"))
        monkeypatch.setattr(engine, "evaluate_precheck", evaluate)
        service = FlowTriggerService(db_session)
        with (
            patch.object(service, "_evaluate_and_record_precheck", evaluate),
            patch(
                "preloop.services.flow_trigger_service.prepare_execution_routing",
                side_effect=lambda db, flow, details, **kw: details,
            ),
            patch("preloop.services.runner_service.validate_runner_capabilities"),
        ):
            parent = crud_flow_execution.create(
                db_session,
                obj_in=FlowExecutionCreate(flow_id=flow.id, status="RUNNING"),
            )
            db_session.commit()
            for kwargs in (
                {
                    "parent_execution_id": parent.id,
                    "root_execution_id": parent.id,
                    "delegation_depth": 1,
                },
                {"batch_id": uuid4()},
            ):
                await service.trigger_flow(flow.id, evaluate_precheck=True, **kwargs)
        evaluate.assert_not_called()
        assert downstream.start.await_count == 2

    async def test_a_schedule_only_precheck_leaves_manual_runs_alone(
        self, db_session, account, project, reader, downstream
    ) -> None:
        flow = make_flow(db_session, account, project, PR_CHECK)
        with (
            patch(
                "preloop.services.flow_trigger_service.prepare_execution_routing",
                side_effect=lambda db, flow, details, **kw: details,
            ),
            patch("preloop.services.runner_service.validate_runner_capabilities"),
        ):
            result = await FlowTriggerService(db_session).trigger_flow(
                flow.id, evaluate_precheck=True
            )
        assert result["status"] == "PENDING"
        downstream.start.assert_awaited_once()


def test_existing_serializers_accept_the_new_status(db_session) -> None:
    """A rollback keeps SKIPPED rows readable: status is a free string."""
    from preloop.models.schemas.flow_execution import (
        FlowExecutionListResponse,
        FlowExecutionResponse,
    )

    now = datetime.now(timezone.utc)
    row = {
        "id": uuid4(),
        "flow_id": uuid4(),
        "status": "SKIPPED",
        "start_time": now,
        "end_time": now,
        "created_at": now,
        "updated_at": now,
        "trigger_event_details": {"payload": {"precheck": {"decision": "skip"}}},
    }
    detail = FlowExecutionResponse.model_validate(row)
    assert detail.status == "SKIPPED"
    assert detail.precheck == {"decision": "skip"}
    assert FlowExecutionListResponse.model_validate(row).status == "SKIPPED"


def test_a_flow_without_an_account_reads_no_repository(db_session, project) -> None:
    """Lookups are account scoped; no account means nothing to read."""
    assert (
        engine.resolve_flow_repositories(
            db_session,
            account_id=None,
            trigger_project_ids=[project.id],
            git_clone_config=None,
        )
        == []
    )


def test_repositories_come_from_projects_and_clone_urls(
    db_session, account, project
) -> None:
    tracker = project.organization.tracker
    found = engine.resolve_flow_repositories(
        db_session,
        account_id=account.id,
        trigger_project_ids=[project.id],
        git_clone_config={
            "repositories": [
                {"tracker_id": str(tracker.id), "repository_url": REPO_URL},
                {"tracker_id": str(tracker.id), "repository_url": REPO_URL + ".git"},
            ]
        },
    )
    assert [repo.slug for repo in found] == [REPO, "acme/docs"]
