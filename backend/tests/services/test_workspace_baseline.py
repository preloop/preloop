"""Tests for resolving a review baseline from a previous execution id.

Covers the account-scoped read behind
``previous_result_execution_id``: what resolves, what degrades to a
mismatch marker, and the precedence of an explicitly delivered baseline
file. The transport (chunked environment, emitted shell) is pinned in
tests/utils/test_workspace_baseline.py.
"""

import base64
import json
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
from preloop.models.models import Account, Flow
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import (
    FlowExecutionCreate,
    FlowExecutionUpdate,
)
from preloop.services.workspace_baseline import resolve_baseline_delivery
from preloop.utils.workspace_baseline import (
    BASELINE_WORKSPACE_PATH,
    MAX_BASELINE_RESULT_BYTES,
    MISMATCH_NO_RESULT,
    MISMATCH_TOO_LARGE,
    MISMATCH_UNAVAILABLE,
)

BASELINE_RESULT = {
    "schema": "preloop.review.codehealth/v1",
    "findings": [{"id": "health:quality:app/main.py:long-function"}],
}


def _account(db_session: Session) -> Account:
    return crud_account.create(
        db_session,
        obj_in={"organization_name": f"Org {uuid4().hex[:8]}", "is_active": True},
    )


def _flow(db_session: Session, account: Account) -> Flow:
    flow_in = FlowCreate(
        name=f"Review Flow {uuid4().hex[:8]}",
        description="Repo code health review",
        trigger_event_source="webhook",
        trigger_event_types=["webhook"],
        prompt_template="Review the repository",
        agent_type="openhands",
        agent_config={"max_iterations": 10},
    )
    return crud_flow.create(db=db_session, flow_in=flow_in, account_id=account.id)


def _execution(db_session: Session, flow: Flow, result=None):
    execution = crud_flow_execution.create(
        db_session, obj_in=FlowExecutionCreate(flow_id=flow.id, status="COMPLETED")
    )
    if result is not None:
        crud_flow_execution.update(
            db_session,
            db_obj=execution,
            obj_in=FlowExecutionUpdate(result=result),
        )
    return execution


@pytest.fixture
def account(db_session: Session) -> Account:
    return _account(db_session)


@pytest.fixture
def flow(db_session: Session, account: Account) -> Flow:
    return _flow(db_session, account)


def _payload(**keys):
    return {"source": "webhook", "type": "webhook", "payload": keys}


def _resolve(db_session, flow, trigger_event_data, **kwargs):
    return resolve_baseline_delivery(
        db_session,
        trigger_event_data=trigger_event_data,
        account_id=flow.account_id,
        flow_id=flow.id,
        **kwargs,
    )


