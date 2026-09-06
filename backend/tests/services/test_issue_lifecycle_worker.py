"""Worker sessions commit durably for engine-bound callers and join pytest connections."""

from uuid import uuid4

from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from preloop.models import models
from preloop.models.crud import crud_account
from preloop.services.issue_lifecycle_worker import (
    lifecycle_worker_db,
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
