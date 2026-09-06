"""Worker sessions commit durably for engine-bound callers and join pytest connections."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_organization,
    crud_project,
    crud_tracker,
)
from preloop.models.crud.base import CRUDBase
from preloop.services.issue_lifecycle_runtime import _lifecycle_flow_entry
from preloop.services.issue_lifecycle_worker import (
    _should_commit_lifecycle_caller,
    lifecycle_entry_decision,
    lifecycle_worker_db,
    lifecycle_worker_hook,
    worker_owned_session,
)


def test_connection_bound_worker_session_joins_caller(db_session: Session) -> None:
    db, owned = worker_owned_session(fallback=db_session)
    try:
        assert owned
        assert db.get_bind() is db_session.get_bind()
        assert isinstance(db.get_bind(), Connection)
    finally:
        db.close()


def test_engine_bound_worker_commit_survives_caller_close(
    db_engine: Engine, monkeypatch
) -> None:
    monkeypatch.setattr(
        "preloop.models.db.session.get_session_factory",
        lambda: sessionmaker(bind=db_engine, autocommit=False, autoflush=False),
    )
    caller = Session(bind=db_engine)
    account_id = None
    with lifecycle_worker_db(fallback=caller) as db:
        assert isinstance(db.get_bind(), Engine)
        account = crud_account.create(
            db,
            obj_in={
                "organization_name": f"lifecycle-durability-{uuid4()}",
                "is_active": True,
            },
        )
        account_id = account.id
    caller.close()
    probe = Session(bind=db_engine)
    try:
        assert probe.get(models.Account, account_id) is not None
    finally:
        row = probe.get(models.Account, account_id)
        if row is not None:
            probe.delete(row)
            probe.commit()
        probe.close()


def _uncommitted_account(caller: Session) -> models.Account:
    account = models.Account(
        organization_name=f"lifecycle-hook-{uuid4()}", is_active=True
    )
    caller.add(account)
    caller.flush()
    return account


def _flow(session: Session, account_id: Any, agent_config: dict[str, Any]) -> Any:
    """Create an enabled flow the trigger service could dispatch."""
    return CRUDBase(models.Flow).create(
        session,
        obj_in={
            "name": f"lifecycle-flow-{uuid4()}",
            "account_id": account_id,
            "agent_type": "codex",
            "agent_config": agent_config,
            "prompt_template": "Implement",
            "is_enabled": True,
            "allowed_mcp_servers": [],
            "allowed_mcp_tools": [],
        },
    )


def _lifecycle_tenant(session: Session) -> SimpleNamespace:
    """Create a tenant whose project selects one implementation flow for pickup.

    This is durable configuration, like production: only the webhook's own
    in-flight rows are left uncommitted for the commit gate to decide on.
    """
    suffix = uuid4()
    account = crud_account.create(
        session,
        obj_in={"organization_name": f"lifecycle-tenant-{suffix}", "is_active": True},
    )
    tracker = crud_tracker.create(
        session,
        obj_in={
            "name": f"tracker-{suffix}",
            "tracker_type": "github",
            "account_id": account.id,
            "api_key": "fake-local-only",
        },
    )
    organization = crud_organization.create(
        session,
        obj_in={
            "name": f"org-{suffix}",
            "identifier": f"org-{suffix}",
            "tracker_id": tracker.id,
        },
    )
    implementer = _flow(session, account.id, {})
    project = crud_project.create(
        session,
        obj_in={
            "name": f"project-{suffix}",
            "identifier": f"example/{suffix}",
            "organization_id": organization.id,
            "settings": {
                "issue_lifecycle": {
                    "ready_enabled": True,
                    "ready_label": "agent-ready",
                    "implementation_flow_id": str(implementer.id),
                }
            },
        },
    )
    return SimpleNamespace(
        account=account,
        tracker=tracker,
        organization=organization,
        project=project,
        flow=implementer,
    )


def _tenant_rows(tenant: SimpleNamespace) -> list[tuple[type, Any]]:
    """Deletion order for a tenant graph, child before parent."""
    return [
        (models.Project, tenant.project.id),
        (models.Flow, tenant.flow.id),
        (models.Organization, tenant.organization.id),
        (models.Tracker, tenant.tracker.id),
        (models.Account, tenant.account.id),
    ]


def _drop(engine: Engine, rows: list[tuple[type, Any]]) -> None:
    """Remove durable rows a test committed."""
    probe = Session(bind=engine)
    try:
        for model, ident in rows:
            row = probe.get(model, ident)
            if row is not None:
                probe.delete(row)
                probe.commit()
    finally:
        probe.close()


@pytest.mark.asyncio
async def test_hook_commits_engine_caller_before_lifecycle_worker(
    db_engine: Engine, monkeypatch
) -> None:
    monkeypatch.setattr(
        "preloop.models.db.session.get_session_factory",
        lambda: sessionmaker(bind=db_engine, autocommit=False, autoflush=False),
    )
    caller = Session(bind=db_engine)
    tenant = _lifecycle_tenant(caller)
    account = _uncommitted_account(caller)
    account_id = account.id
    rows = [(models.Account, account_id), *_tenant_rows(tenant)]

    @lifecycle_worker_hook
    async def peek(service: object, flow: object, event: dict, _nats: object) -> bool:
        with lifecycle_worker_db(service, getattr(service, "db", None)) as db:
            return db.get(models.Account, account_id) is not None

    try:
        seen = await peek(
            SimpleNamespace(db=caller),
            tenant.flow,
            {"type": "issue_labeled", "project_id": str(tenant.project.id)},
            None,
        )
        assert seen is True
        caller.close()
        probe = Session(bind=db_engine)
        try:
            assert probe.get(models.Account, account_id) is not None
        finally:
            probe.close()
    finally:
        caller.close()
        _drop(db_engine, rows)


@pytest.mark.asyncio
async def test_hook_does_not_commit_unrelated_engine_caller(
    db_engine: Engine, monkeypatch
) -> None:
    monkeypatch.setattr(
        "preloop.models.db.session.get_session_factory",
        lambda: sessionmaker(bind=db_engine, autocommit=False, autoflush=False),
    )
    caller = Session(bind=db_engine)
    tenant = _lifecycle_tenant(caller)
    account = _uncommitted_account(caller)
    account_id = account.id
    rows = _tenant_rows(tenant)
    event = {"type": "push", "project_id": str(tenant.project.id)}

    @lifecycle_worker_hook
    async def peek(service: object, flow: object, event: dict, _nats: object) -> bool:
        with lifecycle_worker_db(service, getattr(service, "db", None)) as db:
            return db.get(models.Account, account_id) is not None

    try:
        # The selected implementation flow, but a push: the entry hook declines
        # it, so the caller's unrelated in-flight rows must not become durable.
        seen = await peek(SimpleNamespace(db=caller), tenant.flow, event, None)
        assert seen is False
        caller.close()
        probe = Session(bind=db_engine)
        try:
            assert probe.get(models.Account, account_id) is None
        finally:
            probe.close()
    finally:
        caller.close()
        _drop(db_engine, rows)


@pytest.mark.parametrize(
    "lifecycle_kind, selected, event_type, engaged",
    [
        (None, True, "push", False),
        (None, True, "issue_labeled", True),
        (None, False, "issue_labeled", False),
        ("merge_audit", False, "issue_closed", True),
        ("refinement", False, "issue_closed", True),
        ("deployment_audit", False, "issue_closed", False),
    ],
)
@pytest.mark.asyncio
async def test_commit_gate_never_disagrees_with_flow_entry(
    db_session: Session,
    lifecycle_kind: str | None,
    selected: bool,
    event_type: str,
    engaged: bool,
) -> None:
    """One decision drives both the entry hook and the caller-commit gate."""
    tenant = _lifecycle_tenant(db_session)
    flow = (
        tenant.flow
        if selected
        else _flow(
            db_session,
            tenant.account.id,
            {"lifecycle_kind": lifecycle_kind} if lifecycle_kind else {},
        )
    )
    event = {"type": event_type, "project_id": str(tenant.project.id), "payload": {}}

    assert lifecycle_entry_decision(db_session, flow, event).engaged is engaged
    assert _should_commit_lifecycle_caller(db_session, flow, event) is engaged
    if engaged:
        # Past the gate the entry hook needs a synced issue, which proves it
        # did not short-circuit on the same conditions the gate just read.
        with pytest.raises(ValueError, match="lifecycle_issue_not_synced"):
            await _lifecycle_flow_entry(db_session, flow, event, None, None)
    else:
        assert await _lifecycle_flow_entry(db_session, flow, event, None, None) == (
            False,
            None,
        )
