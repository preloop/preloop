"""Transactional restricted CI lifecycle and fresh authorization lookups."""

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, TypedDict
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.plugins.ci_authorization import can_administer_ci
from preloop.schemas.ci_principal import CiAction, CiGrant

from .api_key import CRUDApiKey
from .audit_log import CRUDAuditLog


class CiRepositoryBinding(TypedDict):
    """Provider fields that determine the administrator-approved clone target."""

    slug: str
    tracker_type: str
    tracker_url: Optional[str]
    organization_id: str


@dataclass(frozen=True)
class CiAuthorizationContext:
    """Machine identity without a human user or ambient owner permissions."""

    account_id: UUID
    principal_id: UUID
    key_id: UUID
    project_id: UUID
    flow_id: UUID
    tracker_id: UUID
    repository_identifier: str
    repository_slug: str
    tracker_type: str
    tracker_url: Optional[str]
    actions: frozenset[CiAction]
    credential_version: int = 1


class CRUDCiPrincipal:
    """CI-only entry points. There is deliberately no generic delete/update."""

    def get(
        self, db: Session, *, account_id: UUID, principal_id: UUID, lock: bool = False
    ) -> Optional[models.CiPrincipal]:
        """Read current account-local state, bypassing identity-map snapshots."""
        query = (
            db.query(models.CiPrincipal)
            .populate_existing()
            .filter(
                models.CiPrincipal.account_id == account_id,
                models.CiPrincipal.id == principal_id,
            )
        )
        if lock:
            query = query.with_for_update()
        return query.first()

    def _binding(
        self, db: Session, *, account_id: UUID, grant: CiGrant
    ) -> tuple[UUID, str, CiRepositoryBinding]:
        """Require local resources and an explicit single-repository binding."""
        row = (
            db.query(models.Project, models.Organization, models.Tracker)
            .join(
                models.Organization,
                models.Project.organization_id == models.Organization.id,
            )
            .join(models.Tracker, models.Organization.tracker_id == models.Tracker.id)
            .populate_existing()
            .filter(
                models.Project.id == grant.project_id,
                models.Tracker.account_id == account_id,
                models.Project.is_active.is_(True),
                models.Organization.is_active.is_(True),
                models.Tracker.is_active.is_(True),
            )
            .first()
        )
        flow = (
            db.query(models.Flow)
            .populate_existing()
            .filter(
                models.Flow.id == grant.flow_id,
                models.Flow.account_id == account_id,
                models.Flow.is_enabled.is_(True),
            )
            .first()
        )
        if row is None or flow is None:
            raise ValueError("CI binding requires active account-local resources")
        project, _organization, tracker = row
        configuration = flow.git_clone_config
        if (
            not isinstance(configuration, dict)
            or configuration.get("enabled") is not True
        ):
            raise ValueError("CI flow requires an enabled repository binding")
        repositories = configuration.get("repositories", [])
        if (
            flow.trigger_project_ids != [str(project.id)]
            or flow.runner_pool != "server"
            or not isinstance(repositories, list)
            or len(repositories) != 1
            or not isinstance(repositories[0], dict)
            or repositories[0].get("project_id") != str(project.id)
            or repositories[0].get("tracker_id") != str(tracker.id)
            or repositories[0].get("repository_url")
        ):
            raise ValueError(
                "CI flow must bind exactly the selected project repository"
            )
        if not project.slug or not project.identifier:
            raise ValueError(
                "CI repository requires a provider identifier and clone slug"
            )
        return (
            tracker.id,
            project.identifier,
            {
                "slug": project.slug,
                "tracker_type": tracker.tracker_type,
                "tracker_url": tracker.url,
                "organization_id": str(project.organization_id),
            },
        )

    def _authorize(
        self, db: Session, *, actor: models.User, operation: str, grant: CiGrant
    ) -> None:
        """Require a current human and edition-specific resource authority."""
        key = getattr(actor, "_auth_api_key", None)
        if key is not None and key.requires_machine_authorization:
            raise PermissionError("CI credentials cannot administer identities")
        human = (
            db.query(models.User)
            .populate_existing()
            .filter(models.User.id == actor.id)
            .first()
        )
        account = (
            db.query(models.Account)
            .populate_existing()
            .filter(models.Account.id == actor.account_id)
            .first()
        )
        if (
            human is None
            or not human.is_active
            or account is None
            or not account.is_active
            or human.account_id != account.id
            or not can_administer_ci(
                human, operation, grant, is_owner=account.primary_user_id == human.id
            )
        ):
            raise PermissionError("CI administration denied")
        if operation not in {"disable", "revoke", "narrow"}:
            self._binding(db, account_id=account.id, grant=grant)

    def _audit(
        self,
        db: Session,
        *,
        actor: models.User,
        principal: models.CiPrincipal,
        operation: str,
        key_id: Optional[UUID] = None,
    ) -> None:
        """Write safe attribution atomically with lifecycle changes."""
        CRUDAuditLog(models.AuditLog).log_action(
            db,
            account_id=principal.account_id,
            user_id=actor.id,
            action=f"ci_identity_{operation}",
            resource_type="ci_principal",
            resource_id=str(principal.id),
            status="success",
            commit=False,
            details={
                "principal_id": str(principal.id),
                "key_id": str(key_id) if key_id else None,
                "grant": principal.grant,
            },
        )

    def _issue_key(
        self,
        db: Session,
        *,
        actor: models.User,
        principal: models.CiPrincipal,
        actions: tuple[CiAction, ...],
        expires_at: Optional[datetime],
    ) -> tuple[models.ApiKey, str]:
        """Disclose a high-entropy token once; persist only its fingerprint."""
        grant = CiGrant.model_validate(principal.grant)
        if (
            not actions
            or len(set(actions)) != len(actions)
            or not set(actions) <= set(grant.actions)
        ):
            raise ValueError(
                "Key actions must be a nonempty subset of the principal grant"
            )
        if expires_at is not None:
            expiry = (
                expires_at.replace(tzinfo=timezone.utc)
                if expires_at.tzinfo is None
                else expires_at
            )
            if expiry <= datetime.now(timezone.utc):
                raise ValueError("CI key expiry must be in the future")
        token = f"ci_{secrets.token_urlsafe(32)}"
        api_keys = CRUDApiKey(models.ApiKey)
        key = models.ApiKey(
            name=f"CI {principal.id} {secrets.token_hex(4)}",
            account_id=principal.account_id,
            user_id=actor.id,
            key=None,
            key_hash=api_keys.build_key_hash(token),
            key_prefix=api_keys.build_key_prefix(token),
            is_active=True,
            expires_at=expires_at,
            scopes=[],
            credential_type="restricted_ci",
            credential_version=1,
            ci_principal_id=principal.id,
            ci_actions=[action.value for action in actions],
        )
        db.add(key)
        db.flush()
        return key, token

    def provision(
        self,
        db: Session,
        *,
        actor: models.User,
        name: str,
        grant: CiGrant,
        key_actions: Optional[tuple[CiAction, ...]] = None,
        expires_at: Optional[datetime] = None,
    ) -> tuple[models.CiPrincipal, models.ApiKey, str]:
        """Internal provisioning only; no usable public setup during rollout."""
        grant = CiGrant.model_validate(grant)
        if not name.strip() or len(name) > 100:
            raise ValueError("CI identity name must contain 1 to 100 characters")
        self._authorize(db, actor=actor, operation="provision", grant=grant)
        actions = grant.actions if key_actions is None else key_actions
        if not actions or not set(actions) <= set(grant.actions):
            raise ValueError("Invalid key action ceiling")
        tracker_id, identifier, repository_binding = self._binding(
            db, account_id=actor.account_id, grant=grant
        )
        try:
            principal = models.CiPrincipal(
                name=name,
                account_id=actor.account_id,
                administered_by_user_id=actor.id,
                is_active=True,
                credential_version=1,
                project_id=grant.project_id,
                flow_id=grant.flow_id,
                tracker_id=tracker_id,
                repository_identifier=identifier,
                repository_binding=repository_binding,
                grant=grant.model_dump(mode="json"),
            )
            db.add(principal)
            db.flush()
            key, token = self._issue_key(
                db,
                actor=actor,
                principal=principal,
                actions=actions,
                expires_at=expires_at,
            )
            self._audit(
                db,
                actor=actor,
                principal=principal,
                operation="provision",
                key_id=key.id,
            )
            db.commit()
            return principal, key, token
        except Exception:
            db.rollback()
            raise

    def change(
        self,
        db: Session,
        *,
        actor: models.User,
        principal_id: UUID,
        grant: Optional[CiGrant] = None,
        enabled: Optional[bool] = None,
    ) -> models.CiPrincipal:
        """Change actions or enabled state; resource identity remains immutable."""
        principal = self.get(
            db, account_id=actor.account_id, principal_id=principal_id, lock=True
        )
        if principal is None:
            raise ValueError("CI identity not found")
        current = CiGrant.model_validate(principal.grant)
        if grant is not None:
            grant = CiGrant.model_validate(grant)
        operation = "change"
        if enabled is False and grant is None:
            operation = "disable"
        elif (
            enabled is not True
            and grant is not None
            and set(grant.actions) <= set(current.actions)
        ):
            operation = "narrow"
        self._authorize(db, actor=actor, operation=operation, grant=grant or current)
        if grant is not None and (
            grant.project_id != principal.project_id
            or grant.flow_id != principal.flow_id
        ):
            raise ValueError("CI binding is immutable; provision a new identity")
        try:
            if grant is not None:
                principal.grant = grant.model_dump(mode="json")
            if enabled is not None:
                principal.is_active = enabled
            operation = "disable" if enabled is False else "change"
            self._audit(db, actor=actor, principal=principal, operation=operation)
            db.commit()
            return principal
        except Exception:
            db.rollback()
            raise

    def rotate(
        self,
        db: Session,
        *,
        actor: models.User,
        principal_id: UUID,
        key_id: UUID,
        expires_at: Optional[datetime] = None,
    ) -> tuple[models.ApiKey, str]:
        """Replace one key atomically without altering principal-owned objects."""
        principal = self.get(
            db, account_id=actor.account_id, principal_id=principal_id, lock=True
        )
        if principal is None or not principal.is_active:
            raise ValueError("Active CI identity required")
        grant = CiGrant.model_validate(principal.grant)
        self._authorize(db, actor=actor, operation="rotate", grant=grant)
        old = (
            db.query(models.ApiKey)
            .populate_existing()
            .filter(
                models.ApiKey.id == key_id,
                models.ApiKey.ci_principal_id == principal.id,
                models.ApiKey.account_id == actor.account_id,
            )
            .with_for_update()
            .first()
        )
        if (
            old is None
            or not old.is_valid()
            or old.credential_type != "restricted_ci"
            or old.credential_version != 1
        ):
            raise ValueError("Valid CI key required")
        if old.scopes != [] or old.key is not None or not old.key_hash:
            raise ValueError("Valid CI key required")
        try:
            old_actions = (
                tuple(CiAction(action) for action in old.ci_actions)
                if isinstance(old.ci_actions, list)
                else ()
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Valid CI key required") from exc
        if not old_actions or len(set(old_actions)) != len(old_actions):
            raise ValueError("Valid CI key required")
        ceiling = tuple(action for action in grant.actions if action in old_actions)
        try:
            key, token = self._issue_key(
                db,
                actor=actor,
                principal=principal,
                actions=ceiling,
                expires_at=expires_at,
            )
            old.is_active = False
            self._audit(
                db, actor=actor, principal=principal, operation="rotate", key_id=key.id
            )
            self._audit(
                db, actor=actor, principal=principal, operation="revoke", key_id=old.id
            )
            db.commit()
            return key, token
        except Exception:
            db.rollback()
            raise

    def revoke_key(
        self, db: Session, *, actor: models.User, principal_id: UUID, key_id: UUID
    ) -> None:
        """Revocation changes only key authentication, never resource ownership."""
        principal = self.get(
            db, account_id=actor.account_id, principal_id=principal_id, lock=True
        )
        if principal is None:
            raise ValueError("CI identity not found")
        self._authorize(
            db,
            actor=actor,
            operation="revoke",
            grant=CiGrant.model_validate(principal.grant),
        )
        key = (
            db.query(models.ApiKey)
            .filter(
                models.ApiKey.id == key_id,
                models.ApiKey.ci_principal_id == principal.id,
                models.ApiKey.account_id == actor.account_id,
            )
            .first()
        )
        if key is None:
            raise ValueError("CI key not found")
        try:
            key.is_active = False
            self._audit(
                db, actor=actor, principal=principal, operation="revoke", key_id=key.id
            )
            db.commit()
        except Exception:
            db.rollback()
            raise

    def authenticate(
        self, db: Session, *, token: str
    ) -> Optional[CiAuthorizationContext]:
        """Intersect fresh account, principal, key, binding and typed grant state."""
        # No optional audit/off scope mode applies to this credential type.
        key = CRUDApiKey(models.ApiKey).get_by_key(
            db, key=token, include_restricted=True
        )
        if key is None:
            return None
        db.refresh(key)
        if (
            key.credential_type != "restricted_ci"
            or key.credential_version != 1
            or key.ci_principal_id is None
            or key.scopes != []
            or not key.is_valid()
            or key.key is not None
            or not key.key_hash
        ):
            return None
        principal = self.get(
            db,
            account_id=key.account_id,
            principal_id=key.ci_principal_id,
        )
        account = (
            db.query(models.Account)
            .populate_existing()
            .filter(models.Account.id == key.account_id)
            .first()
        )
        if (
            principal is None
            or not principal.is_active
            or principal.credential_version != 1
            or account is None
            or not account.is_active
        ):
            return None
        try:
            grant = CiGrant.model_validate(principal.grant)
            if (
                grant.project_id != principal.project_id
                or grant.flow_id != principal.flow_id
            ):
                return None
            actions = (
                tuple(CiAction(action) for action in key.ci_actions)
                if isinstance(key.ci_actions, list)
                else ()
            )
            if not actions or len(set(actions)) != len(actions):
                return None
            effective = frozenset(actions) & frozenset(grant.actions)
            if not effective:
                return None
            binding = self._binding(db, account_id=key.account_id, grant=grant)
            if binding != (
                principal.tracker_id,
                principal.repository_identifier,
                principal.repository_binding,
            ):
                return None
        except (ValidationError, ValueError, TypeError):
            return None
        return CiAuthorizationContext(
            account_id=key.account_id,
            principal_id=principal.id,
            key_id=key.id,
            project_id=grant.project_id,
            flow_id=grant.flow_id,
            tracker_id=principal.tracker_id,
            repository_identifier=principal.repository_identifier,
            repository_slug=binding[2]["slug"],
            tracker_type=binding[2]["tracker_type"],
            tracker_url=binding[2]["tracker_url"],
            actions=effective,
        )


crud_ci_principal = CRUDCiPrincipal()
