"""Envelope shape, one test per v1 event type.

These lock the contract a receiver parses. Changing a field here is a
breaking change for every integration and belongs in a version 2 envelope,
not in an edit to these assertions.
"""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from preloop.models.models.webhook_endpoint import WebhookDelivery
from preloop.services.event_webhooks import emitters, outbox
from preloop.services.event_webhooks.events import (
    ENVELOPE_VERSION,
    EVENT_APPROVAL_CREATED,
    EVENT_APPROVAL_DECIDED,
    EVENT_BUDGET_EXCEEDED,
    EVENT_BUDGET_THRESHOLD,
    EVENT_FLOW_EXECUTION_FINISHED,
    EVENT_POLICY_DENIED,
    EVENT_SESSION_ENDED,
    EVENT_TYPES_V1,
    build_envelope,
)

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


def _only_payload(db_session, account_id):
    rows = (
        db_session.query(WebhookDelivery)
        .filter(WebhookDelivery.account_id == account_id)
        .all()
    )
    assert len(rows) == 1, f"expected one delivery, got {len(rows)}"
    return rows[0].payload


def _assert_envelope(payload, *, event_type, account_id):
    assert set(payload) == {
        "id",
        "type",
        "version",
        "occurred_at",
        "account_id",
        "data",
    }
    assert payload["type"] == event_type
    assert payload["version"] == ENVELOPE_VERSION
    assert payload["account_id"] == str(account_id)
    uuid.UUID(payload["id"])
    datetime.fromisoformat(payload["occurred_at"])
    assert isinstance(payload["data"], dict)


# --- the envelope itself ---------------------------------------------------


def test_envelope_has_exactly_the_documented_keys():
    envelope = build_envelope(
        event_id=uuid.uuid4(),
        event_type=EVENT_POLICY_DENIED,
        account_id=uuid.uuid4(),
        data={"tool_name": "shell"},
        occurred_at=NOW,
    )
    assert list(envelope) == [
        "id",
        "type",
        "version",
        "occurred_at",
        "account_id",
        "data",
    ]
    assert envelope["occurred_at"] == "2026-09-08T12:00:00+00:00"


def test_envelope_normalises_a_naive_timestamp_to_utc():
    envelope = build_envelope(
        event_id=uuid.uuid4(),
        event_type=EVENT_POLICY_DENIED,
        account_id=uuid.uuid4(),
        data={},
        occurred_at=datetime(2026, 9, 8, 12, 0, 0),
    )
    assert envelope["occurred_at"].endswith("+00:00")


def test_the_v1_catalogue_is_the_documented_list():
    assert EVENT_TYPES_V1 == (
        EVENT_APPROVAL_CREATED,
        EVENT_APPROVAL_DECIDED,
        EVENT_POLICY_DENIED,
        EVENT_SESSION_ENDED,
        EVENT_BUDGET_THRESHOLD,
        EVENT_BUDGET_EXCEEDED,
        EVENT_FLOW_EXECUTION_FINISHED,
    )


# --- one per event type ----------------------------------------------------


