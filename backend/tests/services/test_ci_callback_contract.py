"""Independent persisted-owner and fail-closed callback delivery contracts."""

import json
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from preloop.plugins.ci_authorization import register_ci_machine_authorizer
from preloop.schemas.ci_principal import CiAction
from preloop.services.event_webhooks import outbox
from tests.api.test_ci_execution import review_binding
from tests.api.test_ci_principal import ci_resources as create_ci_resources
from tests.api.test_ci_principal import provision

FINISHED = "flow.execution.finished"


@pytest.fixture
def ci_resources(db_session: Session) -> tuple[Any, ...]:
    return create_ci_resources.__wrapped__(db_session)


@pytest.fixture
def callback(db_session: Session, ci_resources: tuple[Any, ...]) -> tuple[Any, ...]:
    from preloop.schemas.ci_subscription import CiSubscriptionCreate

    principal, key, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    endpoint, secret = crud.crud_ci_subscription.create(
        db_session,
        context=context,
        payload=CiSubscriptionCreate(url="https://example.com/completed"),
    )
    execution = crud.crud_ci_execution.create(
        db_session, context=context, binding=review_binding(context), event={}
    )
    CRUDBase(models.FlowExecution).update(
        db_session,
        db_obj=execution,
        obj_in={
            "status": "SUCCEEDED",
            "result": {"apiToken": "private-result", "review": "private-report"},
            "resolved_input_prompt": "private-prompt",
        },
    )
    return principal, key, context, endpoint, secret, execution


def callback_payload(db: Session, endpoint: Any, execution_id: Any) -> Any:
    return crud.crud_ci_subscription.callback_payload(
        db,
        endpoint=endpoint,
        account_id=endpoint.account_id,
        event_type=FINISHED,
        subject_id=execution_id,
    )


def test_valid_callback_uses_persisted_correlation_without_runtime_material(
    db_session: Session, callback: tuple[Any, ...]
) -> None:
    _, _, context, endpoint, secret, execution = callback
    payload = callback_payload(db_session, endpoint, execution.id)
    assert payload == {
        "execution_id": str(execution.id),
        "flow_id": str(context.flow_id),
        "project_id": str(context.project_id),
        "repository_identifier": context.repository_identifier,
        "pr_number": 7,
        "provider_pr_id": "synthetic-pr-7",
        "head_sha": "a" * 40,
        "status": "SUCCEEDED",
        "result_ready": True,
    }
    serialized = json.dumps(payload, default=str)
    for trusted in (
        execution.id,
        context.flow_id,
        context.project_id,
        "a" * 40,
        "SUCCEEDED",
    ):
        assert str(trusted) in serialized
    for private in (secret, "private-result", "private-report", "private-prompt"):
        assert private not in serialized


@pytest.mark.parametrize("status", ["PENDING", "STARTING", "RUNNING", "INITIALIZING"])
def test_nonterminal_execution_never_creates_callback(
    db_session: Session, callback: tuple[Any, ...], status: str
) -> None:
    *_, endpoint, _, execution = callback
    CRUDBase(models.FlowExecution).update(
        db_session, db_obj=execution, obj_in={"status": status}
    )
    assert callback_payload(db_session, endpoint, execution.id) is None


@pytest.mark.parametrize("ownership", ["other_principal", "human", "missing"])
def test_same_flow_foreign_or_human_execution_denied(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    callback: tuple[Any, ...],
    ownership: str,
) -> None:
    _, _, context, endpoint, _, _ = callback
    if ownership == "missing":
        subject = uuid4()
    elif ownership == "human":
        subject = (
            CRUDBase(models.FlowExecution)
            .create(
                db_session, obj_in={"flow_id": context.flow_id, "status": "SUCCEEDED"}
            )
            .id
        )
    else:
        _, _, token = provision(db_session, ci_resources)
        other = crud.crud_ci_principal.authenticate(db_session, token=token)
        execution = crud.crud_ci_execution.create(
            db_session, context=other, binding=review_binding(other), event={}
        )
        CRUDBase(models.FlowExecution).update(
            db_session, db_obj=execution, obj_in={"status": "SUCCEEDED"}
        )
        subject = execution.id
    assert callback_payload(db_session, endpoint, subject) is None


