"""CRUD operations for persisted runtime-session replay runs."""

from __future__ import annotations

import uuid
from typing import Any, Optional, Union

from sqlalchemy.orm import Session

from ..models.runtime_session_replay_run import RuntimeSessionReplayRun
from .base import CRUDBase


class CRUDRuntimeSessionReplayRun(CRUDBase[RuntimeSessionReplayRun]):
    """CRUD operations for persisted replay runs."""

    def create_run(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        runtime_session_id: Union[uuid.UUID, str],
        suggestion_id: Optional[str],
        candidate: dict[str, Any],
        n_runs: int,
        input_delta_tokens: int,
        input_pct_saved: float,
        end_to_end_delta_median: Optional[float],
        end_to_end_delta_low: Optional[float],
        end_to_end_delta_high: Optional[float],
        inconclusive: bool,
        cost_spent: Optional[float],
        status: str,
        consented_by: Optional[str],
        requested_by: Optional[str],
        result: dict[str, Any],
        commit: bool = True,
    ) -> RuntimeSessionReplayRun:
        """Record one replay run outcome.

        Args:
            db: Database session.
            account_id: Owning account id.
            runtime_session_id: Runtime session the replay targeted.
            suggestion_id: Originating suggestion id, if any.
            candidate: The applied candidate (removed tools / filtered fields).
            n_runs: Re-execution pairs performed.
            input_delta_tokens: Exact deterministic input-token delta.
            input_pct_saved: Input-token delta as a fraction of the original.
            end_to_end_delta_median: Median end-to-end token delta, if measured.
            end_to_end_delta_low: Band floor of the end-to-end delta.
            end_to_end_delta_high: Band ceiling of the end-to-end delta.
            inconclusive: Whether the end-to-end delta is within noise.
            cost_spent: Dollars spent re-executing, if known.
            status: ``completed`` | ``aborted_budget`` | ``no_payload``.
            consented_by: Username that consented to the replay.
            requested_by: Username that requested the replay.
            result: Full measurement detail for display/audit.
            commit: Whether to commit the transaction.

        Returns:
            The stored replay-run row.
        """
        db_obj = RuntimeSessionReplayRun(
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            suggestion_id=suggestion_id,
            candidate=candidate,
            n_runs=n_runs,
            input_delta_tokens=input_delta_tokens,
            input_pct_saved=input_pct_saved,
            end_to_end_delta_median=end_to_end_delta_median,
            end_to_end_delta_low=end_to_end_delta_low,
            end_to_end_delta_high=end_to_end_delta_high,
            inconclusive=inconclusive,
            cost_spent=cost_spent,
            status=status,
            consented_by=consented_by,
            requested_by=requested_by,
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
    ) -> list[RuntimeSessionReplayRun]:
        """Return replay runs for one session, latest-first.

        Args:
            db: Database session.
            account_id: Owning account id.
            runtime_session_id: Runtime session id.
            limit: Maximum number of rows (capped at 100).

        Returns:
            Replay-run rows ordered latest-first.
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
