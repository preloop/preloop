"""Persistence for runner share grants and the owner consent policy (EE #186).

The EE runner sharing plugin owns the rules; this module only reads and
writes rows. Every read is scoped to one account.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from preloop.models import models

RunnerShareGrant = models.RunnerShareGrant
RunnerSharingPolicy = models.RunnerSharingPolicy

#: Owner consent policy when no row says otherwise.
DEFAULT_OWNER_CONSENT = "required"


def _uuid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CRUDRunnerShare:
    """Grant and policy rows for runner sharing."""

    # -- grants -------------------------------------------------------------

    def create_grant(
        self,
        db: Session,
        *,
        account_id: Any,
        runner_id: Any,
        owner_user_id: Any,
        grantee_type: str,
        grantee_id: Any,
        scopes: Sequence[str],
        harnesses: Optional[Sequence[str]],
        directory_ids: Optional[Sequence[str]],
        consent_state: str,
        consent_source: Optional[str],
        created_by_user_id: Any,
        commit: bool = True,
    ) -> RunnerShareGrant:
        """Insert one grant.

        Args:
            db: Database session.
            account_id: Account of the runner.
            runner_id: The shared runner.
            owner_user_id: Runner owner at grant time.
            grantee_type: ``team`` or ``user``.
            grantee_id: Team or user id.
            scopes: Subset of ``flows`` and ``sessions``.
            harnesses: Harness ids, or None for all.
            directory_ids: Authorized directory ids, or None for all.
            consent_state: Initial consent state.
            consent_source: How consent was given, when active.
            created_by_user_id: Acting user.
            commit: Commit after the insert.

        Returns:
            The new grant.
        """
        grant = RunnerShareGrant(
            account_id=_uuid(account_id),
            runner_id=_uuid(runner_id),
            owner_user_id=_uuid(owner_user_id) if owner_user_id else None,
            grantee_type=grantee_type,
            grantee_id=_uuid(grantee_id),
            scopes=sorted(set(scopes)),
            harnesses=sorted(set(harnesses)) if harnesses is not None else None,
            directory_ids=(
                sorted(set(directory_ids)) if directory_ids is not None else None
            ),
            consent_state=consent_state,
            consent_source=consent_source,
            consented_at=_now() if consent_state == "active" else None,
            created_by_user_id=(
                _uuid(created_by_user_id) if created_by_user_id else None
            ),
        )
        db.add(grant)
        if commit:
            db.commit()
            db.refresh(grant)
        else:
            db.flush()
        return grant

    def get_grant(
        self, db: Session, *, grant_id: Any, account_id: Any
    ) -> Optional[RunnerShareGrant]:
        """One grant in the account, revoked or not."""
        return db.scalar(
            select(RunnerShareGrant).where(
                RunnerShareGrant.id == _uuid(grant_id),
                RunnerShareGrant.account_id == _uuid(account_id),
            )
        )

    def list_for_runner(
        self,
        db: Session,
        *,
        account_id: Any,
        runner_id: Any,
        include_revoked: bool = False,
    ) -> List[RunnerShareGrant]:
        """Grants on one runner, newest first."""
        query = select(RunnerShareGrant).where(
            RunnerShareGrant.account_id == _uuid(account_id),
            RunnerShareGrant.runner_id == _uuid(runner_id),
        )
        if not include_revoked:
            query = query.where(RunnerShareGrant.revoked_at.is_(None))
        return list(db.scalars(query.order_by(RunnerShareGrant.created_at.desc())))

    def active_grants_for_user(
        self,
        db: Session,
        *,
        account_id: Any,
        runner_id: Any,
        user_id: Any,
        team_ids: Iterable[Any] = (),
    ) -> List[RunnerShareGrant]:
        """Live, consented grants on a runner that name the user or a team."""
        teams = [_uuid(team) for team in team_ids]
        grantee = (RunnerShareGrant.grantee_type == "user") & (
            RunnerShareGrant.grantee_id == _uuid(user_id)
        )
        if teams:
            grantee = or_(
                grantee,
                (RunnerShareGrant.grantee_type == "team")
                & (RunnerShareGrant.grantee_id.in_(teams)),
            )
        return list(
            db.scalars(
                select(RunnerShareGrant).where(
                    RunnerShareGrant.account_id == _uuid(account_id),
                    RunnerShareGrant.runner_id == _uuid(runner_id),
                    RunnerShareGrant.revoked_at.is_(None),
                    RunnerShareGrant.consent_state == "active",
                    grantee,
                )
            )
        )

    def team_ids_for_user(self, db: Session, *, user_id: Any) -> List[uuid.UUID]:
        """Teams the user belongs to."""
        return list(
            db.scalars(
                select(models.TeamMembership.team_id).where(
                    models.TeamMembership.user_id == _uuid(user_id)
                )
            )
        )

    def set_consent(
        self,
        db: Session,
        grant: RunnerShareGrant,
        *,
        state: str,
        source: Optional[str],
        commit: bool = True,
    ) -> RunnerShareGrant:
        """Move a grant to ``active`` or ``declined``."""
        grant.consent_state = state
        grant.consent_source = source
        grant.consented_at = _now() if state == "active" else None
        db.add(grant)
        if commit:
            db.commit()
            db.refresh(grant)
        else:
            db.flush()
        return grant

    def revoke(
        self,
        db: Session,
        grant: RunnerShareGrant,
        *,
        revoked_by_user_id: Any,
        commit: bool = True,
    ) -> RunnerShareGrant:
        """Revoke a grant. Idempotent."""
        if grant.revoked_at is None:
            grant.revoked_at = _now()
            grant.revoked_by_user_id = (
                _uuid(revoked_by_user_id) if revoked_by_user_id else None
            )
            db.add(grant)
        if commit:
            db.commit()
            db.refresh(grant)
        else:
            db.flush()
        return grant

    # -- owner consent policy ----------------------------------------------

    def get_policy(
        self, db: Session, *, account_id: Any, runner_id: Any = None
    ) -> Optional[RunnerSharingPolicy]:
        """The account row (``runner_id`` None) or one runner's row."""
        query = select(RunnerSharingPolicy).where(
            RunnerSharingPolicy.account_id == _uuid(account_id)
        )
        if runner_id is None:
            query = query.where(RunnerSharingPolicy.runner_id.is_(None))
        else:
            query = query.where(RunnerSharingPolicy.runner_id == _uuid(runner_id))
        return db.scalar(query)

    def set_policy(
        self,
        db: Session,
        *,
        account_id: Any,
        runner_id: Any,
        owner_consent: Optional[str],
        updated_by_user_id: Any,
        commit: bool = True,
    ) -> Optional[RunnerSharingPolicy]:
        """Write or clear a policy row.

        ``owner_consent=None`` deletes the row: the account row falls back to
        the default, a runner row falls back to the account row.
        """
        row = self.get_policy(db, account_id=account_id, runner_id=runner_id)
        if owner_consent is None:
            if row is not None:
                db.delete(row)
            row = None
        else:
            if row is None:
                row = RunnerSharingPolicy(
                    account_id=_uuid(account_id),
                    runner_id=_uuid(runner_id) if runner_id is not None else None,
                )
            row.owner_consent = owner_consent
            row.updated_by_user_id = (
                _uuid(updated_by_user_id) if updated_by_user_id else None
            )
            db.add(row)
        if commit:
            db.commit()
            if row is not None:
                db.refresh(row)
        else:
            db.flush()
        return row


crud_runner_share = CRUDRunnerShare()
