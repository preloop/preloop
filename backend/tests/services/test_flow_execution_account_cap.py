"""One account must not be able to take every execution slot on an instance.

On 2026-09-15 a burst of 21 executions from a single account filled all three
serial flow-execution workers on the hosted instance and queued the other
sixteen. Nothing belonging to any other account could start until the queue
was cleared by hand. The instance-wide worker cap bounds throughput; it does
not bound one account's share of it.

The cap is enforced where admission happens (claim time), so retries,
resumes and recovery re-dispatches obey it too.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.execution_concurrency import (
    QUEUED_REASON_ACCOUNT_CAP,
    account_running_cap,
)
from preloop.services.execution_recovery import ExecutionRecoveryService


def _make_account(db: Session, name: str, meta_data: dict | None = None):
    account = crud_account.create(
        db,
        obj_in={
            "organization_name": name,
            "is_active": True,
            "meta_data": meta_data or {},
        },
    )
    db.commit()
    db.refresh(account)
    return account


def _make_flow(db: Session, account_id):
    flow = crud_flow.create(
        db=db,
        flow_in=FlowCreate(
            name=f"cap-test-{uuid.uuid4().hex[:8]}",
            prompt_template="hello",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="openhands",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            is_enabled=True,
            account_id=account_id,
        ),
        account_id=account_id,
    )
    db.commit()
    db.refresh(flow)
    return flow


def _make_runner(db: Session, account_id):
    from preloop.models.crud.flow_runner import crud_flow_runner

    runner = crud_flow_runner.create(
        db,
        obj_in={
            "account_id": account_id,
            "name": f"runner-{uuid.uuid4().hex[:8]}",
            "labels": [],
            "status": "online",
            "token_hash": uuid.uuid4().hex,
        },
    )
    db.commit()
    db.refresh(runner)
    return runner


def _pending(db: Session, flow_id):
    execution = crud_flow_execution.create(
        db,
        obj_in=FlowExecutionCreate(
            flow_id=flow_id,
            status="PENDING",
            trigger_event_details={"source": "test"},
        ),
    )
    db.commit()
    db.refresh(execution)
    return execution


@pytest.fixture
def two_accounts(db_session: Session):
    """Two accounts, one flow each, three PENDING executions each."""
    first = _make_account(db_session, "Account One")
    second = _make_account(db_session, "Account Two")
    first_flow = _make_flow(db_session, first.id)
    second_flow = _make_flow(db_session, second.id)
    return {
        first.id: [_pending(db_session, first_flow.id) for _ in range(3)],
        second.id: [_pending(db_session, second_flow.id) for _ in range(3)],
    }


class TestAccountRunningCap:
    """Resolution: account metadata first, then the deployment setting."""

    def test_default_comes_from_settings(self):
        from preloop.config import settings

        class _Account:
            meta_data = None

        assert account_running_cap(_Account()) == int(
            settings.flow_execution_max_running_per_account
        )

    def test_account_metadata_overrides(self):
        class _Account:
            meta_data = {"flow_execution_max_running_per_account": 7}

        assert account_running_cap(_Account()) == 7

    def test_garbage_and_zero_fall_back_to_the_default(self):
        from preloop.config import settings

        class _Garbage:
            meta_data = {"flow_execution_max_running_per_account": "lots"}

        class _Zero:
            meta_data = {"flow_execution_max_running_per_account": 0}

        expected = int(settings.flow_execution_max_running_per_account)
        assert account_running_cap(_Garbage()) == expected
        assert account_running_cap(_Zero()) == expected


class TestClaimRespectsTheAccountCap:
    """``claim_execution`` refuses admission past the cap, per account."""

    def test_two_accounts_at_cap_one_interleave(
        self, db_session: Session, two_accounts
    ):
        """Three pending each, cap one: one running each, four still PENDING."""
        first_account, second_account = list(two_accounts)
        interleaved = [
            (account, execution)
            for index in range(3)
            for account in (first_account, second_account)
            for execution in [two_accounts[account][index]]
        ]

        claimed: dict = {first_account: [], second_account: []}
        for account, execution in interleaved:
            row = crud_flow_execution.claim_execution(
                db_session,
                execution_id=execution.id,
                worker_id=f"worker-{uuid.uuid4().hex[:6]}",
                stale_after_seconds=120,
                account_cap=1,
            )
            if row is not None:
                claimed[account].append(row.id)

        assert len(claimed[first_account]) == 1
        assert len(claimed[second_account]) == 1
        # The second account's work was not pushed behind the first
        # account's backlog: it got its slot on its own first attempt.
        assert claimed[second_account][0] == two_accounts[second_account][0].id

    def test_held_back_execution_records_why(self, db_session: Session, two_accounts):
        """A refused claim is visible on the row, not only in a worker log."""
        account = list(two_accounts)[0]
        first, second, _ = two_accounts[account]

        assert (
            crud_flow_execution.claim_execution(
                db_session,
                execution_id=first.id,
                worker_id="worker-a",
                stale_after_seconds=120,
                account_cap=1,
            )
            is not None
        )
        assert (
            crud_flow_execution.claim_execution(
                db_session,
                execution_id=second.id,
                worker_id="worker-b",
                stale_after_seconds=120,
                account_cap=1,
            )
            is None
        )

        db_session.refresh(second)
        assert second.status == "PENDING"
        assert second.queued_reason == QUEUED_REASON_ACCOUNT_CAP
        assert second.orchestrator_worker_id is None
        assert (
            crud_flow_execution.get_queued_reason(db_session, execution_id=second.id)
            == QUEUED_REASON_ACCOUNT_CAP
        )

    def test_capped_execution_is_claimed_once_a_slot_frees(
        self, db_session: Session, two_accounts
    ):
        """Finishing the running execution admits the queued one."""
        account = list(two_accounts)[0]
        first, second, _ = two_accounts[account]

        crud_flow_execution.claim_execution(
            db_session,
            execution_id=first.id,
            worker_id="worker-a",
            stale_after_seconds=120,
            account_cap=1,
        )
        assert (
            crud_flow_execution.claim_execution(
                db_session,
                execution_id=second.id,
                worker_id="worker-b",
                stale_after_seconds=120,
                account_cap=1,
            )
            is None
        )

        first.status = "SUCCEEDED"
        first.orchestrator_worker_id = None
        first.orchestrator_heartbeat_at = None
        db_session.add(first)
        db_session.commit()

        admitted = crud_flow_execution.claim_execution(
            db_session,
            execution_id=second.id,
            worker_id="worker-b",
            stale_after_seconds=120,
            account_cap=1,
        )

        assert admitted is not None
        assert admitted.orchestrator_worker_id == "worker-b"
        assert admitted.queued_reason is None

    def test_a_dead_workers_claim_does_not_hold_a_slot(
        self, db_session: Session, two_accounts
    ):
        """A stale claim with no agent session is not an admitted execution."""
        account = list(two_accounts)[0]
        first, second, _ = two_accounts[account]

        crud_flow_execution.claim_execution(
            db_session,
            execution_id=first.id,
            worker_id="worker-a",
            stale_after_seconds=120,
            account_cap=1,
        )
        first.orchestrator_heartbeat_at = datetime.now(timezone.utc) - timedelta(
            seconds=600
        )
        db_session.add(first)
        db_session.commit()

        admitted = crud_flow_execution.claim_execution(
            db_session,
            execution_id=second.id,
            worker_id="worker-b",
            stale_after_seconds=120,
            account_cap=1,
        )

        assert admitted is not None

    def test_resuming_a_live_agent_is_never_capped(
        self, db_session: Session, two_accounts
    ):
        """A container that exists must be monitored whatever the cap says."""
        account = list(two_accounts)[0]
        first, second, _ = two_accounts[account]

        crud_flow_execution.claim_execution(
            db_session,
            execution_id=first.id,
            worker_id="worker-a",
            stale_after_seconds=120,
            account_cap=1,
        )
        second.status = "RUNNING"
        second.agent_session_reference = "job/example-run"
        db_session.add(second)
        db_session.commit()

        resumed = crud_flow_execution.claim_execution(
            db_session,
            execution_id=second.id,
            worker_id="worker-b",
            stale_after_seconds=120,
            account_cap=1,
        )

        assert resumed is not None
        assert resumed.orchestrator_worker_id == "worker-b"

    def test_account_metadata_raises_the_cap(self, db_session: Session):
        """An operator-granted allowance is read at claim time, no restart."""
        account = _make_account(
            db_session,
            "Larger Tenant",
            meta_data={"flow_execution_max_running_per_account": 2},
        )
        flow = _make_flow(db_session, account.id)
        first, second, third = (_pending(db_session, flow.id) for _ in range(3))

        claims = [
            crud_flow_execution.claim_execution(
                db_session,
                execution_id=execution.id,
                worker_id=f"worker-{index}",
                stale_after_seconds=120,
            )
            is not None
            for index, execution in enumerate((first, second, third))
        ]

        assert claims == [True, True, False]

    def test_count_admitted_ignores_parked_runs(self, db_session: Session):
        """A parked run holds no container, so it holds no slot."""
        account = _make_account(db_session, "Parked Tenant")
        flow = _make_flow(db_session, account.id)
        parked = _pending(db_session, flow.id)
        parked.status = "WAITING_FOR_HUMAN"
        parked.orchestrator_worker_id = "worker-a"
        parked.orchestrator_heartbeat_at = datetime.now(timezone.utc)
        db_session.add(parked)
        db_session.commit()

        counts = crud_flow_execution.count_admitted_by_account(
            db_session, account_id=account.id
        )

        assert counts.get(account.id, 0) == 0


class TestTheCapIsHostedOnly:
    """Private runners are the account's own compute, not the shared pool.

    Counting an account's runner work against the hosted allowance punishes
    exactly the accounts that bring capacity: a fleet of three runners would
    silently stop the account from using hosted compute at all.
    """

    def test_fresh_settings_read_five(self, monkeypatch):
        """The deployment default, with no account override in play."""
        from preloop.config import Settings
        from preloop.services import execution_concurrency

        assert (
            Settings.model_fields["flow_execution_max_running_per_account"].default == 5
        )

        class _NoSetting:
            pass

        monkeypatch.setattr(execution_concurrency, "settings", _NoSetting())
        assert execution_concurrency.default_account_cap() == 5

    def test_five_run_and_the_sixth_is_queued(self, db_session: Session):
        """Six hosted PENDING executions, cap five, one carries the reason."""
        account = _make_account(db_session, "Hosted Tenant")
        flow = _make_flow(db_session, account.id)
        executions = [_pending(db_session, flow.id) for _ in range(6)]

        claimed = [
            crud_flow_execution.claim_execution(
                db_session,
                execution_id=execution.id,
                worker_id=f"worker-{index}",
                stale_after_seconds=120,
            )
            for index, execution in enumerate(executions)
        ]

        assert [row is not None for row in claimed] == [True] * 5 + [False]
        db_session.refresh(executions[5])
        assert executions[5].status == "PENDING"
        assert executions[5].queued_reason == QUEUED_REASON_ACCOUNT_CAP

    def test_a_runner_assigned_execution_is_claimed_at_the_hosted_cap(
        self, db_session: Session
    ):
        """Five hosted running, and the runner's own job still starts."""
        account = _make_account(db_session, "Runner Tenant")
        flow = _make_flow(db_session, account.id)
        for index in range(5):
            hosted = _pending(db_session, flow.id)
            hosted.status = "RUNNING"
            hosted.agent_session_reference = f"job/example-run-{index}"
            db_session.add(hosted)
        db_session.commit()

        assigned = _pending(db_session, flow.id)
        runner_id = uuid.uuid4()
        assigned.agent_session_reference = f"runner:{runner_id}:{assigned.id}"
        db_session.add(assigned)
        db_session.commit()

        claimed = crud_flow_execution.claim_execution(
            db_session,
            execution_id=assigned.id,
            worker_id="worker-runner",
            stale_after_seconds=120,
        )

        assert claimed is not None
        assert claimed.queued_reason is None

    def test_runner_work_does_not_fill_the_hosted_count(self, db_session: Session):
        """Runner executions leave the hosted allowance free for hosted work."""
        account = _make_account(db_session, "Mixed Tenant")
        flow = _make_flow(db_session, account.id)
        for _ in range(5):
            on_runner = _pending(db_session, flow.id)
            on_runner.status = "RUNNING"
            on_runner.agent_session_reference = f"runner:{uuid.uuid4()}:{on_runner.id}"
            db_session.add(on_runner)
        db_session.commit()

        counts = crud_flow_execution.count_admitted_by_account(
            db_session, account_id=account.id
        )
        hosted_counts = crud_flow_execution.count_admitted_by_account(
            db_session, account_id=account.id, hosted_only=True
        )
        admitted = crud_flow_execution.claim_execution(
            db_session,
            execution_id=_pending(db_session, flow.id).id,
            worker_id="worker-hosted",
            stale_after_seconds=120,
        )

        assert counts.get(account.id, 0) == 5
        assert hosted_counts.get(account.id, 0) == 0
        assert admitted is not None

    def test_a_queued_runner_execution_still_counts_as_hosted(
        self, db_session: Session
    ):
        """``runner:queued:...`` has no runner yet, so nothing bounds it there."""
        account = _make_account(db_session, "Queued Tenant")
        flow = _make_flow(db_session, account.id)
        waiting = _pending(db_session, flow.id)
        waiting.status = "RUNNING"
        waiting.agent_session_reference = f"runner:queued:linux:{waiting.id}"
        db_session.add(waiting)
        db_session.commit()

        hosted_counts = crud_flow_execution.count_admitted_by_account(
            db_session, account_id=account.id, hosted_only=True
        )

        assert hosted_counts.get(account.id, 0) == 1

    def test_the_lease_column_alone_excludes_an_execution(self, db_session: Session):
        """A lease sets ``runner_id`` before the reference lands on the row."""
        account = _make_account(db_session, "Lease Tenant")
        flow = _make_flow(db_session, account.id)
        leased = _pending(db_session, flow.id)
        leased.status = "RUNNING"
        leased.agent_session_reference = "job/example-pre-lease"
        leased.runner_id = _make_runner(db_session, account.id).id
        db_session.add(leased)
        db_session.commit()

        hosted_counts = crud_flow_execution.count_admitted_by_account(
            db_session, account_id=account.id, hosted_only=True
        )

        assert hosted_counts.get(account.id, 0) == 0