@pytest.mark.parametrize(
    "revocation", ["disabled", "grant", "ee_denial", "project_moved"]
)
def test_dispatch_stops_on_current_authority_denial(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    callback: tuple[Any, ...],
    revocation: str,
) -> None:
    principal, _, _, endpoint, _, execution = callback
    assert callback_payload(db_session, endpoint, execution.id) is not None
    try:
        if revocation == "disabled":
            crud.crud_ci_principal.change(
                db_session,
                actor=ci_resources[0],
                principal_id=principal.id,
                enabled=False,
            )
        elif revocation == "grant":
            grant = ci_resources[3].model_copy(
                update={"actions": (CiAction.READ_RESULT,)}
            )
            crud.crud_ci_principal.change(
                db_session,
                actor=ci_resources[0],
                principal_id=principal.id,
                grant=grant,
            )
        elif revocation == "ee_denial":
            register_ci_machine_authorizer(lambda context, action: False)
        else:
            crud.crud_project.update(
                db_session,
                db_obj=ci_resources[1],
                obj_in={"identifier": "moved-repository"},
            )
        assert callback_payload(db_session, endpoint, execution.id) is None
    finally:
        register_ci_machine_authorizer(None)


def test_initiating_key_revocation_does_not_cancel_accepted_callback(
    db_session: Session, ci_resources: tuple[Any, ...], callback: tuple[Any, ...]
) -> None:
    principal, key, _, endpoint, _, execution = callback
    crud.crud_ci_principal.revoke_key(
        db_session, actor=ci_resources[0], principal_id=principal.id, key_id=key.id
    )
    assert callback_payload(db_session, endpoint, execution.id) is not None


@pytest.mark.parametrize("marker", ["principal", "key", "binding"])
def test_partial_machine_marker_never_falls_back_to_broad_human_envelope(
    db_session: Session, ci_resources: tuple[Any, ...], marker: str
) -> None:
    principal, key, _ = provision(db_session, ci_resources)
    values: dict[str, Any] = {
        "account_id": principal.account_id,
        "url": "https://example.com/malformed",
        "secret_encrypted": "fixture-ciphertext",
        "event_types": [],
    }
    if marker == "principal":
        values["ci_principal_id"] = principal.id
    elif marker == "key":
        values["initiating_ci_key_id"] = key.id
    else:
        values["ci_subscription_binding"] = {}
    CRUDBase(models.WebhookEndpoint).create(db_session, obj_in=values)
    result = outbox.enqueue_event(
        db_session,
        account_id=principal.account_id,
        event_type=FINISHED,
        data={"private": "forged-private-material"},
        subject_id=uuid4(),
    )
    assert result.delivery_ids == []


