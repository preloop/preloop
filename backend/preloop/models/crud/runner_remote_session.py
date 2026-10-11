"""CRUD for remote harness sessions hosted by personal runners (#1482)."""

from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy.orm import Session

from preloop.models import models

from .base import CRUDBase

RunnerRemoteSession = models.RunnerRemoteSession
LIVE_STATES = models.RUNNER_SESSION_LIVE_STATES


class CRUDRunnerRemoteSession(CRUDBase[RunnerRemoteSession]):
    """Queries for runner-hosted sessions. State changes live in the service."""

    def get_for_runner(
        self, db: Session, *, runner_id: Any, remote_session_id: Any
    ) -> Optional[RunnerRemoteSession]:
        """One session, only if it belongs to ``runner_id``."""
        return (
            db.query(self.model)
            .filter(
                self.model.id == remote_session_id,
                self.model.runner_id == runner_id,
            )
            .first()
        )

    def get_for_account(
        self, db: Session, *, account_id: Any, remote_session_id: Any
    ) -> Optional[RunnerRemoteSession]:
        """One session scoped to an account."""
        return (
            db.query(self.model)
            .filter(
                self.model.id == remote_session_id,
                self.model.account_id == account_id,
            )
            .first()
        )

    def get_by_runtime_session(
        self, db: Session, *, account_id: Any, runtime_session_id: Any
    ) -> Optional[RunnerRemoteSession]:
        """The runner session paired with a ``RuntimeSession``."""
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.runtime_session_id == runtime_session_id,
            )
            .first()
        )

    def list_live_for_runner(
        self, db: Session, *, runner_id: Any
    ) -> List[RunnerRemoteSession]:
        """Sessions still holding a slot on one runner, oldest first."""
        return (
            db.query(self.model)
            .filter(
                self.model.runner_id == runner_id,
                self.model.state.in_(LIVE_STATES),
            )
            .order_by(self.model.requested_at)
            .all()
        )

    def list_live_for_account(
        self, db: Session, *, account_id: Any
    ) -> List[RunnerRemoteSession]:
        """Live sessions of one account."""
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.state.in_(LIVE_STATES),
            )
            .all()
        )

    def list_for_runner(
        self, db: Session, *, runner_id: Any, limit: int = 50
    ) -> List[RunnerRemoteSession]:
        """Recent sessions of one runner, newest first."""
        return (
            db.query(self.model)
            .filter(self.model.runner_id == runner_id)
            .order_by(self.model.requested_at.desc())
            .limit(limit)
            .all()
        )

    def list_live_inactive_since(
        self, db: Session, *, cutoff: datetime, limit: int = 500
    ) -> List[RunnerRemoteSession]:
        """Live sessions with no activity since ``cutoff`` (sweeper input)."""
        return (
            db.query(self.model)
            .filter(
                self.model.state.in_(LIVE_STATES),
                self.model.last_activity_at < cutoff,
            )
            .limit(limit)
            .all()
        )


crud_runner_remote_session = CRUDRunnerRemoteSession(RunnerRemoteSession)
