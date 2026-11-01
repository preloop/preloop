"""Concurrent revocations preserve immutable consent and fresh owner checks."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timedelta, timezone
from threading import Event, local
from typing import Any

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.models.crud import ci_result_export
from preloop.models.crud.base import CRUDBase
from tests.api.test_ci_principal import ci_resources as make_resources, provision


def test_concurrent_and_cached_revocations_preserve_first_timestamp(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second DELETE blocks at the consent row, then reads the first result."""
    with Session(db_engine) as db:
        resources = make_resources.__wrapped__(db)
        actor = resources[0]
        actor_id, account_id, person_id = actor.id, actor.account_id, actor.person_id
        principal, key, _ = provision(db, resources)
        principal_id, key_id = principal.id, key.id
        execution = CRUDBase(models.FlowExecution).create(
            db, obj_in={"flow_id": resources[2].id, "status": "SUCCEEDED"}
        )
        record = CRUDBase(models.ExecutionResultGrant).create(
            db,
            obj_in={
                "account_id": account_id,
                "execution_id": execution.id,
                "principal_id": principal.id,
                "granted_by_user_id": actor_id,
                "result_sha256": "a" * 64,
                "binding": {},
                "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
            },
        )
        execution_id, grant_id = execution.id, record.id
        db.commit()

    first_authorized = Event()
    release_first = Event()
    second_preloaded = Event()
    second_query = Event()
    start_second = Event()
    phase = local()
    original_owner = ci_result_export._owner

    def authorize(db: Session, actor: models.User, grant: Any, operation: str) -> None:
        original_owner(db, actor, grant, operation)
        if getattr(phase, "first", False):
            first_authorized.set()
            assert release_first.wait(5), "Test failed to release first transaction"

    def observe_query(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if (
            getattr(phase, "second", False)
            and "FROM execution_result_grant" in statement
        ):
            second_query.set()

    def revoke(*, first: bool) -> datetime:
        with Session(db_engine) as db:
            actor = crud.crud_user.get(db, id=actor_id)
            if not first:
                # Keep this ORM object alive: the locked query must refresh it.
                stale = CRUDBase(models.ExecutionResultGrant).get(db, id=grant_id)
                assert stale.revoked_at is None
                second_preloaded.set()
                assert start_second.wait(5)
            phase.first, phase.second = first, not first
            ci_result_export.revoke_consent(
                db, actor=actor, execution_id=execution_id, grant_id=grant_id
            )
            value = (
                CRUDBase(models.ExecutionResultGrant).get(db, id=grant_id).revoked_at
            )
            assert value is not None
            return value

    monkeypatch.setattr(ci_result_export, "_owner", authorize)
    event.listen(db_engine, "before_cursor_execute", observe_query)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            second = pool.submit(revoke, first=False)
            assert second_preloaded.wait(5)
            first = pool.submit(revoke, first=True)
            try:
                assert first_authorized.wait(5)
                start_second.set()
                assert second_query.wait(5)
                with pytest.raises(TimeoutError):
                    second.result(timeout=0.2)
            finally:
                release_first.set()
                start_second.set()
            first_timestamp = first.result(timeout=5)
            assert second.result(timeout=5) == first_timestamp
        with Session(db_engine) as db:
            actor = crud.crud_user.get(db, id=actor_id)
            ci_result_export.revoke_consent(
                db, actor=actor, execution_id=execution_id, grant_id=grant_id
            )
            assert (
                CRUDBase(models.ExecutionResultGrant).get(db, id=grant_id).revoked_at
                == first_timestamp
            )
            assert (
                len(
                    crud.crud_audit_log.get_by_account(
                        db,
                        account_id=account_id,
                        action="execution_result_export_revoked",
                    )
                )
                == 3
            )
    finally:
        event.remove(db_engine, "before_cursor_execute", observe_query)
        with Session(db_engine) as db:
            CRUDBase(models.ApiKey).delete(db, id=key_id)
            CRUDBase(models.CiPrincipal).delete(db, id=principal_id)
            crud.crud_account.delete(db, id=account_id)
            CRUDBase(models.Person).delete(db, id=person_id)
