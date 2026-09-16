"""Where an approval raised inside a child execution goes (#635).

These tests pin the behaviour the design note
``docs/guide/flow-delegation-child-approvals.md`` reasons from, so the
recommendations it makes for the parking child of #621 cannot quietly stop
being true:

* a question is routed by the account, never by the caller's lineage
* the decision window is bounded by the account cap and knows nothing about a
  parent's remaining wait
* ``WAITING_FOR_HUMAN`` is an interrupted child, not a finished one, and the
  parked row is closed without the result
* the continuation of a parked run carries the parked run's trigger context
  and its lineage, unchanged (#707)

Nothing here exercises a database or a network: the point is the contract.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from preloop.a2a.delegation import task_state_for_status
from preloop.models.crud import crud_flow_execution
from preloop.models.crud.approval_workflow import CRUDApprovalWorkflow
from preloop.models.models.approval_request import ApprovalRequest
from preloop.services import approval_park
from preloop.services.approval_attribution import (
    CallerAttribution,
    attribution_from_user_context,
)
from preloop.services.approval_service import ApprovalService
from preloop.services.approval_window import account_window_cap, resolve_approval_window

UTC = timezone.utc

NOTE_PATH = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "guide"
    / "flow-delegation-child-approvals.md"
)

LINEAGE_NAMES = {"parent_execution_id", "root_execution_id", "delegation_depth"}


def _child_user_context(execution_id: uuid.UUID) -> SimpleNamespace:
    """The MCP context a delegated child's runtime token produces."""
    return SimpleNamespace(
        flow_execution_id=str(execution_id),
        managed_agent_id=None,
        runtime_session_id=None,
        api_key_id=str(uuid.uuid4()),
        runtime_principal_name="Repository review",
    )


class TestRoutingIsAccountScoped:
    """A child's question reaches the account's approvers, like every other."""

    def test_the_request_records_the_asking_execution_and_no_ancestry(self):
        child_id = uuid.uuid4()
        caller = attribution_from_user_context(_child_user_context(child_id))
        assert caller.execution_id == str(child_id)
        assert LINEAGE_NAMES.isdisjoint(CallerAttribution.__dataclass_fields__)

    def test_the_approval_row_has_no_lineage_columns(self):
        """Lineage lives on flow_execution; the console joins, it does not copy."""
        columns = {column.name for column in ApprovalRequest.__table__.columns}
        assert "execution_id" in columns
        assert LINEAGE_NAMES.isdisjoint(columns)

    def test_workflow_lookup_takes_an_account_and_never_an_execution(self):
        by_name = set(inspect.signature(CRUDApprovalWorkflow.get_by_name).parameters)
        default = set(inspect.signature(CRUDApprovalWorkflow.get_default).parameters)
        assert by_name == {"self", "db", "account_id", "name"}
        assert default == {"self", "db", "account_id"}

    def test_approvers_come_from_the_workflow_alone(self):
        params = set(
            inspect.signature(ApprovalService._get_all_approver_user_ids).parameters
        )
        assert params == {"self", "approval_workflow"}


class TestWindowIsBoundedAndParentFree:
    """The child keeps its own window, and that window always closes."""

    def test_the_resolver_takes_no_parent_argument(self):
        params = set(inspect.signature(resolve_approval_window).parameters)
        assert params == {"requested_seconds", "flow", "workflow", "account"}

    def test_the_window_comes_from_the_child_s_own_flow(self):
        window = resolve_approval_window(
            flow=SimpleNamespace(approval_window_seconds=86400),
            workflow=SimpleNamespace(timeout_seconds=600),
        )
        assert window.seconds == 86400
        assert window.source == "flow"

    def test_no_window_outlives_the_account_cap(self):
        account = SimpleNamespace(meta_data={"approval_window_max_seconds": 3600})
        window = resolve_approval_window(requested_seconds=10**9, account=account)
        assert window.seconds == account_window_cap(account) == 3600
        assert window.capped is True

    def test_a_parked_child_always_tells_the_agent_when_the_window_closes(self):
        expires_at = datetime(2026, 9, 15, 10, 0, tzinfo=UTC) + timedelta(days=1)
        payload = approval_park.park_pending_payload(
            request_id=uuid.uuid4(),
            tool_name="ask_user",
            expires_at=expires_at,
            console_url="https://example.com/console/approvals/1",
            question="Waive CVE-2026-1234?",
        )
        assert expires_at.isoformat() in payload
        assert "do not poll" in payload


class _FakeQuery:
    """Captures the filters and values of one conditional UPDATE."""

    def __init__(self, owner: "_FakeDB") -> None:
        self._owner = owner

    def filter(self, *criteria):
        return self

    def update(self, values, synchronize_session=False):
        self._owner.updates.append(
            {getattr(key, "key", str(key)) for key in values.keys()}
        )
        return 1


class _FakeDB:
    def __init__(self) -> None:
        self.updates: list = []

    def query(self, *entities):
        return _FakeQuery(self)

    def commit(self) -> None:
        return None