class TestRunnerNaksCappedWork:
    """A refused claim returns the message to the stream instead of acking."""

    @pytest.mark.asyncio
    async def test_account_cap_naks_with_a_delay(self):
        from preloop.services import flow_execution_runner
        from preloop.services.execution_concurrency import (
            ACCOUNT_CAP_NAK_DELAY_SECONDS,
        )

        nak = AsyncMock()
        ack = AsyncMock()

        with (
            patch.object(
                flow_execution_runner,
                "get_db_session",
                return_value=iter([MagicMock()]),
            ),
            patch.object(
                flow_execution_runner.crud_flow_execution,
                "claim_execution",
                return_value=None,
            ),
            patch.object(
                flow_execution_runner.crud_flow_execution,
                "get_queued_reason",
                return_value=QUEUED_REASON_ACCOUNT_CAP,
            ),
        ):
            result = await flow_execution_runner.claim_and_run_execution(
                str(uuid.uuid4()), ack=ack, nak=nak
            )

        assert result["status"] == "account_cap_queued"
        nak.assert_awaited_once_with(ACCOUNT_CAP_NAK_DELAY_SECONDS)
        # Acking would drop the work on the floor: it is still PENDING.
        ack.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_ordinary_lost_claim_still_acks(self):
        """A claim lost to a peer worker is not a cap refusal."""
        from preloop.services import flow_execution_runner

        nak = AsyncMock()

        with (
            patch.object(
                flow_execution_runner,
                "get_db_session",
                return_value=iter([MagicMock()]),
            ),
            patch.object(
                flow_execution_runner.crud_flow_execution,
                "claim_execution",
                return_value=None,
            ),
            patch.object(
                flow_execution_runner.crud_flow_execution,
                "get_queued_reason",
                return_value=None,
            ),
        ):
            result = await flow_execution_runner.claim_and_run_execution(
                str(uuid.uuid4()), nak=nak
            )

        assert result["status"] == "skipped"
        nak.assert_not_awaited()

    def test_the_worker_wires_a_nak_into_ack_after_claim_tasks(self):
        """The delayed nak only reaches tasks that claim before acking."""
        from preloop.sync import tasks

        assert "execute_flow" in tasks.ACK_AFTER_CLAIM_TASKS
        assert "resume_flow_execution" in tasks.ACK_AFTER_CLAIM_TASKS