def test_outbox_rebuilds_machine_body_but_preserves_human_envelope(
    db_session: Session, callback: tuple[Any, ...]
) -> None:
    _, _, _, endpoint, _, execution = callback
    human = CRUDBase(models.WebhookEndpoint).create(
        db_session,
        obj_in={
            "account_id": endpoint.account_id,
            "url": "https://example.com/human",
            "event_types": [],
            "secret_encrypted": "fixture-ciphertext",
        },
    )
    result = outbox.enqueue_event(
        db_session,
        account_id=endpoint.account_id,
        event_type=FINISHED,
        subject_id=execution.id,
        data={
            "execution_id": str(uuid4()),
            "head_sha": "forged-head",
            "prompt": "caller-private-material",
        },
    )
    assert len(result.delivery_ids) == 2
    rows = [
        CRUDBase(models.WebhookDelivery).get(db_session, id=value)
        for value in result.delivery_ids
    ]
    machine = next(row for row in rows if row.endpoint_id == endpoint.id)
    broad = next(row for row in rows if row.endpoint_id == human.id)
    assert str(execution.id) in json.dumps(machine.payload)
    assert "a" * 40 in json.dumps(machine.payload)
    assert "caller-private-material" not in json.dumps(machine.payload)
    assert "forged-head" not in json.dumps(machine.payload)
    assert broad.payload["data"]["prompt"] == "caller-private-material"
    assert (
        outbox.replay_event(
            db_session, account_id=endpoint.account_id, event_id=result.event_id
        )
        != []
    )
    replay = outbox.replay_event(
        db_session, account_id=endpoint.account_id, event_id=result.event_id
    )
    assert all(
        CRUDBase(models.WebhookDelivery).get(db_session, id=value).endpoint_id
        == human.id
        for value in replay
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("async_path", [False, True])
async def test_raw_delivery_cannot_target_machine_subscription(
    db_session: Session, callback: tuple[Any, ...], async_path: bool
) -> None:
    from preloop.models.db.session import SyncApprovalSession

    *_, endpoint, _, execution = callback
    kwargs = dict(
        endpoint=endpoint,
        event_type=FINISHED,
        payload={"prompt": "forged-private-material"},
        natural_key=str(uuid4()),
        subject_id=execution.id,
    )
    if async_path:
        result = await outbox.enqueue_raw_delivery_async(
            SyncApprovalSession(db_session), **kwargs
        )
    else:
        result = outbox.enqueue_raw_delivery(db_session, **kwargs)
    assert result.delivery_ids == []


@pytest.mark.asyncio
async def test_async_outbox_rebuilds_machine_data_and_checks_revocation(
    db_session: Session, ci_resources: tuple[Any, ...], callback: tuple[Any, ...]
) -> None:
    from preloop.models.db.session import SyncApprovalSession

    principal, _, _, endpoint, _, execution = callback
    adapter = SyncApprovalSession(db_session)
    result = await outbox.enqueue_event_async(
        adapter,
        account_id=endpoint.account_id,
        event_type=FINISHED,
        subject_id=execution.id,
        data={"prompt": "forged-private-material"},
    )
    assert len(result.delivery_ids) == 1
    row = CRUDBase(models.WebhookDelivery).get(db_session, id=result.delivery_ids[0])
    assert str(execution.id) in json.dumps(row.payload)
    assert "forged-private-material" not in json.dumps(row.payload)
    crud.crud_ci_principal.change(
        db_session, actor=ci_resources[0], principal_id=principal.id, enabled=False
    )
    denied = await outbox.enqueue_event_async(
        adapter,
        account_id=endpoint.account_id,
        event_type=FINISHED,
        subject_id=execution.id,
        data={"prompt": "forged-private-material"},
    )
    assert denied.delivery_ids == []


@pytest.mark.parametrize(
    "event_type", ["webhook.test", "approval.created", "flow.execution.started"]
)
def test_machine_subscription_never_receives_other_event_types(
    db_session: Session, callback: tuple[Any, ...], event_type: str
) -> None:
    *_, endpoint, _, execution = callback
    result = outbox.enqueue_event(
        db_session,
        account_id=endpoint.account_id,
        event_type=event_type,
        subject_id=execution.id,
        data={},
    )
    assert result.delivery_ids == []


@pytest.mark.parametrize(
    "mutation", ["missing_field", "extra", "foreign", "wrong_version"]
)
def test_malformed_snapshot_is_denied_without_human_fallback(
    db_session: Session, callback: tuple[Any, ...], mutation: str
) -> None:
    *_, endpoint, _, execution = callback
    malformed = dict(endpoint.ci_subscription_binding)
    if mutation == "missing_field":
        malformed.pop("flow_id")
    elif mutation == "extra":
        malformed["control"] = "forged"
    elif mutation == "foreign":
        malformed["principal_id"] = str(uuid4())
    else:
        malformed["version"] = 99
    # Insert synthetic malformed metadata to exercise the persisted read path.
    # UPDATE immutability is covered separately by the migration owner.
    malformed_endpoint = CRUDBase(models.WebhookEndpoint).create(
        db_session,
        obj_in={
            "account_id": endpoint.account_id,
            "ci_principal_id": endpoint.ci_principal_id,
            "initiating_ci_key_id": endpoint.initiating_ci_key_id,
            "ci_subscription_binding": malformed,
            "event_types": [FINISHED],
            "active": True,
            "source": "account",
            "url": endpoint.url,
            "secret_encrypted": endpoint.secret_encrypted,
        },
    )
    assert callback_payload(db_session, malformed_endpoint, execution.id) is None


def test_deleted_initiating_key_keeps_stable_owner_and_binding_audit(
    db_session: Session, callback: tuple[Any, ...]
) -> None:
    principal, key, _, endpoint, _, execution = callback
    key_id = key.id
    snapshot = dict(endpoint.ci_subscription_binding)
    CRUDBase(models.ApiKey).delete(db_session, id=key_id)
    db_session.refresh(endpoint)
    assert endpoint.initiating_ci_key_id is None
    assert endpoint.ci_principal_id == principal.id
    assert endpoint.ci_subscription_binding == snapshot
    assert str(key_id) in json.dumps(snapshot)
    assert callback_payload(db_session, endpoint, execution.id) is not None


@pytest.mark.parametrize(
    "corruption",
    ["missing_binding", "different_project", "different_head", "missing_owner"],
)
def test_execution_binding_corruption_cannot_supply_trusted_completion(
    db_session: Session, callback: tuple[Any, ...], corruption: str
) -> None:
    _, _, context, endpoint, _, _ = callback
    binding = review_binding(context).model_dump(mode="json")
    values: dict[str, Any] = {
        "flow_id": context.flow_id,
        "status": "SUCCEEDED",
        "ci_principal_id": context.principal_id,
        "initiating_ci_key_id": context.key_id,
        "ci_review_binding": binding,
    }
    if corruption == "missing_binding":
        values["ci_review_binding"] = None
    elif corruption == "different_project":
        binding["project_id"] = str(uuid4())
    elif corruption == "different_head":
        binding["head_sha"] = "invalid"
    else:
        values["ci_principal_id"] = None
    malformed = CRUDBase(models.FlowExecution).create(db_session, obj_in=values)
    assert callback_payload(db_session, endpoint, malformed.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "grant", "secret_and_url"])
