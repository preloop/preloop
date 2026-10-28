"""Account-scoped membership revocation and durable owner notifications."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from preloop.models import models


def email_domain(email: str | None) -> str:
    """Match canonical email domains to the IDNA form stored by domain claims."""
    domain = (email or "").rpartition("@")[2].strip().rstrip(".").lower()
    try:
        return domain.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def owners(db: Session, *, account_id: UUID) -> list[models.User]:
    """Return active owners from this account and valid role assignments."""
    return list(
        db.scalars(
            select(models.User)
            .join(models.UserRole, models.UserRole.user_id == models.User.id)
            .join(models.Role, models.Role.id == models.UserRole.role_id)
            .where(
                models.User.account_id == account_id,
                models.User.is_active.is_(True),
                models.Role.name == "owner",
                or_(
                    models.Role.account_id == account_id,
                    (
                        models.Role.account_id.is_(None)
                        & models.Role.is_system_role.is_(True)
                    ),
                ),
            )
        ).unique()
    )


def revoke_non_sso(db: Session, *, account_id: UUID, domains: set[str]) -> int:
    """Bump matching memberships only, without changing the canonical person."""
    rows = list(
        db.execute(
            select(models.User, models.Person.email_normalized)
            .join(models.Person, models.Person.id == models.User.person_id)
            .where(models.User.account_id == account_id)
            .order_by(models.User.id)
            .with_for_update(of=models.User)
        )
    )
    count = 0
    for user, email in rows:
        if email_domain(email) in domains:
            user.non_sso_auth_generation += 1
            count += 1
    db.flush()
    return count


def enqueue_owner_notices(
    db: Session, *, user: models.User, recipients: list[models.User]
) -> None:
    """Enqueue one message per distinct owner address in the login transaction."""
    login_id = uuid4()
    for email in sorted({owner.email for owner in recipients if owner.email}):
        db.add(
            models.SsoBreakGlassNotice(
                account_id=user.account_id,
                login_id=login_id,
                actor_email=user.email,
                recipient_email=email,
            )
        )
    db.flush()


def pending_notices(
    db: Session, *, limit: int = 50
) -> list[models.SsoBreakGlassNotice]:
    """Claim due notices through delivery, allowing multiple scheduler workers."""
    now = datetime.now(UTC)
    return list(
        db.scalars(
            select(models.SsoBreakGlassNotice)
            .where(
                models.SsoBreakGlassNotice.delivered_at.is_(None),
                or_(
                    models.SsoBreakGlassNotice.retry_at.is_(None),
                    models.SsoBreakGlassNotice.retry_at <= now,
                ),
            )
            .order_by(models.SsoBreakGlassNotice.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )


def finish_notice(
    db: Session, *, notice: models.SsoBreakGlassNotice, delivered: bool
) -> None:
    """Record success or bounded exponential retry without dropping a notice."""
    now = datetime.now(UTC)
    notice.attempts += 1
    if delivered:
        notice.delivered_at = now
    else:
        notice.retry_at = now + timedelta(
            seconds=min(3600, 30 * 2 ** min(notice.attempts, 7))
        )
    db.flush()
