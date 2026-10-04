"""Fresh human-only setup and recovery for stable restricted CI identities."""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.audit_log import CRUDAuditLog
from preloop.models.crud.ci_principal import CiAuthorizationContext, crud_ci_principal
from preloop.models.crud.ci_subscription import crud_ci_subscription
from preloop.schemas.ci_administration import (
    CiAdminCapabilities,
    CiAdminCreate,
    CiAdminIdentityRead,
    CiAdminIssue,
    CiAdminIssued,
    CiAdminKeyIssued,
    CiAdminKeyRead,
    CiAdminRotate,
    CiAdminSubscription,
    CiAdminUpdate,
)
from preloop.schemas.ci_principal import CiAction, CiGrant
from preloop.schemas.ci_subscription import CiSubscriptionCreate

logger = logging.getLogger(__name__)


class CRUDCiAdministration:
    """No machine credential can administer itself or inherit its human's roles."""

    def available(self, db: Session) -> bool:
        """Check complete operation coverage and migrated columns before issuance."""
        from preloop.api.middleware import ci_auth

        if len(ci_auth.CI_ROUTE_POLICIES) != 10 or set(
            ci_auth.CI_ROUTE_POLICIES.values()
        ) != set(CiAction):
            return False
        try:
            db.query(models.CiPrincipal.grant).limit(0).all()
            db.query(models.ApiKey.ci_principal_id, models.ApiKey.ci_actions).limit(
                0
            ).all()
            db.query(models.FlowExecution.ci_review_binding).limit(0).all()
            db.query(models.WebhookEndpoint.ci_subscription_binding).limit(0).all()
            return True
        except SQLAlchemyError:
            db.rollback()
            return False

    def require_available(self, db: Session) -> None:
        """Reject issuance while any core enforcement contract is unavailable."""
        if not self.available(db):
            raise RuntimeError("Restricted CI setup is not available")

    def _require_original_binding(
        self, db: Session, *, principal: models.CiPrincipal, grant: CiGrant
    ) -> None:
        """Do not issue unusable keys after immutable repository identity drift."""
        binding = crud_ci_principal._binding(
            db, account_id=principal.account_id, grant=grant
        )
        if binding != (
            principal.tracker_id,
            principal.repository_identifier,
            principal.repository_binding,
        ):
            raise ValueError("CI binding changed; provision a new identity")

    def _authorize(
        self,
        db: Session,
        *,
        actor: models.User,
        operation: str,
        grant: CiGrant | None,
        principal_id: UUID | None = None,
        key_id: UUID | None = None,
    ) -> None:
        try:
            crud_ci_principal._authorize(
                db, actor=actor, operation=operation, grant=grant
            )
        except PermissionError:
            self._denied(
                db,
                actor=actor,
                operation=operation,
                grant=grant,
                principal_id=principal_id,
                key_id=key_id,
            )
            raise

    def _denied(
        self,
        db: Session,
        *,
        actor: models.User,
        operation: str,
        grant: CiGrant | None = None,
        principal_id: UUID | None = None,
        key_id: UUID | None = None,
    ) -> None:
        """Audit only typed attempted IDs, without looking up foreign resources."""

        try:
            CRUDAuditLog(models.AuditLog).log_action(
                db,
                account_id=actor.account_id,
                user_id=actor.id,
                action="ci_identity_administration_denied",
                resource_type="ci_principal",
                resource_id=str(principal_id) if principal_id else None,
                status="denied",
                details={
                    "operation": operation,
                    "principal_id": str(principal_id) if principal_id else None,
                    "key_id": str(key_id) if key_id else None,
                    "project_id": str(grant.project_id) if grant else None,
                    "flow_id": str(grant.flow_id) if grant else None,
                    "actions": [action.value for action in grant.actions]
                    if grant
                    else [],
                },
            )
        except SQLAlchemyError:
            db.rollback()
            logger.warning("CI administration denial audit unavailable")

    def capabilities(self, db: Session, *, actor: models.User) -> CiAdminCapabilities:
        available = self.available(db)
        flags = {}
        for name, operation in (
            ("can_view", "capabilities"),
            ("can_manage", "manage_capabilities"),
        ):
            try:
                crud_ci_principal._authorize(
                    db, actor=actor, operation=operation, grant=None
                )
                flags[name] = True
            except PermissionError:
                flags[name] = False
        return CiAdminCapabilities(
            available=available,
            supported_actions=list(CiAction) if available else [],
            can_view=flags["can_view"],
            can_manage=flags["can_manage"],
        )

    def preview(
        self, db: Session, *, actor: models.User, grant: CiGrant
    ) -> dict[str, Any]:
        self.require_available(db)
        self._authorize(db, actor=actor, operation="provision", grant=grant)
        tracker_id, identifier, repository = crud_ci_principal._binding(
            db, account_id=actor.account_id, grant=grant
        )
        if repository["tracker_type"] not in {"github", "gitlab"}:
            raise ValueError("Restricted CI supports GitHub and GitLab repositories")
        return {
            "grant": grant.model_dump(mode="json"),
            "tracker_id": str(tracker_id),
            "repository_identifier": identifier,
            "repository_slug": repository["slug"],
            "tracker_type": repository["tracker_type"],
            "runner_pool": "server",
        }

    def _principal(
        self,
        db: Session,
        *,
        actor: models.User,
        principal_id: UUID,
        operation: str,
        key_id: UUID | None = None,
    ) -> models.CiPrincipal:
        row = crud_ci_principal.get(
            db, account_id=actor.account_id, principal_id=principal_id
        )
        if row is None:
            self._denied(
                db,
                actor=actor,
                operation=operation,
                principal_id=principal_id,
                key_id=key_id,
            )
            raise LookupError("CI identity not found")
        return row

    def project_metadata(
        self, db: Session, *, principal: models.CiPrincipal
    ) -> CiAdminIdentityRead:
        keys = (
            db.query(models.ApiKey)
            .populate_existing()
            .filter(
                models.ApiKey.account_id == principal.account_id,
                models.ApiKey.ci_principal_id == principal.id,
                models.ApiKey.credential_type == "restricted_ci",
                models.ApiKey.credential_version == 1,
            )
            .order_by(models.ApiKey.created_at.desc())
            .all()
        )
        return CiAdminIdentityRead(
            id=principal.id,
            name=principal.name,
            is_active=principal.is_active,
            credential_version=principal.credential_version,
            grant=CiGrant.model_validate(principal.grant),
            repository_identifier=principal.repository_identifier,
            repository_slug=principal.repository_binding["slug"],
            tracker_type=principal.repository_binding["tracker_type"],
            keys=[
                CiAdminKeyRead(
                    id=key.id,
                    actions=[
                        action
                        for action in (key.ci_actions or [])
                        if isinstance(action, str)
                        and action in {value.value for value in CiAction}
                    ],
                    is_active=key.is_active,
                    expires_at=key.expires_at,
                    created_at=key.created_at,
                    last_used_at=key.last_used_at,
                )
                for key in keys
            ],
        )

    def get(
        self, db: Session, *, actor: models.User, principal_id: UUID
    ) -> CiAdminIdentityRead:
        principal = self._principal(
            db, actor=actor, principal_id=principal_id, operation="view"
        )
        self._authorize(
            db,
            actor=actor,
            operation="view",
            grant=CiGrant.model_validate(principal.grant),
            principal_id=principal.id,
        )
        return self.project_metadata(db, principal=principal)

    def list(self, db: Session, *, actor: models.User) -> list[CiAdminIdentityRead]:
        self._authorize(db, actor=actor, operation="capabilities", grant=None)
        rows = (
            db.query(models.CiPrincipal)
            .populate_existing()
            .filter(models.CiPrincipal.account_id == actor.account_id)
            .order_by(models.CiPrincipal.created_at.desc())
            .all()
        )
        result = []
        for principal in rows:
            try:
                crud_ci_principal._authorize(
                    db,
                    actor=actor,
                    operation="view",
                    grant=CiGrant.model_validate(principal.grant),
                )
            except (PermissionError, ValueError):
                continue
            result.append(self.project_metadata(db, principal=principal))
        return result

    def create(
        self, db: Session, *, actor: models.User, payload: CiAdminCreate
    ) -> CiAdminIssued:
        self.preview(db, actor=actor, grant=payload.grant)
        try:
            principal, key, token = crud_ci_principal.provision(
                db,
                actor=actor,
                name=payload.name,
                grant=payload.grant,
                expires_at=payload.expires_at,
                commit=False,
            )
            issued = CiAdminIssued(
                identity=self.project_metadata(db, principal=principal),
                key_id=key.id,
                token=token,
            )
            db.commit()
            return issued
        except Exception:
            db.rollback()
            raise

    def change(
        self,
        db: Session,
        *,
        actor: models.User,
        principal_id: UUID,
        payload: CiAdminUpdate,
    ) -> models.CiPrincipal:
        principal = self._principal(
            db, actor=actor, principal_id=principal_id, operation="change"
        )
        current = CiGrant.model_validate(principal.grant)
        operation = "change"
        if payload.enabled is False and payload.grant is None:
            operation = "disable"
        elif (
            payload.enabled is not True
            and payload.grant is not None
            and set(payload.grant.actions) <= set(current.actions)
        ):
            operation = "narrow"
        if operation == "change":
            self.require_available(db)
        self._authorize(
            db,
            actor=actor,
            operation=operation,
            grant=payload.grant or current,
            principal_id=principal.id,
        )
        return crud_ci_principal.change(
            db,
            actor=actor,
            principal_id=principal.id,
            grant=payload.grant,
            enabled=payload.enabled,
        )

    def issue(
        self,
        db: Session,
        *,
        actor: models.User,
        principal_id: UUID,
        payload: CiAdminIssue,
    ) -> CiAdminKeyIssued:
        self.require_available(db)
        principal = self._principal(
            db, actor=actor, principal_id=principal_id, operation="issue"
        )
        grant = CiGrant.model_validate(principal.grant)
        self._authorize(
            db, actor=actor, operation="issue", grant=grant, principal_id=principal.id
        )
        if not principal.is_active:
            raise ValueError("Enable the identity before issuing a credential")
        self._require_original_binding(db, principal=principal, grant=grant)
        try:
            key, token = crud_ci_principal._issue_key(
                db,
                actor=actor,
                principal=principal,
                actions=payload.actions
                if payload.actions is not None
                else grant.actions,
                expires_at=payload.expires_at,
            )
            crud_ci_principal._audit(
                db, actor=actor, principal=principal, operation="issue", key_id=key.id
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        return CiAdminKeyIssued(principal_id=principal.id, key_id=key.id, token=token)

    def rotate(
        self,
        db: Session,
        *,
        actor: models.User,
        principal_id: UUID,
        key_id: UUID,
        payload: CiAdminRotate,
    ) -> CiAdminKeyIssued:
        self.require_available(db)
        principal = self._principal(
            db,
            actor=actor,
            principal_id=principal_id,
            operation="rotate",
            key_id=key_id,
        )
        self._authorize(
            db,
            actor=actor,
            operation="rotate",
            grant=CiGrant.model_validate(principal.grant),
            principal_id=principal.id,
            key_id=key_id,
        )
        self._require_original_binding(
            db, principal=principal, grant=CiGrant.model_validate(principal.grant)
        )
        try:
            key, token = crud_ci_principal.rotate(
                db,
                actor=actor,
                principal_id=principal.id,
                key_id=key_id,
                expires_at=payload.expires_at,
            )
        except (PermissionError, ValueError):
            self._denied(
                db,
                actor=actor,
                operation="rotate",
                principal_id=principal.id,
                key_id=key_id,
            )
            raise
        return CiAdminKeyIssued(principal_id=principal.id, key_id=key.id, token=token)

    def revoke(
        self, db: Session, *, actor: models.User, principal_id: UUID, key_id: UUID
    ) -> None:
        principal = self._principal(
            db,
            actor=actor,
            principal_id=principal_id,
            operation="revoke",
            key_id=key_id,
        )
        self._authorize(
            db,
            actor=actor,
            operation="revoke",
            grant=CiGrant.model_validate(principal.grant),
            principal_id=principal.id,
            key_id=key_id,
        )
        try:
            crud_ci_principal.revoke_key(
                db, actor=actor, principal_id=principal.id, key_id=key_id
            )
        except (PermissionError, ValueError):
            self._denied(
                db,
                actor=actor,
                operation="revoke",
                principal_id=principal.id,
                key_id=key_id,
            )
            raise

    def create_subscription(
        self,
        db: Session,
        *,
        actor: models.User,
        principal_id: UUID,
        payload: CiAdminSubscription,
    ) -> tuple[models.WebhookEndpoint, str]:
        self.require_available(db)
        principal = self._principal(
            db,
            actor=actor,
            principal_id=principal_id,
            operation="create_subscription",
            key_id=payload.key_id,
        )
        grant = CiGrant.model_validate(principal.grant)
        self._authorize(
            db,
            actor=actor,
            operation="create_subscription",
            grant=grant,
            principal_id=principal.id,
            key_id=payload.key_id,
        )
        key = (
            db.query(models.ApiKey)
            .populate_existing()
            .filter(
                models.ApiKey.id == payload.key_id,
                models.ApiKey.account_id == actor.account_id,
                models.ApiKey.ci_principal_id == principal.id,
                models.ApiKey.credential_type == "restricted_ci",
                models.ApiKey.credential_version == 1,
            )
            .first()
        )
        if key is None:
            self._denied(
                db,
                actor=actor,
                operation="create_subscription",
                grant=grant,
                principal_id=principal.id,
                key_id=payload.key_id,
            )
            raise LookupError("CI credential audit anchor not found")
        binding = principal.repository_binding
        context = CiAuthorizationContext(
            account_id=principal.account_id,
            principal_id=principal.id,
            key_id=key.id,
            project_id=principal.project_id,
            flow_id=principal.flow_id,
            tracker_id=principal.tracker_id,
            repository_identifier=principal.repository_identifier,
            repository_slug=binding["slug"],
            tracker_type=binding["tracker_type"],
            tracker_url=binding["tracker_url"],
            actions=frozenset(grant.actions),
        )
        try:
            fresh = crud_ci_principal.authorize_principal(
                db, context=context, action=CiAction.CREATE_SUBSCRIPTION
            )
        except PermissionError:
            self._denied(
                db,
                actor=actor,
                operation="create_subscription",
                grant=grant,
                principal_id=principal.id,
                key_id=payload.key_id,
            )
            raise
        receiver = CiSubscriptionCreate.model_validate(
            payload.model_dump(exclude={"key_id"})
        )
        endpoint, secret = crud_ci_subscription._persist(
            db, context=fresh, payload=receiver, commit=False
        )
        crud_ci_principal._audit(
            db,
            actor=actor,
            principal=principal,
            operation="subscription_create",
            key_id=key.id,
        )
        db.commit()
        return endpoint, secret


crud_ci_administration = CRUDCiAdministration()
