"""Deleting a flow that other records reference returns 409, never 500.

CI identities and security maintenance rows hold ON DELETE RESTRICT keys to
the flow. The delete must report them before changing anything and must not
revoke, disable or detach them.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.api.endpoints import flows
from preloop.models import crud, models
from preloop.models.crud.flow import FlowDeleteBlockers
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from tests.api import test_ci_principal as ci_fixtures
from tests.conftest import maybe_await

# Reuse the CI identity fixtures (an account, owner, project and bound flow).
ci_resources = ci_fixtures.ci_resources
reset_ci_authority = ci_fixtures.reset_ci_authority
provision = ci_fixtures.provision


def _delete(db: Session, flow: models.Flow, user: models.User) -> Any:
    return maybe_await(flows.delete_flow(db=db, flow_id=flow.id, current_user=user))


def _disable(db: Session, flow: models.Flow) -> None:
    flow.is_enabled = False
    db.flush()


def _release(
    db: Session, owner: models.User, project: models.Project, flow: models.Flow
) -> models.SecurityMaintenanceRelease:
    workflow = models.ApprovalWorkflow(account_id=owner.account_id, name="sm")
    db.add(workflow)
    db.flush()
    return crud.crud_security_maintenance.create_release(
        db,
        account_id=owner.account_id,
        fields={
            "product_key": "product",
            "release_key": "1.0",
            "display_name": "Product 1.0",
            "project_id": project.id,
            "pinned_build_ref": "ref",
            "sbom_input_ref": "sbom",
            "audit_flow_id": flow.id,
            "implementation_flow_id": flow.id,
            "approval_workflow_id": workflow.id,
        },
    )


@pytest.mark.asyncio
async def test_flow_with_ci_identity_is_refused_and_identity_untouched(
    db_session: Session, ci_resources: Any
) -> None:
    owner, _, flow, _ = ci_resources
    principal, _, _ = provision(db_session, ci_resources)
    _disable(db_session, flow)
    before = (principal.is_active, principal.credential_version, principal.flow_id)

    with pytest.raises(HTTPException) as exc:
        await _delete(db_session, flow, owner)

    assert exc.value.status_code == 409
    assert "Synthetic CI" in exc.value.detail
    assert str(principal.id) in exc.value.detail
    assert "/api/v1/ci-identities/" in exc.value.detail
    assert crud.crud_flow.get(db_session, id=flow.id) is not None
    db_session.expire_all()
    row = db_session.get(models.CiPrincipal, principal.id)
    assert (row.is_active, row.credential_version, row.flow_id) == before


@pytest.mark.asyncio
async def test_unreferenced_disabled_flow_is_deleted(
    db_session: Session, ci_resources: Any
) -> None:
    owner, _, flow, _ = ci_resources
    _disable(db_session, flow)

    response = await _delete(db_session, flow, owner)

    assert response.id == flow.id
    assert crud.crud_flow.get(db_session, id=flow.id) is None


@pytest.mark.asyncio
async def test_flow_used_by_security_maintenance_release_is_refused(
    db_session: Session, ci_resources: Any
) -> None:
    owner, project, flow, _ = ci_resources
    release = _release(db_session, owner, project, flow)
    _disable(db_session, flow)

    with pytest.raises(HTTPException) as exc:
        await _delete(db_session, flow, owner)

    assert exc.value.status_code == 409
    assert "Product 1.0" in exc.value.detail
    assert str(release.id) in exc.value.detail
    assert crud.crud_flow.get(db_session, id=flow.id) is not None


@pytest.mark.asyncio
async def test_flow_whose_execution_is_baseline_evidence_is_refused(
    db_session: Session, ci_resources: Any
) -> None:
    owner, project, flow, _ = ci_resources
    audit_flow = models.Flow(
        name="Audit",
        prompt_template="x",
        agent_type="openhands",
        agent_config={},
        account_id=owner.account_id,
    )
    db_session.add(audit_flow)
    db_session.flush()
    release = _release(db_session, owner, project, audit_flow)
    execution = crud.crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    db_session.add(
        models.SecurityMaintenanceBaseline(
            account_id=owner.account_id,
            release_id=release.id,
            audit_execution_id=execution.id,
            result_digest="0" * 64,
            verdict="clean",
        )
    )
    db_session.flush()
    _disable(db_session, flow)

    with pytest.raises(HTTPException) as exc:
        await _delete(db_session, flow, owner)

    assert exc.value.status_code == 409
    assert "baseline" in exc.value.detail
    assert crud.crud_flow.get(db_session, id=flow.id) is not None


@pytest.mark.asyncio
async def test_integrity_error_on_remove_is_409_with_rollback(
    db_session: Session, ci_resources: Any, mocker: Any
) -> None:
    owner, _, flow, _ = ci_resources
    _disable(db_session, flow)
    db = MagicMock(wraps=db_session)
    mocker.patch.object(
        flows.crud_flow, "get_delete_blockers", return_value=FlowDeleteBlockers()
    )
    mocker.patch.object(
        flows.crud_flow,
        "remove",
        side_effect=IntegrityError("DELETE", {}, Exception("fk")),
    )

    with pytest.raises(HTTPException) as exc:
        await _delete(db, flow, owner)

    assert exc.value.status_code == 409
    db.rollback.assert_called_once()


@pytest.mark.asyncio
async def test_active_execution_conflict_detail_is_clean(
    db_session: Session, ci_resources: Any
) -> None:
    owner, _, flow, _ = ci_resources
    crud.crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )

    with pytest.raises(HTTPException) as exc:
        await _delete(db_session, flow, owner)

    assert exc.value.status_code == 409
    assert exc.value.detail == (
        "Flow has 1 active execution(s). Stop them first (POST "
        '/api/v1/flows/executions/{id}/command with {"command": "stop"}) '
        "and retry the delete."
    )


@pytest.mark.asyncio
async def test_other_account_references_are_counted_but_not_named(
    db_session: Session, ci_resources: Any
) -> None:
    owner, project, flow, _ = ci_resources
    other = crud.crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )
    foreign = models.CiPrincipal(
        name="Foreign identity",
        account_id=other.id,
        project_id=project.id,
        flow_id=flow.id,
        tracker_id=project.organization.tracker_id,
        repository_identifier="example/repository",
        repository_binding={},
        grant={},
    )
    db_session.add(foreign)
    db_session.flush()
    _disable(db_session, flow)

    with pytest.raises(HTTPException) as exc:
        await _delete(db_session, flow, owner)

    assert exc.value.status_code == 409
    assert "Foreign identity" not in exc.value.detail
    assert str(foreign.id) not in exc.value.detail
    assert "1 reference(s) from other accounts" in exc.value.detail
    assert crud.crud_flow.get(db_session, id=flow.id) is not None


@pytest.mark.asyncio
async def test_release_using_flow_only_for_recheck_is_refused(
    db_session: Session, ci_resources: Any
) -> None:
    owner, project, flow, _ = ci_resources
    audit_flow = models.Flow(
        name="Audit",
        prompt_template="x",
        agent_type="openhands",
        agent_config={},
        account_id=owner.account_id,
    )
    db_session.add(audit_flow)
    db_session.flush()
    release = _release(db_session, owner, project, audit_flow)
    release.recheck_flow_id = flow.id
    db_session.flush()
    _disable(db_session, flow)

    with pytest.raises(HTTPException) as exc:
        await _delete(db_session, flow, owner)

    assert exc.value.status_code == 409
    assert str(release.id) in exc.value.detail