class TestBaselineResolution:
    """A valid id inside the account delivers that execution's result."""

    def test_no_key_delivers_nothing(self, db_session: Session, flow: Flow):
        assert _resolve(db_session, flow, _payload(depth="quick")) is None

    def test_valid_execution_id_delivers_the_stored_result(
        self, db_session: Session, flow: Flow
    ):
        previous = _execution(db_session, flow, result=BASELINE_RESULT)

        delivery = _resolve(
            db_session,
            flow,
            _payload(previous_result_execution_id=str(previous.id)),
        )

        assert delivery is not None
        assert delivery.delivered
        assert delivery.path == BASELINE_WORKSPACE_PATH
        assert delivery.source_execution_id == str(previous.id)
        assert json.loads(base64.b64decode(delivery.content_base64)) == BASELINE_RESULT

    def test_sentinel_picks_the_latest_run_with_a_result(
        self, db_session: Session, flow: Flow
    ):
        _execution(db_session, flow, result={"schema": "old", "findings": []})
        newest = _execution(db_session, flow, result=BASELINE_RESULT)
        _execution(db_session, flow)  # no result: never a baseline

        delivery = _resolve(
            db_session, flow, _payload(previous_result_execution_id="last")
        )

        assert delivery is not None and delivery.delivered
        assert delivery.source_execution_id == str(newest.id)

    def test_missing_account_scope_is_unresolvable_not_unscoped(
        self, db_session: Session, flow: Flow
    ):
        """A NULL account_id must not fall through to an unfiltered get."""
        previous = _execution(db_session, flow, result=BASELINE_RESULT)

        delivery = resolve_baseline_delivery(
            db_session,
            trigger_event_data=_payload(previous_result_execution_id=str(previous.id)),
            account_id=None,
            flow_id=flow.id,
        )

        assert delivery is not None
        assert delivery.mismatch_reason == MISMATCH_UNAVAILABLE
        assert delivery.content_base64 == ""
        assert delivery.source_execution_id is None

    def test_missing_account_scope_refuses_the_sentinel(
        self, db_session: Session, flow: Flow
    ):
        _execution(db_session, flow, result=BASELINE_RESULT)

        delivery = resolve_baseline_delivery(
            db_session,
            trigger_event_data=_payload(previous_result_execution_id="last"),
            account_id=None,
            flow_id=flow.id,
        )

        assert delivery is not None
        assert delivery.mismatch_reason == MISMATCH_UNAVAILABLE
        assert delivery.content_base64 == ""

    def test_sentinel_never_picks_the_current_execution(
        self, db_session: Session, flow: Flow
    ):
        current = _execution(db_session, flow, result=BASELINE_RESULT)

        delivery = _resolve(
            db_session,
            flow,
            _payload(previous_result_execution_id="last"),
            exclude_execution_id=current.id,
        )

        assert delivery is not None
        assert delivery.mismatch_reason == MISMATCH_UNAVAILABLE

    def test_sentinel_on_a_first_run_degrades(self, db_session: Session, flow: Flow):
        delivery = _resolve(
            db_session, flow, _payload(previous_result_execution_id="last")
        )

        assert delivery is not None
        assert delivery.mismatch_reason == MISMATCH_UNAVAILABLE


