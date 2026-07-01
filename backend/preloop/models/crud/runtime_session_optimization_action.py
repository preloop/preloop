"""CRUD operations for applied runtime-session optimization actions."""

from __future__ import annotations

from typing import Any, Optional, Union

import uuid

from sqlalchemy.orm import Session

from ..models.runtime_session_optimization_action import (
    RuntimeSessionOptimizationAction,
)
from .base import CRUDBase


class CRUDRuntimeSessionOptimizationAction(CRUDBase[RuntimeSessionOptimizationAction]):
    """CRUD operations for applied optimization actions."""

    def create_applied(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        runtime_session_id: Union[uuid.UUID, str],
        suggestion_id: str,
        suggestion_title: Optional[str],
        action_type: str,
        params: dict[str, Any],
        applied_by: Optional[str],
        runtime_principal_id: Optional[str],
        baseline: Optional[dict[str, Any]],
        result: dict[str, Any],
        commit: bool = True,
    ) -> RuntimeSessionOptimizationAction:
        """Record one applied optimization action.

        Args:
            db: Database session.
            account_id: Owning account id.
            runtime_session_id: Runtime session the action was applied from.
            suggestion_id: Id of the originating suggestion.
            suggestion_title: Title of the originating suggestion.
            action_type: Applied action type, e.g. ``scope_tools``.
            params: Action parameters as applied.
            applied_by: Username of the applying user.
            runtime_principal_id: Runtime principal the action targets.
            baseline: Pre-apply usage averages for outcome measurement.
            result: Details of what was created or changed.
            commit: Whether to commit the transaction.

        Returns:
            The stored action row.
        """
        db_obj = RuntimeSessionOptimizationAction(
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            suggestion_id=suggestion_id,
            suggestion_title=suggestion_title,
            action_type=action_type,
            params=params,
            status="applied",
            applied_by=applied_by,
            runtime_principal_id=runtime_principal_id,
            baseline=baseline,
            result=result,
        )
        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        return db_obj

    def list_for_session(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        runtime_session_id: Union[uuid.UUID, str],
        limit: int = 50,
    ) -> list[RuntimeSessionOptimizationAction]:
        """Return applied actions for one session, latest-first.

        Args:
            db: Database session.
            account_id: Owning account id.
            runtime_session_id: Runtime session id.
            limit: Maximum number of rows (capped at 100).

        Returns:
            Applied action rows ordered latest-first.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.runtime_session_id == runtime_session_id,
            )
            .order_by(self.model.created_at.desc())
            .limit(min(max(limit, 1), 100))
            .all()
        )

    def exists_for_suggestion(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        runtime_session_id: Union[uuid.UUID, str],
        suggestion_id: str,
        action_type: str,
    ) -> bool:
        """Check whether the same suggestion action was already applied.

        Args:
            db: Database session.
            account_id: Owning account id.
            runtime_session_id: Runtime session id.
            suggestion_id: Id of the originating suggestion.
            action_type: Applied action type.

        Returns:
            True when a matching applied action exists.
        """
        return (
            db.query(self.model.id)
            .filter(
                self.model.account_id == account_id,
                self.model.runtime_session_id == runtime_session_id,
                self.model.suggestion_id == suggestion_id,
                self.model.action_type == action_type,
            )
            .first()
            is not None
        )
