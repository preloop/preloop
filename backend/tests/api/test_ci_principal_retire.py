"""Retiring a CI identity releases its flow so the flow can be deleted.

Retirement is explicit, permanent and keeps history: the identity is disabled,
its keys are revoked, ``flow_id`` is cleared, and the grant still records the
original flow.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.api.endpoints import ci_identities, flows
from preloop.models import crud, models
from preloop.models.crud import ci_result_export
from preloop.plugins.ci_authorization import register_ci_administrator
from preloop.schemas.ci_administration import CiAdminUpdate
from tests.api import test_ci_principal as ci_fixtures
from tests.conftest import maybe_await

ci_resources = ci_fixtures.ci_resources
reset_ci_authority = ci_fixtures.reset_ci_authority
provision = ci_fixtures.provision


def _retire(db: Session, actor: models.User, principal_id: Any) -> Any:
    return ci_identities.retire_identity(principal_id=principal_id, actor=actor, db=db)


def test_retire_disables_revokes_and_releases_flow(
    db_session: Session, ci_resources: Any
) -> None:
    owner, _, flow, _ = ci_resources
    principal, key, token = provision(db_session, ci_resources)

    read = _retire(db_session, owner, principal.id)

    assert read.retired is True
    assert read.is_active is False
    assert all(not k.is_active for k in read.keys)
    db_session.expire_all()
    row = db_session.get(models.CiPrincipal, principal.id)
    assert row.flow_id is None
    assert row.grant["flow_id"] == str(flow.id)
    assert db_session.get(models.ApiKey, key.id).is_active is False
    assert crud.crud_ci_principal.authenticate(db_session, token=token) is None
    assert (
        db_session.query(models.AuditLog)
        .filter(
            models.AuditLog.action == "ci_identity_retire",
            models.AuditLog.resource_id == str(principal.id),
        )
        .count()
        == 1
    )


def test_retired_identity_cannot_be_reenabled_or_reissued(
    db_session: Session, ci_resources: Any
) -> None:
    owner, _, _, grant = ci_resources
    principal, _, _ = provision(db_session, ci_resources)
    _retire(db_session, owner, principal.id)

    for payload in (CiAdminUpdate(enabled=True), CiAdminUpdate(grant=grant)):
        with pytest.raises(HTTPException) as exc:
            ci_identities.update_identity(
                principal_id=principal.id, payload=payload, actor=owner, db=db_session
            )
        assert exc.value.status_code == 400
    with pytest.raises(ValueError):
        crud.crud_ci_principal.change(
            db_session, actor=owner, principal_id=principal.id, enabled=True
        )
    db_session.expire_all()
    row = db_session.get(models.CiPrincipal, principal.id)
    assert (row.is_active, row.flow_id) == (False, None)


def test_retire_is_idempotent(db_session: Session, ci_resources: Any) -> None:
    owner, _, _, _ = ci_resources
    principal, _, _ = provision(db_session, ci_resources)

    _retire(db_session, owner, principal.id)
    again = _retire(db_session, owner, principal.id)

    assert again.retired is True
    assert (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "ci_identity_retire")
        .filter(models.AuditLog.resource_id == str(principal.id))
        .count()
        == 1
    )


def test_retire_requires_disable_authority(
    db_session: Session, ci_resources: Any
) -> None:
    owner, _, _, _ = ci_resources
    principal, _, _ = provision(db_session, ci_resources)
    register_ci_administrator(lambda *_: False)

    with pytest.raises(HTTPException) as exc:
        _retire(db_session, owner, principal.id)

    assert exc.value.status_code == 403
    db_session.expire_all()
    row = db_session.get(models.CiPrincipal, principal.id)
    assert row.is_active is True and row.flow_id is not None


def test_retire_unknown_identity_is_404(db_session: Session, ci_resources: Any) -> None:
    owner, _, _, _ = ci_resources
    with pytest.raises(HTTPException) as exc:
        _retire(db_session, owner, ci_fixtures.uuid4())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_flow_delete_points_to_retire_and_succeeds_after_it(
    db_session: Session, ci_resources: Any
) -> None:
    owner, _, flow, _ = ci_resources
    principal, _, _ = provision(db_session, ci_resources)
    flow.is_enabled = False
    db_session.flush()

    with pytest.raises(HTTPException) as exc:
        await maybe_await(
            flows.delete_flow(db=db_session, flow_id=flow.id, current_user=owner)
        )
    assert exc.value.status_code == 409
    assert "/api/v1/ci-identities/{id}/retire" in exc.value.detail

    _retire(db_session, owner, principal.id)
    await maybe_await(
        flows.delete_flow(db=db_session, flow_id=flow.id, current_user=owner)
    )

    assert crud.crud_flow.get(db_session, id=flow.id) is None
    db_session.expire_all()
    row = db_session.get(models.CiPrincipal, principal.id)
    assert row is not None and row.flow_id is None and row.is_active is False


def test_retired_identity_cannot_create_subscription(
    db_session: Session, ci_resources: Any, mocker: Any
) -> None:
    owner, _, _, _ = ci_resources
    principal, key, _ = provision(db_session, ci_resources)
    _retire(db_session, owner, principal.id)
    mocker.patch.object(
        crud.crud_ci_administration, "require_available", return_value=None
    )
    payload = SimpleNamespace(key_id=key.id)
    before = db_session.query(models.WebhookEndpoint).count()

    with pytest.raises(PermissionError, match="retired"):
        crud.crud_ci_administration.create_subscription(
            db_session, actor=owner, principal_id=principal.id, payload=payload
        )

    assert db_session.query(models.WebhookEndpoint).count() == before
    assert (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "ci_identity_administration_denied")
        .filter(models.AuditLog.resource_id == str(principal.id))
        .count()
        == 1
    )


def test_retired_identity_cannot_consent_to_result_export(
    db_session: Session, ci_resources: Any
) -> None:
    owner, _, _, _ = ci_resources
    principal, _, _ = provision(db_session, ci_resources)
    _retire(db_session, owner, principal.id)

    with pytest.raises(PermissionError, match="retired"):
        ci_result_export.prepare_consent(
            db_session,
            actor=owner,
            execution_id=ci_fixtures.uuid4(),
            payload=SimpleNamespace(principal_id=principal.id),
        )


def test_denied_retire_is_audited_as_retire(
    db_session: Session, ci_resources: Any
) -> None:
    owner, _, _, _ = ci_resources
    principal, _, _ = provision(db_session, ci_resources)
    register_ci_administrator(lambda *_: False)

    with pytest.raises(HTTPException):
        _retire(db_session, owner, principal.id)

    denials = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "ci_identity_administration_denied")
        .filter(models.AuditLog.resource_id == str(principal.id))
        .all()
    )
    assert denials and all(d.details["operation"] == "retire" for d in denials)