class TestBaselineDegrades:
    """Nothing about a baseline may fail a run."""

    def test_foreign_execution_is_treated_as_missing(
        self, db_session: Session, flow: Flow
    ):
        other_account = _account(db_session)
        other_flow = _flow(db_session, other_account)
        foreign = _execution(db_session, other_flow, result=BASELINE_RESULT)

        delivery = _resolve(
            db_session, flow, _payload(previous_result_execution_id=str(foreign.id))
        )

        assert delivery is not None
        assert delivery.mismatch_reason == MISMATCH_UNAVAILABLE
        assert delivery.content_base64 == ""
        assert delivery.source_execution_id is None

    def test_foreign_and_unknown_ids_are_indistinguishable(
        self, db_session: Session, flow: Flow
    ):
        """The marker may not reveal that a foreign execution exists."""
        other_account = _account(db_session)
        other_flow = _flow(db_session, other_account)
        foreign = _execution(db_session, other_flow, result=BASELINE_RESULT)

        for_foreign = _resolve(
            db_session, flow, _payload(previous_result_execution_id=str(foreign.id))
        )
        for_unknown = _resolve(
            db_session, flow, _payload(previous_result_execution_id=str(uuid4()))
        )

        assert for_foreign.model_dump() == for_unknown.model_dump()

    def test_execution_without_a_result(self, db_session: Session, flow: Flow):
        resultless = _execution(db_session, flow)

        delivery = _resolve(
            db_session, flow, _payload(previous_result_execution_id=str(resultless.id))
        )

        assert delivery.mismatch_reason == MISMATCH_NO_RESULT

    def test_malformed_id(self, db_session: Session, flow: Flow):
        delivery = _resolve(
            db_session, flow, _payload(previous_result_execution_id="not-a-uuid")
        )

        assert delivery.mismatch_reason == MISMATCH_UNAVAILABLE

    def test_non_string_id(self, db_session: Session, flow: Flow):
        delivery = _resolve(
            db_session, flow, _payload(previous_result_execution_id={"id": 1})
        )

        assert delivery.mismatch_reason == MISMATCH_UNAVAILABLE

    def test_result_over_the_cap_is_refused_not_truncated(
        self, db_session: Session, flow: Flow
    ):
        oversized = _execution(
            db_session,
            flow,
            result={"findings": ["x" * 200] * (MAX_BASELINE_RESULT_BYTES // 200)},
        )

        delivery = _resolve(
            db_session, flow, _payload(previous_result_execution_id=str(oversized.id))
        )

        assert delivery.mismatch_reason == MISMATCH_TOO_LARGE
        assert delivery.content_base64 == ""

    def test_result_just_under_the_cap_is_delivered(
        self, db_session: Session, flow: Flow
    ):
        sized = _execution(
            db_session,
            flow,
            result={"findings": ["x" * 1000] * 100},
        )

        delivery = _resolve(
            db_session, flow, _payload(previous_result_execution_id=str(sized.id))
        )

        assert delivery.delivered


class TestExplicitFileWins:
    """Documented precedence: an attached baseline file beats the id."""

    def test_previous_result_path_wins(self, db_session: Session, flow: Flow):
        previous = _execution(db_session, flow, result=BASELINE_RESULT)

        delivery = _resolve(
            db_session,
            flow,
            _payload(
                previous_result_execution_id=str(previous.id),
                previous_result_path=BASELINE_WORKSPACE_PATH,
            ),
            seed_paths=[BASELINE_WORKSPACE_PATH],
        )

        assert delivery is None

    def test_previous_result_url_wins(self, db_session: Session, flow: Flow):
        previous = _execution(db_session, flow, result=BASELINE_RESULT)

        delivery = _resolve(
            db_session,
            flow,
            _payload(
                previous_result_execution_id=str(previous.id),
                previous_result_url="https://example.com/result.json",
            ),
        )

        assert delivery is None

    def test_seeded_baseline_file_wins(self, db_session: Session, flow: Flow):
        previous = _execution(db_session, flow, result=BASELINE_RESULT)

        delivery = _resolve(
            db_session,
            flow,
            _payload(previous_result_execution_id=str(previous.id)),
            seed_paths=[BASELINE_WORKSPACE_PATH],
        )

        assert delivery is None

    def test_unrelated_seed_does_not_block_the_baseline(
        self, db_session: Session, flow: Flow
    ):
        previous = _execution(db_session, flow, result=BASELINE_RESULT)

        delivery = _resolve(
            db_session,
            flow,
            _payload(previous_result_execution_id=str(previous.id)),
            seed_paths=["fixtures/input.json"],
        )

        assert delivery is not None and delivery.delivered


class TestLatestSucceededWithResult:
    """``succeeded_only`` backs the Delivery Report's previous report (#2019)."""

    def test_a_failed_run_is_not_the_previous_report(
        self, db_session: Session, flow: Flow
    ):
        good = _execution(db_session, flow, result=BASELINE_RESULT)
        crud_flow_execution.update(
            db_session, db_obj=good, obj_in=FlowExecutionUpdate(status="SUCCEEDED")
        )
        failed = _execution(db_session, flow, result={"status": "error"})
        crud_flow_execution.update(
            db_session, db_obj=failed, obj_in=FlowExecutionUpdate(status="FAILED")
        )

        latest = crud_flow_execution.latest_with_result(
            db_session, flow_id=flow.id, account_id=str(flow.account_id)
        )
        succeeded = crud_flow_execution.latest_with_result(
            db_session,
            flow_id=flow.id,
            account_id=str(flow.account_id),
            succeeded_only=True,
        )

        assert latest is not None and latest.id == failed.id
        assert succeeded is not None and succeeded.id == good.id
