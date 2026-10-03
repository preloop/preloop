"""The permission hook's cwd labels the session it belongs to (#1148)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import Engine, delete
from sqlalchemy.orm import sessionmaker

from preloop.api.endpoints import agent_permission as endpoint
from preloop.models import models
from preloop.models.crud import crud_account


@pytest.fixture
def two_accounts(db_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    """Commit two accounts with one session each; the helper opens its own session."""
    factory = sessionmaker(bind=db_engine)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ids: dict = {}
    with factory() as db:
        for name in ("mine", "theirs"):
            account = crud_account.create(db, obj_in={"organization_name": uuid4().hex})
            session = models.RuntimeSession(
                account_id=account.id,
                session_source_type="claude_code",
                session_source_id=uuid4().hex,
                started_at=now,
            )
            db.add(session)
            db.commit()
            ids[name] = {"account_id": account.id, "session_id": session.id}
    monkeypatch.setattr(endpoint, "get_session_factory", lambda: factory)
    try:
        yield {"factory": factory, **ids}
    finally:
        with factory() as db:
            for name in ("mine", "theirs"):
                db.execute(
                    delete(models.Account).where(
                        models.Account.id == ids[name]["account_id"]
                    )
                )
            db.commit()


def _identity(account_id: object) -> endpoint.PermissionIdentity:
    return endpoint.PermissionIdentity(
        account_id=str(account_id),
        user_id=uuid4(),
        api_key_id=uuid4(),
        managed_agent_id=None,
        runtime_session_id=None,
        managed_agent_name="Agent",
    )


def _cwd(factory: sessionmaker, session_id: object) -> str | None:
    with factory() as db:
        return db.get(models.RuntimeSession, session_id).cwd


def test_cwd_is_recorded_trimmed_and_bounded(two_accounts: dict) -> None:
    mine = two_accounts["mine"]
    identity = _identity(mine["account_id"])

    endpoint._record_session_cwd(identity, mine["session_id"], "  /work/alpha \n")
    assert _cwd(two_accounts["factory"], mine["session_id"]) == "/work/alpha"

    endpoint._record_session_cwd(identity, mine["session_id"], "/" + "x" * 2000)
    stored = _cwd(two_accounts["factory"], mine["session_id"])
    assert stored is not None
    assert len(stored) == endpoint.MAX_SESSION_CWD_CHARS


def test_cwd_never_crosses_accounts(two_accounts: dict) -> None:
    mine = two_accounts["mine"]
    theirs = two_accounts["theirs"]

    endpoint._record_session_cwd(
        _identity(mine["account_id"]), theirs["session_id"], "/work/intruder"
    )

    assert _cwd(two_accounts["factory"], theirs["session_id"]) is None


def test_store_failure_does_not_raise(
    two_accounts: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_factory() -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(endpoint, "get_session_factory", broken_factory)

    endpoint._record_session_cwd(
        _identity(two_accounts["mine"]["account_id"]),
        two_accounts["mine"]["session_id"],
        "/work/alpha",
    )
