"""Tests for runtime session optimization result CRUD helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from preloop.models.crud import crud_runtime_session_optimization_result
from preloop.models.crud.runtime_session import crud_runtime_session
from preloop.models.models.runtime_session_optimization_result import (
    RuntimeSessionOptimizationResult,
)


def _create_runtime_session(db_session, account_id) -> object:
    now = datetime.now(UTC)
    return crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="claude_code",
        session_source_id=f"opt-cache-{uuid4()}",
        session_reference="opt-cache",
        runtime_principal_type="claude_code",
        runtime_principal_id="opt-cache",
        runtime_principal_name="Claude Workspace",
        started_at=now,
        last_activity_at=now,
    )


def test_upsert_inserts_and_updates_cached_response(db_session, create_account) -> None:
    """Upsert should create one row and replace it on the same scope."""
    account = create_account()
    runtime_session = _create_runtime_session(db_session, account.id)
    db_session.commit()
    scope_hash = "scope-a"
    first_response = {"generated_by": "model", "suggestions": []}
    second_response = {"generated_by": "local", "suggestions": [{"id": "s1"}]}

    created = crud_runtime_session_optimization_result.upsert(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        scope_hash=scope_hash,
        model_id="model-1",
        response=first_response,
    )
    updated = crud_runtime_session_optimization_result.upsert(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        scope_hash=scope_hash,
        model_id="model-2",
        response=second_response,
    )

    assert created.id == updated.id
    assert updated.model_id == "model-2"
    assert updated.response == second_response
    rows = (
        db_session.query(RuntimeSessionOptimizationResult)
        .filter(
            RuntimeSessionOptimizationResult.runtime_session_id == runtime_session.id
        )
        .all()
    )
    assert len(rows) == 1


def test_get_by_scope_returns_none_for_missing_cache(
    db_session, create_account
) -> None:
    """Unknown scope hashes should not return a cached row."""
    account = create_account()
    runtime_session = _create_runtime_session(db_session, account.id)
    db_session.commit()

    cached = crud_runtime_session_optimization_result.get_by_scope(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        scope_hash="missing",
    )

    assert cached is None


def test_upsert_retries_after_unique_constraint_race(
    db_session, create_account
) -> None:
    """Concurrent inserts should recover from the scope unique constraint."""
    account = create_account()
    runtime_session = _create_runtime_session(db_session, account.id)
    db_session.commit()
    scope_hash = "race-scope"
    response = {"generated_by": "model", "suggestions": []}
    existing = RuntimeSessionOptimizationResult(
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        scope_hash=scope_hash,
        model_id="model-1",
        response={"generated_by": "local", "suggestions": []},
    )
    db_session.add(existing)
    db_session.commit()

    with patch(
        "preloop.models.crud.runtime_session_optimization_result."
        "CRUDRuntimeSessionOptimizationResult.get_by_scope",
        side_effect=[None, existing],
    ):
        db_session.commit = MagicMock(
            side_effect=[IntegrityError("insert", {}, Exception("dup")), None]
        )
        db_session.rollback = MagicMock()
        db_session.refresh = MagicMock()

        result = crud_runtime_session_optimization_result.upsert(
            db_session,
            account_id=account.id,
            runtime_session_id=runtime_session.id,
            scope_hash=scope_hash,
            model_id="model-2",
            response=response,
        )

    assert result is existing
    assert existing.model_id == "model-2"
    assert existing.response == response
    db_session.rollback.assert_called_once()
