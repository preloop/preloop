"""Atomic membership-only revocation; canonical identities never fan out here."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from preloop.models import models


@dataclass(frozen=True)
class OffboardingResult:
    """Counts describe this transition, not previously revoked credentials."""

    user_id: UUID
    already_offboarded: bool = False
    counts: dict[str, int] = field(default_factory=dict)
    preserved_machine_key_ids: tuple[UUID, ...] = ()


def account_machine_key_ids(db: Session, *, account_id: UUID) -> set[UUID]:
    """Identify account-owned credentials from durable database bindings."""
    ci = db.scalars(
        select(models.ApiKey.id)
        .join(
            models.CiPrincipal, models.ApiKey.ci_principal_id == models.CiPrincipal.id
        )
        .where(
            models.ApiKey.account_id == account_id,
            models.CiPrincipal.account_id == account_id,
        )
    )
    agents = db.scalars(
        select(models.ApiKey.id)
        .join(
            models.ManagedAgentCredential,
            models.ManagedAgentCredential.api_key_id == models.ApiKey.id,
        )
        .join(
            models.ManagedAgent,
            models.ManagedAgent.id == models.ManagedAgentCredential.managed_agent_id,
        )
        .where(
            models.ApiKey.account_id == account_id,
            models.ManagedAgentCredential.account_id == account_id,
            models.ManagedAgent.account_id == account_id,
        )
    )
    return set(ci) | set(agents)


def offboard_record(
    db: Session, *, account_id: UUID, user_id: UUID, reason: str
) -> OffboardingResult:
    """Stage one complete revocation without committing or changing siblings.

    Locks account, canonical person and membership in that order. The account
    lock also serializes runner removal with runner job leasing.
    """
    from preloop.models.crud import crud_cli_session, crud_flow_runner

    account = db.scalar(
        select(models.Account).where(models.Account.id == account_id).with_for_update()
    )
    person_id = db.scalar(
        select(models.User.person_id).where(
            models.User.id == user_id, models.User.account_id == account_id
        )
    )
    if account is None or person_id is None:
        raise ValueError("Membership not found in account")
    db.scalar(
        select(models.Person).where(models.Person.id == person_id).with_for_update()
    )
    user = db.scalar(
        select(models.User)
        .where(models.User.id == user_id, models.User.account_id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise ValueError("Membership not found in account")
    keys = list(
        db.scalars(
            select(models.ApiKey)
            .where(
                models.ApiKey.account_id == account_id, models.ApiKey.user_id == user_id
            )
            .with_for_update()
        )
    )
    machine_keys = account_machine_key_ids(db, account_id=account_id)
    preserved = tuple(key.id for key in keys if key.id in machine_keys)
    if not user.is_active and user.offboarded_at is not None:
        return OffboardingResult(
            user_id=user_id,
            already_offboarded=True,
            preserved_machine_key_ids=preserved,
        )

    from preloop.models.crud.capacity import record_capacity_change

    if user.is_active:
        record_capacity_change(db, str(account_id))
    now = datetime.now(UTC)
    user.is_active = False
    user.auth_generation += 1
    user.offboarded_at = now
    counts = {
        "session_generations": 1,
        "personal_keys": 0,
        "runtime_sessions": 0,
        "runners": 0,
        "stopped_executions": 0,
    }
    counts["cli_sessions"] = crud_cli_session.revoke_all(
        db, user_id=user_id, commit=False
    )
    for model, marker in (
        (models.OAuthMCPAuthorizationCode, "is_used"),
        (models.OAuthMCPAccessToken, "is_revoked"),
        (models.OAuthMCPRefreshToken, "is_revoked"),
    ):
        rows = db.execute(
            update(model)
            .where(
                model.account_id == account_id,
                model.user_id == user_id,
                getattr(model, marker).is_(False),
            )
            .values(**{marker: True})
            .returning(model.id)
        ).all()
        counts[model.__tablename__] = len(rows)
    runtime_ids: set[UUID] = set()
    for key in keys:
        if key.id in machine_keys:
            continue
        if key.is_active:
            key.is_active = False
            counts["personal_keys"] += 1
        context = key.context_data if isinstance(key.context_data, dict) else {}
        if context.get("runtime_session_id"):
            try:
                runtime_ids.add(UUID(str(context["runtime_session_id"])))
            except (ValueError, TypeError, AttributeError):
                pass
    if runtime_ids:
        counts["runtime_sessions"] = len(
            db.execute(
                update(models.RuntimeSession)
                .where(
                    models.RuntimeSession.account_id == account_id,
                    models.RuntimeSession.id.in_(runtime_ids),
                    models.RuntimeSession.ended_at.is_(None),
                )
                .values(ended_at=now, last_activity_at=now, turn_state="ended")
                .returning(models.RuntimeSession.id)
            ).all()
        )
    counts["team_memberships"] = len(
        db.execute(
            delete(models.TeamMembership)
            .where(
                models.TeamMembership.user_id == user_id,
                models.TeamMembership.team_id.in_(
                    select(models.Team.id).where(models.Team.account_id == account_id)
                ),
            )
            .returning(models.TeamMembership.id)
        ).all()
    )
    runners = list(
        db.scalars(
            select(models.FlowRunner.id).where(
                models.FlowRunner.account_id == account_id,
                models.FlowRunner.registered_by_user_id == user_id,
            )
        )
    )
    for runner_id in runners:
        stopped = crud_flow_runner.delete_runner(
            db,
            account_id=account_id,
            runner_id=runner_id,
            force=True,
            reason=reason,
            commit=False,
        )
        if stopped is not None:
            counts["runners"] += 1
            counts["stopped_executions"] += len(stopped)
    db.flush()
    return OffboardingResult(
        user_id=user_id, counts=counts, preserved_machine_key_ids=preserved
    )


def lock_for_reactivation(
    db: Session, *, account_id: UUID, user_id: UUID
) -> models.User:
    """Lock the same lifecycle boundary and refuse disabled canonical identities."""
    account = db.scalar(
        select(models.Account).where(models.Account.id == account_id).with_for_update()
    )
    person_id = db.scalar(
        select(models.User.person_id).where(
            models.User.id == user_id, models.User.account_id == account_id
        )
    )
    if account is None or not account.is_active or person_id is None:
        raise ValueError("Membership is unavailable")
    person = db.scalar(
        select(models.Person)
        .where(models.Person.id == person_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    user = db.scalar(
        select(models.User)
        .where(models.User.id == user_id, models.User.account_id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if person is None or person.identity_status != "active" or user is None:
        raise ValueError("Membership is unavailable")
    return user


def require_active_membership(
    db: Session, *, account_id: UUID, user_id: UUID
) -> models.User:
    """Serialize human credential issuance with membership revocation.

    Takes the account lock that offboarding takes first, then the membership
    row. Keep these locks until the new credential is committed: an
    authentication result obtained before offboarding is not issuance
    authority. Person suspension is enforced at authentication, not here.

    Raises:
        ValueError: The account is missing or inactive, or the membership is
            missing, belongs to another account, or is inactive.
    """
    account = db.scalar(
        select(models.Account).where(models.Account.id == account_id).with_for_update()
    )
    user = db.scalar(
        select(models.User)
        .where(models.User.id == user_id, models.User.account_id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account is None or not account.is_active or user is None:
        raise ValueError("Membership is unavailable")
    if not user.is_active:
        raise ValueError("Inactive membership cannot issue credentials")
    return user


@dataclass(frozen=True)
class MachineCredentialSummary:
    """One account-owned machine credential that a member created."""

    api_key_id: UUID
    name: str
    kind: str
    principal_id: UUID
    principal_name: str
    is_active: bool
    created_at: datetime | None
    last_used_at: datetime | None


def list_member_machine_credentials(
    db: Session, *, account_id: UUID, user_id: UUID
) -> list[MachineCredentialSummary]:
    """List account-owned machine credentials created by one membership.

    Offboarding keeps these credentials working because the account owns
    them. Admins use this list to review or rotate them after a member leaves.

    Args:
        db: Database session.
        account_id: Account that owns the credentials.
        user_id: Membership that created them.

    Returns:
        CI identity and managed-agent credentials, oldest first.
    """
    ci_rows = db.execute(
        select(models.ApiKey, models.CiPrincipal.id, models.CiPrincipal.name)
        .join(
            models.CiPrincipal, models.ApiKey.ci_principal_id == models.CiPrincipal.id
        )
        .where(
            models.ApiKey.account_id == account_id,
            models.CiPrincipal.account_id == account_id,
            models.ApiKey.user_id == user_id,
        )
    ).all()
    agent_rows = db.execute(
        select(models.ApiKey, models.ManagedAgent.id, models.ManagedAgent.display_name)
        .join(
            models.ManagedAgentCredential,
            models.ManagedAgentCredential.api_key_id == models.ApiKey.id,
        )
        .join(
            models.ManagedAgent,
            models.ManagedAgent.id == models.ManagedAgentCredential.managed_agent_id,
        )
        .where(
            models.ApiKey.account_id == account_id,
            models.ManagedAgentCredential.account_id == account_id,
            models.ManagedAgent.account_id == account_id,
            or_(
                models.ApiKey.user_id == user_id,
                models.ManagedAgentCredential.created_by_user_id == user_id,
            ),
        )
    ).all()
    summaries: dict[UUID, MachineCredentialSummary] = {}
    for kind, rows in (("ci_principal", ci_rows), ("managed_agent", agent_rows)):
        for key, principal_id, principal_name in rows:
            summaries.setdefault(
                key.id,
                MachineCredentialSummary(
                    api_key_id=key.id,
                    name=key.name,
                    kind=kind,
                    principal_id=principal_id,
                    principal_name=principal_name,
                    is_active=key.is_active,
                    created_at=key.created_at,
                    last_used_at=key.last_used_at,
                ),
            )
    return sorted(
        summaries.values(),
        key=lambda item: (
            item.created_at.isoformat() if item.created_at else "",
            str(item.api_key_id),
        ),
    )