class TestReaperDoesNotStorm:
    """The recovery loop must not republish what nothing can claim."""

    @staticmethod
    def _fake_dispatch(published: list):
        async def _dispatch(execution_id):
            published.append(str(execution_id))
            return True

        return _dispatch

    @pytest.mark.asyncio
    async def test_a_held_back_execution_is_published_once_per_window(
        self, db_session: Session
    ):
        """Four pending, cap one: one message, and no repeat on the next tick."""
        account = _make_account(db_session, "Storm Tenant")
        flow = _make_flow(db_session, account.id)
        executions = [_pending(db_session, flow.id) for _ in range(4)]

        service = ExecutionRecoveryService()
        published: list = []
        dispatch = self._fake_dispatch(published)

        from preloop.services import flow_execution_dispatcher

        with (
            patch.object(flow_execution_dispatcher, "dispatch_execute", dispatch),
            patch.object(flow_execution_dispatcher, "dispatch_resume", dispatch),
            patch(
                "preloop.services.execution_concurrency.account_running_cap",
                return_value=1,
            ),
        ):
            first_tick = await service._redispatch_stale_executions(
                db_session, stale_after_seconds=120
            )
            # A worker picks the message up and admits it.
            crud_flow_execution.claim_execution(
                db_session,
                execution_id=executions[0].id,
                worker_id="worker-a",
                stale_after_seconds=120,
                account_cap=1,
            )
            second_tick = await service._redispatch_stale_executions(
                db_session, stale_after_seconds=120
            )

        # One free slot, so one message. The other three stay PENDING and
        # unpublished instead of being republished on every 30 second tick.
        assert first_tick == 1
        assert published == [str(executions[0].id)]
        assert second_tick == 0
        assert len(published) == 1

    @pytest.mark.asyncio
    async def test_backoff_holds_even_when_nobody_claims(self, db_session: Session):
        """The same id is not republished twice inside one stale window."""
        account = _make_account(db_session, "Backoff Tenant")
        flow = _make_flow(db_session, account.id)
        execution = _pending(db_session, flow.id)

        service = ExecutionRecoveryService()
        published: list = []
        dispatch = self._fake_dispatch(published)

        from preloop.services import flow_execution_dispatcher

        with (
            patch.object(flow_execution_dispatcher, "dispatch_execute", dispatch),
            patch.object(flow_execution_dispatcher, "dispatch_resume", dispatch),
        ):
            for _ in range(3):
                await service._redispatch_stale_executions(
                    db_session, stale_after_seconds=120
                )

        assert published.count(str(execution.id)) == 1

    @pytest.mark.asyncio
    async def test_a_live_agent_is_redispatched_whatever_the_cap_says(
        self, db_session: Session
    ):
        """An unmonitored container is worse than being one over the cap."""
        account = _make_account(db_session, "Live Agent Tenant")
        flow = _make_flow(db_session, account.id)

        monitored = _pending(db_session, flow.id)
        monitored.status = "RUNNING"
        monitored.agent_session_reference = "job/example-run-one"
        monitored.orchestrator_worker_id = "worker-a"
        monitored.orchestrator_heartbeat_at = datetime.now(timezone.utc)
        orphaned = _pending(db_session, flow.id)
        orphaned.status = "RUNNING"
        orphaned.agent_session_reference = "job/example-run-two"
        db_session.add_all([monitored, orphaned])
        db_session.commit()

        service = ExecutionRecoveryService()
        published: list = []
        dispatch = self._fake_dispatch(published)

        from preloop.services import flow_execution_dispatcher

        with (
            patch.object(flow_execution_dispatcher, "dispatch_execute", dispatch),
            patch.object(flow_execution_dispatcher, "dispatch_resume", dispatch),
            patch(
                "preloop.services.execution_concurrency.account_running_cap",
                return_value=1,
            ),
        ):
            dispatched = await service._redispatch_stale_executions(
                db_session, stale_after_seconds=120
            )

        # The account is already at its cap because of ``monitored``, and the
        # orphan is re-dispatched anyway: it has a container to adopt.
        assert dispatched == 1
        assert published == [str(orphaned.id)]

    @pytest.mark.asyncio
    async def test_the_backoff_is_recorded_on_the_execution(self, db_session: Session):
        """The backoff lives on the row, not in one process's memory.

        It used to be a per-process dict, which bounded one worker and
        nothing else. On the row, every replica reads the same counter.
        """
        account = _make_account(db_session, "Recorded Backoff Tenant")
        flow = _make_flow(db_session, account.id)
        execution = _pending(db_session, flow.id)

        service = ExecutionRecoveryService()
        published: list = []
        dispatch = self._fake_dispatch(published)

        from preloop.services import flow_execution_dispatcher

        with (
            patch.object(flow_execution_dispatcher, "dispatch_execute", dispatch),
            patch.object(flow_execution_dispatcher, "dispatch_resume", dispatch),
        ):
            await service._redispatch_stale_executions(
                db_session, stale_after_seconds=120
            )

        db_session.refresh(execution)
        assert execution.redispatch_count == 1
        assert execution.last_redispatch_at is not None