def _approval(**overrides):
    base = dict(
        id=uuid.uuid4(),
        account_id=None,
        status="pending",
        tool_name="deploy",
        summary="Deploy to production",
        approval_workflow_id=uuid.uuid4(),
        tool_configuration_id=uuid.uuid4(),
        execution_id="exec-1",
        managed_agent_id=uuid.uuid4(),
        managed_agent_name="Release bot",
        runtime_session_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        requested_at=datetime(2026, 9, 8, 11, 0, 0),
        expires_at=datetime(2026, 9, 8, 11, 5, 0),
        resolved_at=None,
        approver_comment=None,
        responses=None,
        decided_by_ai=False,
        ai_model=None,
        ai_confidence=None,
        auto_approved_reason=None,
        auto_approval_bypass_id=None,
        rule_context=None,
        tool_args={"target": "prod", "token": "secret-value"},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_approval_created_envelope(db_session, account, make_endpoint):
    make_endpoint()
    request = _approval(account_id=account.id)

    await emitters.emit_approval_event_async(
        FakeAsync(db_session), request, EVENT_APPROVAL_CREATED
    )

    payload = _only_payload(db_session, account.id)
    _assert_envelope(payload, event_type=EVENT_APPROVAL_CREATED, account_id=account.id)
    data = payload["data"]
    assert data["approval_request_id"] == str(request.id)
    assert data["tool_name"] == "deploy"
    assert data["summary"] == "Deploy to production"
    assert data["managed_agent_name"] == "Release bot"
    assert data["requested_at"] == "2026-09-08T11:00:00+00:00"
    assert data["rule"] is None


@pytest.mark.asyncio
async def test_approval_decided_envelope(db_session, account, make_endpoint):
    make_endpoint()
    request = _approval(
        account_id=account.id,
        status="approved",
        resolved_at=datetime(2026, 9, 8, 11, 2, 0),
        approver_comment="looks fine",
        responses=[{"user_id": "11111111-1111-1111-1111-111111111111"}],
        rule_context={
            "source": "rule_match",
            "rule_id": "22222222-2222-2222-2222-222222222222",
            "rule_name": "Production deploys need a human",
            "expression": "args.target == 'prod'",
            "priority": 10,
        },
    )

    await emitters.emit_approval_event_async(
        FakeAsync(db_session), request, EVENT_APPROVAL_DECIDED
    )

    payload = _only_payload(db_session, account.id)
    _assert_envelope(payload, event_type=EVENT_APPROVAL_DECIDED, account_id=account.id)
    data = payload["data"]
    assert data["decision"] == "approved"
    assert data["resolved_at"] == "2026-09-08T11:02:00+00:00"
    assert data["actor"] == {
        "kind": "user",
        "id": "11111111-1111-1111-1111-111111111111",
    }
    assert data["rule"]["rule_name"] == "Production deploys need a human"
    assert data["comment"] == "looks fine"


def test_policy_denied_envelope(db_session, account, make_endpoint):
    make_endpoint()

    outbox.enqueue_event(
        db_session,
        account_id=account.id,
        event_type=EVENT_POLICY_DENIED,
        data=emitters.policy_denied_data(
            tool_name="shell",
            rule_description="No shell in production",
            condition_matched="args.cmd contains 'rm'",
            execution_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="corr-1",
            extra_details={"source": "policy_evaluator"},
        ),
    )

    payload = _only_payload(db_session, account.id)
    _assert_envelope(payload, event_type=EVENT_POLICY_DENIED, account_id=account.id)
    data = payload["data"]
    assert data["decision"] == "deny"
    assert data["tool_name"] == "shell"
    assert data["rule_description"] == "No shell in production"
    assert data["details"] == {"source": "policy_evaluator"}


def test_session_ended_envelope(db_session, account, make_endpoint):
    make_endpoint()
    session = SimpleNamespace(
        id=uuid.uuid4(),
        account_id=account.id,
        session_source_type="flow_execution",
        session_source_id="exec-9",
        runtime_principal_type="flow_execution",
        runtime_principal_name="Nightly audit",
        runtime_principal_id="exec-9",
        started_at=datetime(2026, 9, 8, 10, 0, 0),
        ended_at=datetime(2026, 9, 8, 10, 30, 0),
    )

    emitters.emit_session_ended(db_session, session, reason="execution_finished")

    payload = _only_payload(db_session, account.id)
    _assert_envelope(payload, event_type=EVENT_SESSION_ENDED, account_id=account.id)
    data = payload["data"]
    assert data["runtime_session_id"] == str(session.id)
    assert data["reason"] == "execution_finished"
    assert data["duration_seconds"] == 1800
    assert data["runtime_principal_name"] == "Nightly audit"


def test_budget_threshold_envelope(db_session, account, make_endpoint):
    make_endpoint()

    emitters.emit_budget_event(
        db_session,
        account_id=account.id,
        exceeded=False,
        scope="account",
        period="2026-09",
        limit_amount=100.0,
        spent_amount=80.0,
        threshold_percent=80,
    )

    payload = _only_payload(db_session, account.id)
    _assert_envelope(payload, event_type=EVENT_BUDGET_THRESHOLD, account_id=account.id)
    data = payload["data"]
    assert data["scope"] == "account"
    assert data["period"] == "2026-09"
    assert data["limit_amount"] == 100.0
    assert data["spent_amount"] == 80.0
    assert data["percent_used"] == 80.0
    assert data["threshold_percent"] == 80
    assert data["currency"] == "USD"


def test_budget_exceeded_envelope(db_session, account, make_endpoint):
    make_endpoint()

    emitters.emit_budget_event(
        db_session,
        account_id=account.id,
        exceeded=True,
        scope="flow",
        scope_id="exec-3",
        period="2026-09",
        limit_amount=50.0,
        spent_amount=61.5,
    )

    payload = _only_payload(db_session, account.id)
    _assert_envelope(payload, event_type=EVENT_BUDGET_EXCEEDED, account_id=account.id)
    data = payload["data"]
    assert data["scope"] == "flow"
    assert data["scope_id"] == "exec-3"
    assert data["percent_used"] == 123.0
    assert data["threshold_percent"] is None


def test_flow_execution_finished_envelope(db_session, account, make_endpoint):
    make_endpoint()
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        trigger_type="schedule",
        start_time=datetime(2026, 9, 8, 9, 0, 0),
        end_time=datetime(2026, 9, 8, 9, 15, 0),
        evidence_receipt={
            "version": 1,
            "kind": "evidence",
            "status": "available",
            "transport": "object_store",
            "artifact_id": "art-1",
            "sha256": "a" * 64,
            "manifest_sha256": "b" * 64,
            "size_bytes": 4096,
            "created_at": "2026-09-08T09:15:00+00:00",
            "expires_at": "2026-09-22T09:15:00+00:00",
            "retention_hours": 336,
            "object_lock": False,
            "legal_hold": False,
            "integrity_verified": False,
        },
    )
    flow = SimpleNamespace(id=uuid.uuid4(), account_id=account.id, name="Nightly audit")

    emitters.emit_flow_execution_finished(
        db_session, execution, flow, status="completed"
    )

    payload = _only_payload(db_session, account.id)
    _assert_envelope(
        payload, event_type=EVENT_FLOW_EXECUTION_FINISHED, account_id=account.id
    )
    data = payload["data"]
    assert data["status"] == "completed"
    assert data["flow_name"] == "Nightly audit"
    assert data["failure_category"] is None
    receipt = data["evidence_receipt"]
    assert receipt["status"] == "available"
    assert receipt["sha256"] == "a" * 64
    assert receipt["integrity_verified"] is False
    # The receipt travels without a claim that anything was verified in
    # transit, and without any payload bytes.
    assert "digest" not in receipt
    assert receipt["legal_hold"] is False