async def test_worker_revalidates_after_waiting_for_semaphore(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    callback: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    import asyncio

    import httpx

    from preloop.services.event_webhooks import worker
    from preloop.services.event_webhooks.signing import (
        SIGNATURE_HEADER,
        verify_signature,
    )

    principal, _, context, endpoint, old_secret, execution = callback
    other_execution = crud.crud_ci_execution.create(
        db_session, context=context, binding=review_binding(context), event={}
    )
    CRUDBase(models.FlowExecution).update(
        db_session, db_obj=other_execution, obj_in={"status": "FAILED"}
    )
    for run in (execution, other_execution):
        assert (
            len(
                outbox.enqueue_event(
                    db_session,
                    account_id=endpoint.account_id,
                    event_type=FINISHED,
                    subject_id=run.id,
                    data={"prompt": "forged-private"},
                ).delivery_ids
            )
            == 1
        )
    monkeypatch.setattr(worker.settings, "webhook_delivery_concurrency", 1)
    connection = db_session.connection()
    sessions: list[Session] = []
    closed: list[Session] = []

    class OwnedSession(Session):
        def close(self) -> None:
            closed.append(self)
            super().close()

    def factory() -> Session:
        session = OwnedSession(
            bind=connection, join_transaction_mode="create_savepoint"
        )
        sessions.append(session)
        return session

    entered = asyncio.Event()
    release = asyncio.Event()
    sent: list[tuple[str, bytes, dict[str, str]]] = []

    class SyntheticClient:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(
            self, url: str, *, content: bytes, headers: dict[str, str]
        ) -> Any:
            sent.append((url, content, headers))
            if len(sent) == 1:
                entered.set()
                await release.wait()
            return httpx.Response(204)

    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kwargs: SyntheticClient())
    task = asyncio.create_task(worker.run_once(db_factory=factory))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        new_secret = old_secret
        if change == "disabled":
            crud.crud_ci_principal.change(
                db_session,
                actor=ci_resources[0],
                principal_id=principal.id,
                enabled=False,
            )
        elif change == "grant":
            crud.crud_ci_principal.change(
                db_session,
                actor=ci_resources[0],
                principal_id=principal.id,
                grant=ci_resources[3].model_copy(
                    update={"actions": (CiAction.READ_RESULT,)}
                ),
            )
        else:
            from preloop.schemas.ci_subscription import CiSubscriptionUpdate

            crud.crud_ci_subscription.update_owned(
                db_session,
                context=context,
                endpoint_id=endpoint.id,
                payload=CiSubscriptionUpdate(url="https://example.com/new-target"),
            )
            _, new_secret = crud.crud_ci_subscription.rotate_secret(
                db_session, context=context, endpoint_id=endpoint.id
            )
        release.set()
        attempted = await asyncio.wait_for(task, timeout=10)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert len(sessions) == 3
    assert len({id(session) for session in sessions}) == 3
    assert set(map(id, sessions)) == set(map(id, closed))
    if change == "secret_and_url":
        assert attempted == 2
        assert sent[1][0] == "https://example.com/new-target"
        assert verify_signature(new_secret, sent[1][2][SIGNATURE_HEADER], sent[1][1])
        assert not verify_signature(
            old_secret, sent[1][2][SIGNATURE_HEADER], sent[1][1]
        )
        assert "forged-private" not in sent[1][1].decode()
    else:
        assert attempted == 1
        assert len(sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["disabled", "grant", "ee_denial", "rotate_key", "rotate_secret"]
)
async def test_worker_retry_rechecks_authority_and_current_secret(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    callback: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    from datetime import datetime, timedelta, timezone

    import httpx

    from preloop.services.event_webhooks import worker
    from preloop.services.event_webhooks.signing import (
        SIGNATURE_HEADER,
        verify_signature,
    )

    principal, key, context, endpoint, old_secret, execution = callback
    result = outbox.enqueue_event(
        db_session,
        account_id=endpoint.account_id,
        event_type=FINISHED,
        subject_id=execution.id,
        data={},
    )
    delivery_id = result.delivery_ids[0]
    connection = db_session.connection()

    def factory() -> Session:
        return Session(bind=connection, join_transaction_mode="create_savepoint")

    sent: list[tuple[bytes, dict[str, str]]] = []

    class SyntheticClient:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(
            self, url: str, *, content: bytes, headers: dict[str, str]
        ) -> Any:
            sent.append((content, headers))
            return httpx.Response(503 if len(sent) == 1 else 204)

    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kwargs: SyntheticClient())
    assert await worker.run_once(db_factory=factory) == 1
    delivery = CRUDBase(models.WebhookDelivery).get(db_session, id=delivery_id)
    db_session.refresh(delivery)
    assert delivery.status == "pending"
    assert delivery.attempt_count == 1
    assert verify_signature(old_secret, sent[0][1][SIGNATURE_HEADER], sent[0][0])
    CRUDBase(models.WebhookDelivery).update(
        db_session,
        db_obj=delivery,
        obj_in={
            "next_attempt_at": datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(seconds=1)
        },
    )
    new_secret = old_secret
    try:
        if change == "disabled":
            crud.crud_ci_principal.change(
                db_session,
                actor=ci_resources[0],
                principal_id=principal.id,
                enabled=False,
            )
        elif change == "grant":
            crud.crud_ci_principal.change(
                db_session,
                actor=ci_resources[0],
                principal_id=principal.id,
                grant=ci_resources[3].model_copy(
                    update={"actions": (CiAction.READ_RESULT,)}
                ),
            )
        elif change == "ee_denial":
            register_ci_machine_authorizer(lambda context, action: False)
        elif change == "rotate_key":
            crud.crud_ci_principal.rotate(
                db_session,
                actor=ci_resources[0],
                principal_id=principal.id,
                key_id=key.id,
            )
        else:
            _, new_secret = crud.crud_ci_subscription.rotate_secret(
                db_session, context=context, endpoint_id=endpoint.id
            )
        attempted = await worker.run_once(db_factory=factory)
        db_session.refresh(delivery)
        if change in {"rotate_key", "rotate_secret"}:
            assert attempted == 1
            assert len(sent) == 2
            assert delivery.status == "delivered"
            assert delivery.attempt_count == 2
            assert verify_signature(
                new_secret, sent[1][1][SIGNATURE_HEADER], sent[1][0]
            )
            if change == "rotate_secret":
                assert not verify_signature(
                    old_secret, sent[1][1][SIGNATURE_HEADER], sent[1][0]
                )
            assert json.loads(sent[1][0])["data"]["execution_id"] == str(execution.id)
        else:
            assert attempted == 0
            assert len(sent) == 1
            assert delivery.status == "dead"
            assert delivery.claimed_at is None
            assert delivery.attempt_count == 1
    finally:
        register_ci_machine_authorizer(None)


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "CANCELLED", "STOPPED"])
def test_every_owned_terminal_status_queues_trusted_completion(
    db_session: Session, callback: tuple[Any, ...], status: str
) -> None:
    *_, endpoint, _, execution = callback
    CRUDBase(models.FlowExecution).update(
        db_session, db_obj=execution, obj_in={"status": status}
    )
    payload = callback_payload(db_session, endpoint, execution.id)
    assert payload is not None
    assert payload["status"] == status
    assert payload["execution_id"] == str(execution.id)
    result = outbox.enqueue_event(
        db_session,
        account_id=endpoint.account_id,
        event_type=FINISHED,
        subject_id=execution.id,
        data={"status": "forged"},
    )
    assert len(result.delivery_ids) == 1
    delivery = CRUDBase(models.WebhookDelivery).get(
        db_session, id=result.delivery_ids[0]
    )
    assert delivery.payload["data"]["status"] == status


@pytest.mark.parametrize("status", ["dead", "delivered"])
def test_pre_post_revalidation_preserves_already_terminal_delivery(
    db_session: Session, callback: tuple[Any, ...], status: str
) -> None:
    *_, endpoint, _, execution = callback
    result = outbox.enqueue_event(
        db_session,
        account_id=endpoint.account_id,
        event_type=FINISHED,
        subject_id=execution.id,
        data={},
    )
    delivery = CRUDBase(models.WebhookDelivery).get(
        db_session, id=result.delivery_ids[0]
    )
    CRUDBase(models.WebhookDelivery).update(
        db_session,
        db_obj=delivery,
        obj_in={"status": status, "attempt_count": 2, "last_error": "existing outcome"},
    )
    original_payload = dict(delivery.payload)
    assert (
        crud.crud_ci_subscription.prepare_delivery(db_session, delivery_id=delivery.id)
        is None
    )
    db_session.refresh(delivery)
    assert delivery.status == status
    assert delivery.attempt_count == 2
    assert delivery.last_error == "existing outcome"
    assert delivery.payload == original_payload
