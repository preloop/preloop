"""CRUD operations for cached runtime-session optimization results."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models.runtime_session_optimization_result import (
    RuntimeSessionOptimizationResult,
)
from .base import CRUDBase


class CRUDRuntimeSessionOptimizationResult(CRUDBase[RuntimeSessionOptimizationResult]):
    """CRUD operations for cached optimization responses."""

    def get_by_scope(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        scope_hash: str,
    ) -> Optional[RuntimeSessionOptimizationResult]:
        """Return the cached result for one session/scope pair, if any.

        Args:
            db: Database session.
            account_id: Owning account id.
            runtime_session_id: Runtime session id.
            scope_hash: Stable hash of the request scope and model.

        Returns:
            Cached result row or ``None``.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.runtime_session_id == runtime_session_id,
                self.model.scope_hash == scope_hash,
            )
            .first()
        )

    def upsert(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        scope_hash: str,
        model_id: Optional[str],
        response: dict[str, Any],
        commit: bool = True,
    ) -> RuntimeSessionOptimizationResult:
        """Insert or replace the cached result for one session/scope pair.

        Args:
            db: Database session.
            account_id: Owning account id.
            runtime_session_id: Runtime session id.
            scope_hash: Stable hash of the request scope and model.
            model_id: Model used for generation, if any.
            response: Serialized optimization response payload.
            commit: Whether to commit the transaction.

        Returns:
            The stored cache row.
        """
        existing = self.get_by_scope(
            db,
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            scope_hash=scope_hash,
        )
        if existing is not None:
            existing.model_id = model_id
            existing.response = response
            db.add(existing)
            db_obj = existing
        else:
            db_obj = RuntimeSessionOptimizationResult(
                account_id=account_id,
                runtime_session_id=runtime_session_id,
                scope_hash=scope_hash,
                model_id=model_id,
                response=response,
            )
            db.add(db_obj)
        if not commit:
            return db_obj
        try:
            db.commit()
            db.refresh(db_obj)
            return db_obj
        except IntegrityError:
            # Concurrent inserts can race on the session/scope unique constraint.
            db.rollback()
            existing = self.get_by_scope(
                db,
                account_id=account_id,
                runtime_session_id=runtime_session_id,
                scope_hash=scope_hash,
            )
            if existing is None:
                raise
            existing.model_id = model_id
            existing.response = response
            db.add(existing)
            db.commit()
            db.refresh(existing)
            return existing
