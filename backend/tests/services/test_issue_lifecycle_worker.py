"""Worker sessions commit durably for engine-bound callers and join pytest connections."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from preloop.models import models
from preloop.models.crud import crud_account
from preloop.services.issue_lifecycle_worker import (
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


@pytest.mark.asyncio
async def test_hook_commits_engine_caller_before_lifecycle_worker(
    db_engine: Engine, monkeypatch
) -> None:
    monkeypatch.setattr(
        "preloop.models.db.session.get_session_factory",
        lambda: sessionmaker(bind=db_engine, autocommit=False, autoflush=False),
    )
    caller = Session(bind=db_engine)
    account = _uncommitted_account(caller)
    account_id = account.id

    @lifecycle_worker_hook
    async def peek(service: object, flow: object, event: dict, _nats: object) -> bool:
        with lifecycle_worker_db(service, getattr(service, "db", None)) as db:
            return db.get(models.Account, account_id) is not None

    seen = await peek(
        SimpleNamespace(db=caller),
        SimpleNamespace(
            agent_config={"lifecycle_kind": "merge_audit"}, account_id=uuid4()
        ),
        {"type": "issue_closed"},
        None,
    )
    assert seen is True
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


@pytest.mark.asyncio
async def test_hook_does_not_commit_unrelated_engine_caller(
    db_engine: Engine, monkeypatch
) -> None:
    monkeypatch.setattr(
        "preloop.models.db.session.get_session_factory",
        lambda: sessionmaker(bind=db_engine, autocommit=False, autoflush=False),
    )
    caller = Session(bind=db_engine)
    account = _uncommitted_account(caller)
    account_id = account.id

    @lifecycle_worker_hook
    async def peek(service: object, flow: object, event: dict, _nats: object) -> bool:
        with lifecycle_worker_db(service, getattr(service, "db", None)) as db:
            return db.get(models.Account, account_id) is not None

    seen = await peek(
        SimpleNamespace(db=caller),
        SimpleNamespace(agent_config={}, account_id=uuid4()),
        {"type": "push"},
        None,
    )
    assert seen is False
    caller.close()
    probe = Session(bind=db_engine)
    try:
        assert probe.get(models.Account, account_id) is None
    finally:
        probe.close()
