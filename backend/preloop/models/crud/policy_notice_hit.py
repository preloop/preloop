"""CRUD operations for policy notice hits (``notify`` model I/O matches)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional
from uuid import UUID

from sqlalchemy import case, func, or_, select, text
from sqlalchemy.orm import Session

from ..models.permission import Permission, Role, RolePermission, TeamRole, UserRole
from ..models.policy_notice_hit import PolicyNoticeHit
from ..models.team import Team, TeamMembership
from ..models.user import User
from .base import CRUDBase


@dataclass(frozen=True)
class PolicyNoticeRuleSummary:
    """Hits of one rule inside a window, with the most recent one."""

    rule_id: str
    rule_description: Optional[str]
    target: str
    count: int
    last_hit_id: UUID
    last_hit_at: datetime
    last_excerpt: Optional[str]
    last_user_id: Optional[UUID]
    last_username: Optional[str]


def _naive_utc(value: datetime) -> datetime:
    """``created_at`` is naive UTC (house convention); compare like with like."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class CRUDPolicyNoticeHit(CRUDBase[PolicyNoticeHit]):
    """CRUD operations for policy notice hits."""

    def record(
        self,
        db: Session,
        *,
        account_id: UUID,
        user_id: Optional[UUID],
        target: str,
        rule_id: str,
        rule_description: Optional[str],
        text_sha256: str,
        excerpt: Optional[str],
        now: Optional[datetime] = None,
    ) -> PolicyNoticeHit:
        """Insert one hit and flush it. The caller commits.

        Args:
            db: Database session.
            account_id: Account whose policy matched.
            user_id: User whose model call matched, when known.
            target: ``model.request`` or ``model.response``.
            rule_id: Matching rule id.
            rule_description: Human-readable rule or condition description.
            text_sha256: SHA-256 of the scanned text.
            excerpt: Redacted excerpt, or None when redaction failed.
            now: Hit time. Set from the application clock rather than the
                transaction's ``now()`` so hits order by when they happened.

        Returns:
            The flushed hit.
        """
        hit = PolicyNoticeHit(
            account_id=account_id,
            user_id=user_id,
            target=target,
            rule_id=rule_id,
            rule_description=rule_description,
            text_sha256=text_sha256,
            excerpt=excerpt,
            created_at=_naive_utc(now or datetime.now(timezone.utc)),
        )
        db.add(hit)
        db.flush()
        return hit

    def claim_notification(
        self,
        db: Session,
        *,
        hit: PolicyNoticeHit,
        window: timedelta,
        now: Optional[datetime] = None,
    ) -> bool:
        """Mark ``hit`` as the one that notifies, unless one did recently.

        At most one outbound message per rule and user per ``window``. A
        transaction-scoped advisory lock on (account, rule, user) serialises
        concurrent claims across replicas; it is released when the caller
        commits or rolls back.

        Args:
            db: Database session. The caller commits.
            hit: The freshly recorded hit.
            window: Debounce window.
            now: Injected clock for tests.

        Returns:
            True when this hit should send the notice.
        """
        moment = now or datetime.now(timezone.utc)
        lock_key = f"policy_notice:{hit.account_id}:{hit.rule_id}:{hit.user_id or '-'}"
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": lock_key}
        )
        query = select(PolicyNoticeHit.id).where(
            PolicyNoticeHit.account_id == hit.account_id,
            PolicyNoticeHit.rule_id == hit.rule_id,
            PolicyNoticeHit.notified_at.is_not(None),
            PolicyNoticeHit.notified_at > moment - window,
        )
        if hit.user_id is None:
            query = query.where(PolicyNoticeHit.user_id.is_(None))
        else:
            query = query.where(PolicyNoticeHit.user_id == hit.user_id)
        if db.execute(query.limit(1)).first() is not None:
            return False
        hit.notified_at = moment
        db.add(hit)
        db.flush()
        return True

    def summarize_by_rule(
        self,
        db: Session,
        *,
        account_id: Any,
        since: datetime,
        limit: int = 100,
    ) -> List[PolicyNoticeRuleSummary]:
        """Per-rule hit counts since ``since``, newest rule activity first.

        Args:
            db: Database session.
            account_id: Account to summarise.
            since: Window start (aware or naive UTC).
            limit: Maximum number of rules returned.

        Returns:
            One summary per rule with at least one hit in the window.
        """
        start = _naive_utc(since)
        counts = (
            select(
                PolicyNoticeHit.rule_id.label("rule_id"),
                func.count(PolicyNoticeHit.id).label("hit_count"),
            )
            .where(
                PolicyNoticeHit.account_id == account_id,
                PolicyNoticeHit.created_at >= start,
            )
            .group_by(PolicyNoticeHit.rule_id)
            .subquery()
        )
        ranked = (
            select(
                PolicyNoticeHit,
                func.row_number()
                .over(
                    partition_by=PolicyNoticeHit.rule_id,
                    order_by=(
                        PolicyNoticeHit.created_at.desc(),
                        PolicyNoticeHit.id.desc(),
                    ),
                )
                .label("rank"),
            )
            .where(
                PolicyNoticeHit.account_id == account_id,
                PolicyNoticeHit.created_at >= start,
            )
            .subquery()
        )
        latest = select(ranked).where(ranked.c.rank == 1).subquery()
        rows = db.execute(
            select(
                latest.c.rule_id,
                latest.c.rule_description,
                latest.c.target,
                counts.c.hit_count,
                latest.c.id,
                latest.c.created_at,
                latest.c.excerpt,
                latest.c.user_id,
                User.username,
            )
            .join(counts, counts.c.rule_id == latest.c.rule_id)
            .outerjoin(User, User.id == latest.c.user_id)
            .order_by(latest.c.created_at.desc())
            .limit(limit)
        ).all()
        return [
            PolicyNoticeRuleSummary(
                rule_id=row[0],
                rule_description=row[1],
                target=row[2],
                count=int(row[3]),
                last_hit_id=row[4],
                last_hit_at=row[5].replace(tzinfo=timezone.utc),
                last_excerpt=row[6],
                last_user_id=row[7],
                last_username=row[8],
            )
            for row in rows
        ]

    def get_policy_owners(
        self,
        db: Session,
        *,
        account_id: Any,
        primary_user_id: Optional[Any] = None,
        permission_name: str = "manage_policies",
        limit: int = 500,
    ) -> List[User]:
        """Active users of an account who may manage its policies.

        The predicate runs in SQL so the recipients do not depend on which
        slice of a large account a capped user scan happens to return. It
        mirrors ``preloop.utils.permissions.user_holds_permission``: a role
        counts when it belongs to the account (or is global) and is either
        the system ``owner`` role or grants ``permission_name``, whether it
        is held directly or through a team of the same account. The primary
        user and superusers always count.

        Args:
            db: Database session.
            account_id: Account whose owners to list.
            primary_user_id: The account's primary user, listed first.
            permission_name: Permission that makes a user an owner.
            limit: Maximum number of users returned.

        Returns:
            Owners, primary user first, then by username.
        """
        granting_roles = select(Role.id).where(
            or_(Role.account_id.is_(None), Role.account_id == account_id),
            or_(
                (Role.name == "owner") & Role.is_system_role.is_(True),
                Role.id.in_(
                    select(RolePermission.role_id)
                    .join(Permission, Permission.id == RolePermission.permission_id)
                    .where(Permission.name == permission_name)
                ),
            ),
        )
        direct = select(UserRole.user_id).where(UserRole.role_id.in_(granting_roles))
        via_team = (
            select(TeamMembership.user_id)
            .join(Team, Team.id == TeamMembership.team_id)
            .join(TeamRole, TeamRole.team_id == TeamMembership.team_id)
            .where(
                Team.account_id == account_id,
                TeamRole.role_id.in_(granting_roles),
            )
        )
        conditions = [
            User.is_superuser.is_(True),
            User.id.in_(direct),
            User.id.in_(via_team),
        ]
        order_by: List[Any] = []
        if primary_user_id is not None:
            conditions.append(User.id == primary_user_id)
            order_by.append(case((User.id == primary_user_id, 0), else_=1))
        order_by += [User.username, User.id]
        return list(
            db.scalars(
                select(User)
                .where(
                    User.account_id == account_id,
                    User.is_active.is_(True),
                    or_(*conditions),
                )
                .order_by(*order_by)
                .limit(limit)
            ).all()
        )


crud_policy_notice_hit = CRUDPolicyNoticeHit(PolicyNoticeHit)
