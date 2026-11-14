"""Credential rejection episodes, serialized notices and execution admission."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from preloop.models import crud, models
from preloop.models.models.secret_reference import UPSTREAM_AUTH_REJECTION_KEY
from preloop.services.gateway_execution import GatewayModelSnapshot
from preloop.services.model_credential_alerts import build_credential_rejection_message
from preloop.services.model_credential_health import record_gateway_credential_outcome
from preloop.services.model_routing import (
    ModelCredentialsRejectedError,
    prepare_execution_routing,
    validate_execution_model_credentials,
)
from tests.services.test_model_credential_health import STORED_KEY, _stored_model


def verdict(db: Session, model: Any, status: int = 401) -> None:
    """Record a synthetic provider verdict without calling an upstream."""
    record_gateway_credential_outcome(
        db,
        ai_model=model,
        status_code=status,
        error_class={401: "upstream_auth", 402: "upstream_quota_exhausted"}.get(status),
        error_detail="No credits available." if status == 402 else "User not found.",
        credential_type="api_key",
    )


@pytest.mark.parametrize("status", [401, 402])
def test_one_notice_and_event_per_episode(
    db_session: Session, test_user: models.User, status: int
) -> None:
    """Repeats are quiet; replacing and rejecting the new key starts an episode."""
    model = _stored_model(db_session, test_user.account_id)
    with patch(
        "preloop.services.model_credential_alerts.enqueue_credential_rejection_notice"
    ) as notice:
        verdict(db_session, GatewayModelSnapshot.from_model(model), status)
        for _ in range(57):
            verdict(db_session, GatewayModelSnapshot.from_model(model), status)
        assert notice.call_count == 1
        data = notice.call_args.kwargs["data"]
        assert data["code"] == (
            "upstream_auth_rejected" if status == 401 else "upstream_billing_rejected"
        )
        message = build_credential_rejection_message(data)
        assert "Preloop login" in message["text"]
        assert "openrouter" in message["text"]
        assert model.name in message["text"]
        assert STORED_KEY not in str(message)
        assert data["first_rejected_at"] in message["text"]
        assert data["remedy"] in message["text"]
        entries = crud.crud_audit_log.get_by_account(
            db_session,
            account_id=test_user.account_id,
            action="ai_model.credential_rejected",
        )
        assert len(entries) == 1
        assert STORED_KEY not in str(entries[0].details)
        crud.crud_ai_model.update(
            db_session, db_obj=model, obj_in={"api_key": "sk-synthetic-replacement"}
        )
        db_session.refresh(model)
        assert model.credentials_rejected is False
        verdict(db_session, GatewayModelSnapshot.from_model(model), status)
        assert notice.call_count == 2


def test_billing_mark_clears_on_acceptance(
    db_session: Session, test_user: models.User
) -> None:
    """A provider-billing 402 marks credentials and the next acceptance clears it."""
    model = _stored_model(db_session, test_user.account_id)
    with patch(
        "preloop.services.model_credential_alerts.enqueue_credential_rejection_notice"
    ):
        verdict(db_session, GatewayModelSnapshot.from_model(model), 402)
    db_session.refresh(model)
    assert model.credentials_status == "error"
    assert model.credentials_last_error_code == "upstream_billing_rejected"
    assert "No credits available." in model.credentials_last_error
    verdict(db_session, GatewayModelSnapshot.from_model(model), 200)
    db_session.refresh(model)
    assert model.credentials_status == "active"


@pytest.mark.parametrize("status", [401, 402])
@pytest.mark.parametrize("excluded", ["hosted", "system", "oauth", "403"])
def test_excluded_credentials_never_touch_db(status: int, excluded: str) -> None:
    """Hosted, system, OAuth and permission denials cannot become key incidents."""
    db = MagicMock()
    model = SimpleNamespace(
        id=uuid4(), account_id=None if excluded == "system" else uuid4()
    )
    with patch(
        "preloop.services.model_credential_health.crud_secret_reference"
    ) as secret:
        record_gateway_credential_outcome(
            db,
            ai_model=model,
            status_code=403 if excluded == "403" else status,
            error_class="upstream_auth"
            if status == 401
            else "upstream_quota_exhausted",
            error_detail="Rejected",
            credential_type="oauth" if excluded == "oauth" else "api_key",
            hosted=excluded == "hosted",
        )
        assert not secret.mock_calls
        assert not db.mock_calls


def test_healthy_key_has_no_db_work() -> None:
    """The healthy gateway path adds neither queries nor writes."""
    db = MagicMock()
    with patch(
        "preloop.services.model_credential_health.crud_secret_reference"
    ) as secret:
        verdict(
            db,
            SimpleNamespace(id=uuid4(), account_id=uuid4(), credentials_rejected=False),
            200,
        )
        assert not db.mock_calls
        assert not secret.mock_calls


def test_two_concurrent_rejections_claim_one_notice(db_engine: Any) -> None:
    """Independent sessions serialize the first verdict on the secret row lock."""
    factory = sessionmaker(bind=db_engine)
    account_id = uuid4()
    with factory() as db:
        crud.crud_account.create(
            db,
            obj_in={
                "id": account_id,
                "organization_name": "Synthetic",
                "is_active": True,
            },
        )
        model = _stored_model(db, account_id)
        db.commit()
        snapshot = GatewayModelSnapshot.from_model(model)
    barrier = Barrier(2)

    def reject() -> None:
        """Represent one gateway replica with its own connection."""
        with factory() as db:
            barrier.wait(timeout=10)
            verdict(db, snapshot)

    try:
        with patch(
            "preloop.services.model_credential_alerts.enqueue_credential_rejection_notice"
        ) as notice:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(reject) for _ in range(2)]
                for future in futures:
                    future.result(timeout=20)
            assert notice.call_count == 1
        with factory() as db:
            entries = crud.crud_audit_log.get_by_account(
                db, account_id=account_id, action="ai_model.credential_rejected"
            )
            assert len(entries) == 1
    finally:
        with factory() as db:
            crud.crud_account.purge(db, account_id=str(account_id))


@pytest.mark.parametrize(
    "result,rejected",
    [
        ("accepted", False),
        ("auth", True),
        ("timeout", False),
    ],
)
def test_admission_probes_marked_auth_keys(
    db_session: Session, test_user: models.User, result: str, rejected: bool
) -> None:
    """Admission depends on a fresh verdict, never the age of the old mark."""
    from preloop.services.model_credential_probe import CredentialProbeResult

    model = _stored_model(db_session, test_user.account_id)
    crud.crud_secret_reference.record_upstream_auth_rejection(
        db_session,
        ai_model_id=model.id,
        provider="openrouter",
        status_code=401,
        message="Rejected",
        now=datetime.now(timezone.utc) - timedelta(days=30),
        commit=False,
    )
    flow = SimpleNamespace(ai_model_id=model.id, agent_type="codex", agent_config={})
    with patch(
        "preloop.services.model_credential_probe.probe_rejected_model_credentials",
        return_value=CredentialProbeResult(rejected, "User not found."),
    ) as probe:
        if rejected:
            with pytest.raises(
                ModelCredentialsRejectedError, match="User not found"
            ) as exc:
                validate_execution_model_credentials(db_session, flow, {})
            assert exc.value.code == "model_credentials_rejected"
            detail = exc.value.to_detail()
            assert detail["provider"] == model.provider_name
            assert detail["provider_message"] == "User not found."
            assert (
                detail["first_rejected_at"]
                == model.credentials_secret.upstream_auth_rejection["first_rejected_at"]
            )
            assert detail["upstream_status"] == 401
        else:
            validate_execution_model_credentials(db_session, flow, {})
        probe.assert_called_once()


@pytest.mark.parametrize("status", [None, 402])
def test_healthy_and_billing_keys_are_never_probed(
    db_session: Session, test_user: models.User, status: int | None
) -> None:
    """Healthy runs and credit top-ups have no admission probe."""
    model = _stored_model(db_session, test_user.account_id)
    if status is not None:
        crud.crud_secret_reference.record_upstream_auth_rejection(
            db_session,
            ai_model_id=model.id,
            provider="openrouter",
            status_code=status,
            message="No credits",
            now=datetime.now(timezone.utc),
            commit=False,
        )
    flow = SimpleNamespace(ai_model_id=model.id, agent_type="codex", agent_config={})
    with patch(
        "preloop.services.model_credential_probe.probe_rejected_model_credentials"
    ) as probe:
        validate_execution_model_credentials(db_session, flow, {})
        probe.assert_not_called()


def test_routing_refuses_before_principal_validation(
    db_session: Session, test_user: models.User
) -> None:
    """All routing consumers, including matrix and CI, share the admission check."""
    from preloop.services.model_credential_probe import CredentialProbeResult

    model = _stored_model(db_session, test_user.account_id)
    model.credentials_secret.meta_data = {
        UPSTREAM_AUTH_REJECTION_KEY: {
            "last_rejected_at": datetime.now(timezone.utc).isoformat(),
            "status_code": 401,
        }
    }
    db_session.flush()
    flow = SimpleNamespace(ai_model_id=model.id, agent_type="codex", agent_config={})
    with (
        patch(
            "preloop.services.model_routing._prepare_execution_routing", return_value={}
        ),
        patch(
            "preloop.services.model_routing.validate_execution_model_principal"
        ) as principal,
        patch(
            "preloop.services.model_credential_probe.probe_rejected_model_credentials",
            return_value=CredentialProbeResult(True, "User not found."),
        ),
    ):
        with pytest.raises(ModelCredentialsRejectedError):
            prepare_execution_routing(db_session, flow, {})
        principal.assert_not_called()


@pytest.mark.parametrize("metadata", [[], ["legacy"], "legacy"])
@pytest.mark.parametrize("status", [401, 402])
def test_rejection_replaces_non_dictionary_secret_metadata(
    db_session: Session,
    test_user: models.User,
    metadata: Any,
    status: int,
) -> None:
    """Legacy/plugin JSON values cannot prevent the provider verdict being stored."""
    model = _stored_model(db_session, test_user.account_id)
    model.credentials_secret.meta_data = metadata
    db_session.flush()
    outcome = crud.crud_secret_reference.record_upstream_auth_rejection(
        db_session,
        ai_model_id=model.id,
        provider="openrouter",
        status_code=status,
        message="Rejected",
        now=datetime.now(timezone.utc),
        commit=False,
    )
    assert outcome.first_rejection is True
    assert model.credentials_upstream_rejection["status_code"] == status


@pytest.mark.asyncio
@pytest.mark.parametrize("matrix", [False, True])
async def test_async_triggers_probe_on_a_worker_thread(
    db_session: Session,
    test_user: models.User,
    matrix: bool,
) -> None:
    """Marked-key I/O must never run on the ASGI loop, including matrix cells."""
    import threading
    from preloop.services.flow_trigger_service import FlowTriggerService
    from preloop.services.model_credential_probe import CredentialProbeResult
    from tests.services.test_model_routing import _usable_model, _flow, _patch_dispatch

    model = _usable_model(db_session, test_user.account_id, name="Review model")
    flow = _flow(db_session, test_user, ai_model_id=model.id)
    model.credentials_secret.meta_data = {
        UPSTREAM_AUTH_REJECTION_KEY: {
            "last_rejected_at": datetime.now(timezone.utc).isoformat(),
            "status_code": 401,
        }
    }
    db_session.flush()
    loop_thread = threading.get_ident()

    def check_thread(*args: Any, **kwargs: Any) -> CredentialProbeResult:
        """Represent a blocking provider check with an inconclusive verdict."""
        assert threading.get_ident() != loop_thread
        return CredentialProbeResult()

    nats, dispatch = _patch_dispatch()
    with (
        nats,
        dispatch,
        patch(
            "preloop.services.model_credential_probe.probe_rejected_model_credentials",
            side_effect=check_thread,
        ) as probe,
    ):
        service = FlowTriggerService(db_session)
        if matrix:
            await service.trigger_flow_matrix(flow_id=flow.id, matrix=[{}, {}])
        else:
            await service.trigger_flow(flow_id=flow.id)
        assert probe.call_count == (2 if matrix else 1)


@pytest.mark.asyncio
async def test_no_progress_retry_probes_only_the_final_selection(
    db_session: Session,
    test_user: models.User,
) -> None:
    """An effort-only escalation cannot probe the same rejected key twice."""
    from preloop.services.flow_trigger_service import FlowTriggerService
    from preloop.services.model_credential_probe import CredentialProbeResult
    from tests.services.test_model_routing import _usable_model, _flow, _patch_dispatch

    model = _usable_model(db_session, test_user.account_id, name="Review model")
    flow = _flow(db_session, test_user, ai_model_id=model.id)
    nats, dispatch = _patch_dispatch()
    with nats, dispatch:
        source = await FlowTriggerService(db_session).trigger_flow(flow_id=flow.id)
    model.credentials_secret.meta_data = {
        UPSTREAM_AUTH_REJECTION_KEY: {
            "last_rejected_at": datetime.now(timezone.utc).isoformat(),
            "status_code": 401,
        }
    }
    db_session.flush()
    nats, dispatch = _patch_dispatch()
    with (
        nats,
        dispatch,
        patch(
            "preloop.services.model_credential_probe.probe_rejected_model_credentials",
            return_value=CredentialProbeResult(),
        ) as probe,
    ):
        await FlowTriggerService(db_session).trigger_flow(
            flow_id=flow.id,
            retry_of_execution_id=source["id"],
            no_progress_escalation={"reasoning_effort": "high"},
        )
        probe.assert_called_once()


@pytest.mark.asyncio
async def test_existing_execution_recovery_never_probes_a_rejected_key(
    db_session: Session, test_user: models.User
) -> None:
    """Reattaching to a running container is not admission for a new execution."""
    import asyncio
    from unittest.mock import AsyncMock

    from preloop.agents.base import AgentStatus
    from preloop.services import execution_recovery

    model = _stored_model(db_session, test_user.account_id)
    model.credentials_secret.meta_data = {
        UPSTREAM_AUTH_REJECTION_KEY: {
            "last_rejected_at": datetime.now(timezone.utc).isoformat(),
            "status_code": 401,
        }
    }
    db_session.flush()
    flow = SimpleNamespace(
        id=uuid4(),
        ai_model_id=model.id,
        agent_type="codex",
        agent_config={},
        account_id=test_user.account_id,
    )
    execution = SimpleNamespace(
        id=uuid4(),
        flow_id=flow.id,
        flow=flow,
        status="RUNNING",
        agent_session_reference="synthetic-existing-session",
        trigger_event_details={},
    )
    executor = MagicMock()
    executor.get_status = AsyncMock(return_value=AgentStatus.RUNNING)
    executor.aclose = AsyncMock()
    service = execution_recovery.ExecutionRecoveryService()

    def running_executions(db: Session, **kwargs: Any) -> list[Any]:
        """Expose only the already-running execution to recovery."""
        return [execution] if kwargs["status"] == "RUNNING" else []

    # Observe provider work even if recovery imported the public probe by alias.
    with (
        patch(
            "preloop.services.flow_execution_dispatcher.flow_execution_worker_enabled",
            return_value=False,
        ),
        patch.object(
            execution_recovery.crud_flow_execution,
            "get_multi",
            side_effect=running_executions,
        ),
        patch.object(execution_recovery, "get_nats_client", new=AsyncMock()),
        patch("preloop.agents.create_executor_for_execution", return_value=executor),
        patch.object(execution_recovery, "FlowExecutionOrchestrator"),
        patch.object(service, "_resume_monitoring_task", new=AsyncMock()),
        patch(
            "preloop.services.model_credential_probe._probe_rejected_model_credentials",
            side_effect=AssertionError("Recovery must not probe credentials"),
        ) as probe,
    ):
        assert await service.recover_orphaned_executions(db_session) == 1
        await asyncio.gather(*service.recovery_tasks)
        probe.assert_not_called()
    assert execution.status == "RUNNING"
    assert model.credentials_rejected is True
