"""Owner consent must precede every private runner assignment."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from preloop.models.crud import crud_flow_execution, crud_flow_runner
from preloop.models.crud import owner_consent
from preloop.services import owner_consent as consent_service
from preloop.services.runner_service import lease_job


@pytest.mark.parametrize(
    ("mode", "status", "leased"),
    [
        ("always", "pending", True),
        ("never", "approved", False),
        ("ask_me", "pending", False),
        ("ask_me", "approved", True),
        ("ask_me", "declined", False),
        ("ask_me", "expired", False),
        (None, "pending", False),
    ],
)
def test_consent_precedes_lease(monkeypatch, mode, status, leased) -> None:
    runner = SimpleNamespace(
        id=uuid4(),
        registered_by_user_id=uuid4(),
        flow_execution_consent=mode,
        status="online",
        free_slots=1,
        capabilities={"docker_available": True},
        accept_untagged=True,
    )
    monkeypatch.setattr(
        crud_flow_execution, "admit_runtime_start", lambda *a, **k: True
    )
    monkeypatch.setattr(crud_flow_runner, "find_matching", lambda *a, **k: [runner])
    monkeypatch.setattr(owner_consent, "runner_for_execution", lambda *a, **k: None)
    gate = MagicMock(
        return_value=consent_service.OwnerConsentResult(status, uuid4(), False)
    )
    monkeypatch.setattr(consent_service, "check_owner_consent", gate)
    claim = MagicMock(return_value=runner)
    assignment = MagicMock(return_value=SimpleNamespace())
    monkeypatch.setattr(crud_flow_runner, "claim_free_slot", claim)
    monkeypatch.setattr(crud_flow_runner, "create_assignment", assignment)
    monkeypatch.setattr(
        "preloop.services.runner_service.emit_runner_updated", lambda *a: None
    )
    outcome = {}
    result = lease_job(
        MagicMock(),
        account_id=uuid4(),
        pool="local",
        execution_id=uuid4(),
        payload={"prompt": "Review the example change"},
        outcome=outcome,
    )
    assert (result is runner) is leased
    assert assignment.called is leased
    assert claim.called is leased
    assert gate.called is (mode not in {"always", "never"})
    if gate.called:
        # The owner sees what will run; the ids remain the binding.
        assert callable(gate.call_args.kwargs["describe"])
        assert set(gate.call_args.kwargs["details"]) == {"execution_id", "runner_id"}
    if mode == "ask_me" and status != "approved":
        assert outcome["consent_status"] == status


def test_retry_keeps_original_runner_owner(monkeypatch) -> None:
    original, another = uuid4(), uuid4()
    candidates = [
        SimpleNamespace(id=another, status="online", free_slots=2),
        SimpleNamespace(
            id=original,
            status="online",
            free_slots=1,
            flow_execution_consent="ask_me",
            registered_by_user_id=uuid4(),
            capabilities={"docker_available": True},
            accept_untagged=True,
        ),
    ]
    monkeypatch.setattr(
        crud_flow_execution, "admit_runtime_start", lambda *a, **k: True
    )
    monkeypatch.setattr(crud_flow_runner, "find_matching", lambda *a, **k: candidates)
    monkeypatch.setattr(
        owner_consent, "runner_for_execution", lambda *a, **k: str(original)
    )
    gate = MagicMock(
        return_value=consent_service.OwnerConsentResult("pending", uuid4(), False)
    )
    monkeypatch.setattr(consent_service, "check_owner_consent", gate)
    claim = MagicMock()
    monkeypatch.setattr(crud_flow_runner, "claim_free_slot", claim)
    assert (
        lease_job(
            MagicMock(),
            account_id=uuid4(),
            pool="local",
            execution_id=uuid4(),
            payload={},
        )
        is None
    )
    assert gate.call_args.kwargs["subject_id"] == str(original)
    claim.assert_not_called()


@pytest.mark.parametrize("ownerless", [True, False])
def test_unroutable_owner_is_skipped_without_decline(monkeypatch, ownerless) -> None:
    account = uuid4()
    skipped = SimpleNamespace(
        id=uuid4(),
        account_id=account,
        registered_by_user_id=None if ownerless else uuid4(),
        flow_execution_consent="ask_me",
        status="online",
        free_slots=1,
        capabilities={"docker_available": True},
        accept_untagged=True,
    )
    eligible = SimpleNamespace(
        id=uuid4(),
        account_id=account,
        registered_by_user_id=uuid4(),
        flow_execution_consent="always",
        status="online",
        free_slots=1,
        capabilities={"docker_available": True},
        accept_untagged=True,
    )
    monkeypatch.setattr(
        crud_flow_execution, "admit_runtime_start", lambda *a, **k: True
    )
    monkeypatch.setattr(
        crud_flow_runner, "find_matching", lambda *a, **k: [skipped, eligible]
    )
    monkeypatch.setattr(owner_consent, "runner_for_execution", lambda *a, **k: None)
    gate = MagicMock(side_effect=ValueError("Owner is unavailable"))
    monkeypatch.setattr(consent_service, "check_owner_consent", gate)
    claim = MagicMock(return_value=eligible)
    monkeypatch.setattr(crud_flow_runner, "claim_free_slot", claim)
    monkeypatch.setattr(
        crud_flow_runner, "create_assignment", lambda *a, **k: SimpleNamespace()
    )
    monkeypatch.setattr(
        "preloop.services.runner_service.emit_runner_updated", lambda *a: None
    )
    outcome = {}
    assert (
        lease_job(
            MagicMock(),
            account_id=account,
            pool="local",
            execution_id=uuid4(),
            payload={},
            outcome=outcome,
        )
        is eligible
    )
    assert claim.call_count == 1
    assert claim.call_args.kwargs["runner_id"] == eligible.id
    assert outcome["consent_status"] == "approved"
    assert gate.called is not ownerless
    monkeypatch.setattr(crud_flow_runner, "find_matching", lambda *a, **k: [skipped])
    outcome = {}
    assert (
        lease_job(
            MagicMock(),
            account_id=account,
            pool="local",
            execution_id=uuid4(),
            payload={},
            outcome=outcome,
        )
        is None
    )
    assert "consent_status" not in outcome


@pytest.mark.parametrize("status", ["pending", "approved"])
def test_shared_runner_asks_owner_in_resource_account(monkeypatch, status) -> None:
    execution_account, resource_account = uuid4(), uuid4()
    runner = SimpleNamespace(
        id=uuid4(),
        account_id=resource_account,
        registered_by_user_id=uuid4(),
        flow_execution_consent="ask_me",
        status="online",
        free_slots=1,
        capabilities={"docker_available": True},
        accept_untagged=True,
    )
    monkeypatch.setattr(
        crud_flow_execution, "admit_runtime_start", lambda *a, **k: True
    )
    monkeypatch.setattr(crud_flow_runner, "find_matching", lambda *a, **k: [runner])
    monkeypatch.setattr(owner_consent, "runner_for_execution", lambda *a, **k: None)
    gate = MagicMock(
        return_value=consent_service.OwnerConsentResult(status, uuid4(), False)
    )
    monkeypatch.setattr(consent_service, "check_owner_consent", gate)
    monkeypatch.setattr(crud_flow_runner, "claim_free_slot", lambda *a, **k: runner)
    monkeypatch.setattr(
        crud_flow_runner, "create_assignment", lambda *a, **k: SimpleNamespace()
    )
    monkeypatch.setattr(
        "preloop.services.runner_service.emit_runner_updated", lambda *a: None
    )
    result = lease_job(
        MagicMock(),
        account_id=execution_account,
        pool="shared",
        execution_id=uuid4(),
        payload={},
    )
    assert gate.call_args.kwargs["account_id"] == resource_account
    assert (result is runner) is (status == "approved")


def test_ownerless_ask_me_runner_does_not_block_hosted_fallback(monkeypatch) -> None:
    from preloop.services.runner_service import _has_online_private_runner

    runner = SimpleNamespace(
        id=uuid4(),
        registered_by_user_id=None,
        flow_execution_consent="ask_me",
        status="online",
        free_slots=1,
        capabilities={"docker_available": True},
        accept_untagged=True,
    )
    monkeypatch.setattr(crud_flow_runner, "find_matching", lambda *a, **k: [runner])
    assert not _has_online_private_runner(MagicMock(), uuid4())