class TestWhatTheParentSees:
    """A child waiting for a person is interrupted, not finished."""

    def test_waiting_for_human_is_input_required(self):
        assert task_state_for_status("WAITING_FOR_HUMAN") == "TASK_STATE_INPUT_REQUIRED"

    def test_waiting_for_human_is_not_terminal(self):
        from preloop.services.flow_orchestrator import TERMINAL_EXECUTION_STATUSES

        assert "WAITING_FOR_HUMAN" not in TERMINAL_EXECUTION_STATUSES
        assert "WAITING_FOR_HUMAN" not in crud_flow_execution.PARK_PARENT_CLOSE_STATUSES

    def test_the_parked_row_is_closed_without_a_result(self):
        """So a parent must read the outcome off the continuation instead."""
        db = _FakeDB()
        crud_flow_execution.close_parked_parent_for_resume(
            db,
            resume_execution_id=uuid.uuid4(),
            status="SUCCEEDED",
            end_time=datetime.now(UTC),
        )
        assert db.updates == [{"status", "end_time"}]


def _parked_child(**overrides) -> SimpleNamespace:
    """A delegated child that parked on a question, lineage and all."""
    root_id = uuid.uuid4()
    base = dict(
        id=uuid.uuid4(),
        flow_id=uuid.uuid4(),
        trigger_event_details={"payload": {"repository": "example/widget"}},
        cli_session=None,
        parked_compute_seconds=140,
        park_request_id=uuid.uuid4(),
        start_time=datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
        parked_at=datetime(2026, 9, 15, 10, 2, tzinfo=UTC),
        parent_execution_id=root_id,
        root_execution_id=root_id,
        delegation_depth=1,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
class TestTheContinuation:
    """The decision starts a new execution; what that execution carries."""

    async def _create_call(self, monkeypatch, parked):
        from preloop.models import crud as crud_pkg

        crud_exec = MagicMock()
        crud_exec.create.return_value = SimpleNamespace(id=uuid.uuid4())
        crud_exec.mark_park_resumed.return_value = True
        monkeypatch.setattr(crud_pkg, "crud_flow_execution", crud_exec)
        monkeypatch.setattr(
            "preloop.services.model_routing.prepare_execution_routing",
            lambda db, flow, details, **kwargs: details,
        )
        monkeypatch.setattr(
            "preloop.services.flow_execution_dispatcher.flow_execution_worker_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "preloop.services.flow_execution_dispatcher.dispatch_execute",
            AsyncMock(return_value=None),
        )
        await approval_park._start_resume_execution(
            MagicMock(),
            SimpleNamespace(id=parked.flow_id),
            parked,
            approval_park.build_resume_details(
                parked,
                {
                    "request_id": str(parked.park_request_id),
                    "status": "approved",
                    "tool_name": "ask_user",
                    "answer": "waived",
                    "answered_at": "2026-09-15T11:00:00+00:00",
                },
            ),
        )
        return crud_exec.create.call_args.kwargs["obj_in"]

    async def test_the_continuation_keeps_the_child_s_trigger_context(
        self, monkeypatch
    ):
        parked = _parked_child()
        obj_in = await self._create_call(monkeypatch, parked)
        assert obj_in.flow_id == parked.flow_id
        assert obj_in.status == "PENDING"
        details = obj_in.trigger_event_details
        assert details["_resume"]["execution_id"] == str(parked.id)
        assert details["payload"]["repository"] == "example/widget"

    async def test_the_continuation_carries_the_child_s_lineage(self, monkeypatch):
        """#707: the lineage crosses the park unchanged, it is not re-parented.

        A continuation is the same logical child carrying on rather than a new
        child of the run it continues, so the depth is copied and never
        incremented: the park link is already expressed by
        ``resume_execution_id``.
        """
        parked = _parked_child()
        obj_in = await self._create_call(monkeypatch, parked)
        assert obj_in.parent_execution_id == parked.parent_execution_id
        assert obj_in.root_execution_id == parked.root_execution_id
        assert obj_in.delegation_depth == parked.delegation_depth == 1

    async def test_a_root_run_continues_without_a_lineage(self, monkeypatch):
        """No lineage to carry, and none invented: null, null and depth 0."""
        parked = _parked_child(
            parent_execution_id=None, root_execution_id=None, delegation_depth=0
        )
        obj_in = await self._create_call(monkeypatch, parked)
        assert obj_in.parent_execution_id is None
        assert obj_in.root_execution_id is None
        assert obj_in.delegation_depth == 0

    async def test_a_row_predating_the_lineage_columns_still_continues(
        self, monkeypatch
    ):
        """Read defensively: an old parked row has no lineage attributes."""
        parked = _parked_child()
        for name in LINEAGE_NAMES:
            delattr(parked, name)
        obj_in = await self._create_call(monkeypatch, parked)
        assert obj_in.parent_execution_id is None
        assert obj_in.root_execution_id is None
        assert obj_in.delegation_depth == 0


class TestTheNote:
    """The spike's deliverable is the note; keep it beside the pins."""

    def test_the_note_answers_all_three_questions(self):
        text = NOTE_PATH.read_text(encoding="utf-8")
        assert "approval workflow and requesting subject" in text
        assert "parent and root execution ids" in text
        assert "parent's remaining child wait" in text
        assert "TASK_STATE_INPUT_REQUIRED" in text