def test_flow_execution_without_a_receipt_says_none(db_session, account, make_endpoint):
    make_endpoint()
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        trigger_type="manual",
        start_time=None,
        end_time=None,
        evidence_receipt=None,
    )
    flow = SimpleNamespace(id=uuid.uuid4(), account_id=account.id, name="Ad hoc")

    emitters.emit_flow_execution_finished(db_session, execution, flow, status="failed")

    data = _only_payload(db_session, account.id)["data"]
    assert data["evidence_receipt"] is None
    assert data["status"] == "failed"


# --- actor derivation ------------------------------------------------------


@pytest.mark.parametrize(
    "overrides,expected_kind",
    [
        ({"status": "expired"}, "system"),
        ({"status": "cancelled"}, "system"),
        ({"status": "approved", "decided_by_ai": True, "ai_model": "gpt-x"}, "ai"),
        ({"status": "approved", "auto_approved_reason": "bypass"}, "bypass"),
    ],
)
def test_decision_actor_kinds(overrides, expected_kind):
    actor = emitters._decision_actor(_approval(**overrides))
    assert actor["kind"] == expected_kind


def test_approval_payloads_never_carry_tool_arguments():
    request = _approval(status="approved")
    for data in (
        emitters.approval_created_data(request),
        emitters.approval_decided_data(request),
    ):
        assert "tool_args" not in data
        assert "secret-value" not in str(data)


class FakeAsync:
    """Minimal async facade over the sync test session."""

    def __init__(self, session):
        self._session = session

    async def execute(self, statement, *args, **kwargs):
        return self._session.execute(statement, *args, **kwargs)

    async def commit(self):
        self._session.flush()
