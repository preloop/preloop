"""Artifact PUTs replay a transaction PostgreSQL aborted on a lock cycle."""

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from psycopg import errors
from sqlalchemy.exc import OperationalError

from preloop.api.endpoints import flow_artifacts as endpoint


def _operational(orig: Exception) -> OperationalError:
    return OperationalError("SELECT 1", {}, orig)


CLAIMS: dict[str, Any] = {
    "account_id": uuid4(),
    "flow_id": uuid4(),
    "thread_id": "thread",
    "kind": "workspace",
}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(endpoint.time, "sleep", lambda _seconds: None)


def test_deadlock_is_rolled_back_and_replayed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    reference = object()

    def put(*_args: Any, **_kwargs: Any) -> object:
        calls.append(1)
        if len(calls) == 1:
            raise _operational(errors.DeadlockDetected("deadlock detected"))
        return reference

    monkeypatch.setattr(endpoint, "put_artifact", put)
    db = MagicMock()
    result = endpoint._put_with_retry(db, uuid4(), CLAIMS, b"archive")
    assert result is reference
    assert len(calls) == 2
    db.rollback.assert_called_once()


def test_persistent_deadlock_surfaces_after_bounded_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def put(*_args: Any, **_kwargs: Any) -> object:
        calls.append(1)
        raise _operational(errors.SerializationFailure("could not serialize"))

    monkeypatch.setattr(endpoint, "put_artifact", put)
    with pytest.raises(OperationalError):
        endpoint._put_with_retry(MagicMock(), uuid4(), CLAIMS, b"archive")
    assert len(calls) == endpoint.ARTIFACT_PUT_ATTEMPTS


def test_other_operational_errors_are_not_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def put(*_args: Any, **_kwargs: Any) -> object:
        calls.append(1)
        raise _operational(errors.LockNotAvailable("lock timeout"))

    monkeypatch.setattr(endpoint, "put_artifact", put)
    with pytest.raises(OperationalError):
        endpoint._put_with_retry(MagicMock(), uuid4(), CLAIMS, b"archive")
    assert len(calls) == 1


class _Psycopg2DeadlockError(Exception):
    """psycopg2 exposes the SQLSTATE as ``pgcode`` and has no ``sqlstate``."""

    pgcode = "40P01"


def test_psycopg2_deadlock_is_replayed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    reference = object()

    def put(*_args: Any, **_kwargs: Any) -> object:
        calls.append(1)
        if len(calls) == 1:
            raise _operational(_Psycopg2DeadlockError("deadlock detected"))
        return reference

    monkeypatch.setattr(endpoint, "put_artifact", put)
    assert endpoint._put_with_retry(MagicMock(), uuid4(), CLAIMS, b"x") is reference
    assert len(calls) == 2
