"""Fresh human-only setup and recovery for stable restricted CI identities."""

import logging
from collections.abc import Sequence
from typing import Any, Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.audit_log import CRUDAuditLog
from preloop.models.crud.ci_principal import (
    CiAuthorizationContext,
    crud_ci_principal,
    stored_binding,
)
from preloop.models.crud.ci_subscription import crud_ci_subscription
from preloop.schemas.ci_administration import (
    CiAdminCapabilities,
    CiAdminCreate,
    CiAdminDispatchSubscription,
    CiAdminDispatchToken,
    CiAdminIdentityRead,
    CiAdminIssue,
    CiAdminIssued,
    CiAdminKeyIssued,
    CiAdminKeyRead,
    CiAdminRotate,
    CiAdminSubscription,
    CiAdminUpdate,
    CiSharedFlowProject,
    CiSharedFlowRead,
    CiSharedFlowUpdate,
)
from preloop.schemas.ci_principal import CiAction, CiGrant
from preloop.schemas.ci_subscription import CiSubscriptionCreate

logger = logging.getLogger(__name__)


def _binding_mode(repository_binding: Any) -> Literal["dedicated", "shared"]:
    """Name the provisioning-time binding mode of a repository snapshot."""
    shared = (
        isinstance(repository_binding, dict)
        and repository_binding.get("ci_project_selection") is True
    )
    return "shared" if shared else "dedicated"


# The machine ceiling includes the explicitly granted own-execution evidence route.
# Changing enforcement coverage requires explicit contract/rollout review before
# issuance resumes; extra routes must not silently broaden a deployed ceiling.
_EXPECTED_V1_MACHINE_ROUTE_COUNT = 13


class CiSharedFlowError(ValueError):
    """A refused allowlist change whose message is safe to return.

    The messages are fixed sentences. They never quote a project, flow or
    tracker, so a refusal cannot confirm that a resource of another account
    exists.
    """


SHARED_FLOW_NOT_READ_ONLY = (
    "Only an enabled read-only review flow (git_clone_config.publication_mode: "
    "read_only) can be shared between projects"
)
SHARED_FLOW_HAS_DEDICATED_IDENTITIES = (
    "This flow is dedicated and still has CI identities bound to it. Retire "
    "them first: an identity's binding mode is fixed when it is provisioned"
)
SHARED_FLOW_PROJECT_REFUSED = (
    "Every project must be an active project of this account whose repository "
    "belongs to a GitHub App tracker"
)
SHARED_FLOW_CONTRACT_REFUSED = (
    "The flow does not satisfy the read-only review contract; see the "
    "restricted CI guide"
)


class CRUDCiAdministration:
    """No machine credential can administer itself or inherit its human's roles."""

    def available(self, db: Session) -> bool:
        """Check complete operation coverage and migrated columns before issuance."""
        from preloop.api.middleware import ci_auth

        if len(ci_auth.CI_ROUTE_POLICIES) != _EXPECTED_V1_MACHINE_ROUTE_COUNT or set(
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
        if binding != stored_binding(principal):
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
        audit_operation: str | None = None,
    ) -> None:
        try:
            crud_ci_principal._authorize(
                db, actor=actor, operation=operation, grant=grant
            )
        except PermissionError:
            self._denied(
                db,
                actor=actor,
                operation=audit_operation or operation,
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
            "binding_mode": _binding_mode(repository),
            # ``server``, or the named private pool of a read-only review that
            # runs on a Codex host profile (checked by ``_binding``).
            "runner_pool": self._flow_runner_pool(db, actor=actor, grant=grant),
            # The flow is the actor's own (checked by ``_binding``); its name
            # lets the person previewing recognise it without the UUID.
            "flow_name": self._flow_name(db, actor=actor, grant=grant),
        }

    @staticmethod
    def _flow_runner_pool(db: Session, *, actor: models.User, grant: CiGrant) -> str:
        from preloop.models.crud import crud_flow

        flow = crud_flow.get(db, id=grant.flow_id, account_id=str(actor.account_id))
        pool = getattr(flow, "runner_pool", None)
        return pool if isinstance(pool, str) and pool else "server"

    @staticmethod
    def _flow_name(db: Session, *, actor: models.User, grant: CiGrant) -> str | None:
        from preloop.models.crud import crud_flow

        flow = crud_flow.get(db, id=grant.flow_id, account_id=str(actor.account_id))
        name = getattr(flow, "name", None)
        return name if isinstance(name, str) else None

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
            retired=principal.flow_id is None,
            credential_version=principal.credential_version,
            grant=CiGrant.model_validate(principal.grant),
            binding_mode=_binding_mode(principal.repository_binding),
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
            extend_keys=payload.extend_keys is True,
        )

    def retire(
        self, db: Session, *, actor: models.User, principal_id: UUID
    ) -> models.CiPrincipal:
        """Permanently disable an identity and release its flow binding."""
        principal = self._principal(
            db, actor=actor, principal_id=principal_id, operation="retire"
        )
        self._authorize(
            db,
            actor=actor,
            operation="disable",
            grant=CiGrant.model_validate(principal.grant),
            principal_id=principal.id,
            audit_operation="retire",
        )
        try:
            return crud_ci_principal.retire(db, actor=actor, principal_id=principal.id)
        except (PermissionError, ValueError):
            self._denied(db, actor=actor, operation="retire", principal_id=principal.id)
            raise

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
        """Human-authorize an HTTP completion callback for one principal."""
        return self._create_subscription(
            db, actor=actor, principal_id=principal_id, payload=payload
        )

    def create_dispatch_subscription(
        self,
        db: Session,
        *,
        actor: models.User,
        principal_id: UUID,
        payload: CiAdminDispatchSubscription,
    ) -> tuple[models.WebhookEndpoint, str]:
        """Human-authorize a GitHub repository_dispatch completion sink.

        The dispatch credential is accepted only here and on the human token
        replacement route: a restricted CI key can neither set nor read it.
        """
        receiver = CiAdminSubscription(
            key_id=payload.key_id,
            url="https://api.github.com",
            description=payload.description,
            active=payload.active,
        )
        return self._create_subscription(
            db,
            actor=actor,
            principal_id=principal_id,
            payload=receiver,
            dispatch=(payload.event_type, payload.token.get_secret_value()),
        )

    def replace_dispatch_token(
        self,
        db: Session,
        *,
        actor: models.User,
        principal_id: UUID,
        endpoint_id: UUID,
        payload: CiAdminDispatchToken,
    ) -> models.WebhookEndpoint:
        """Replace the stored dispatch credential of one dispatch subscription."""
        self.require_available(db)
        principal = self._principal(
            db,
            actor=actor,
            principal_id=principal_id,
            operation="create_subscription",
        )
        grant = CiGrant.model_validate(principal.grant)
        self._authorize(
            db,
            actor=actor,
            operation="create_subscription",
            grant=grant,
            principal_id=principal.id,
        )
        endpoint = (
            db.query(models.WebhookEndpoint)
            .populate_existing()
            .filter(
                models.WebhookEndpoint.id == endpoint_id,
                models.WebhookEndpoint.account_id == principal.account_id,
                models.WebhookEndpoint.ci_principal_id == principal.id,
            )
            .with_for_update()
            .first()
        )
        if endpoint is None:
            raise LookupError("CI subscription not found")
        crud_ci_subscription.replace_dispatch_token(
            db, endpoint=endpoint, token=payload.token.get_secret_value()
        )
        crud_ci_principal._audit(
            db,
            actor=actor,
            principal=principal,
            operation="dispatch_token_replace",
        )
        db.commit()
        db.refresh(endpoint)
        return endpoint

    def _create_subscription(
        self,
        db: Session,
        *,
        actor: models.User,
        principal_id: UUID,
        payload: CiAdminSubscription,
        dispatch: tuple[str, str] | None = None,
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
        if principal.flow_id is None:
            self._denied(
                db,
                actor=actor,
                operation="create_subscription",
                grant=grant,
                principal_id=principal.id,
                key_id=payload.key_id,
            )
            raise PermissionError("CI identity is retired")
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
            db, context=fresh, payload=receiver, commit=False, dispatch=dispatch
        )
        crud_ci_principal._audit(
            db,
            actor=actor,
            principal=principal,
            operation=(
                "subscription_create"
                if dispatch is None
                else "dispatch_subscription_create"
            ),
            key_id=key.id,
        )
        db.commit()
        return endpoint, secret

    def _account_projects(
        self, db: Session, *, account_id: Any, project_ids: Sequence[UUID]
    ) -> dict[str, tuple[models.Project, models.Tracker]]:
        """Load active projects of one account with their owning trackers.

        A project of another account, an inactive project and an unknown id
        are all simply absent from the result.
        """
        if not project_ids:
            return {}
        rows = (
            db.query(models.Project, models.Tracker)
            .join(
                models.Organization,
                models.Project.organization_id == models.Organization.id,
            )
            .join(models.Tracker, models.Organization.tracker_id == models.Tracker.id)
            .populate_existing()
            .filter(
                models.Project.id.in_(project_ids),
                models.Tracker.account_id == account_id,
                models.Project.is_active.is_(True),
                models.Organization.is_active.is_(True),
                models.Tracker.is_active.is_(True),
            )
            .all()
        )
        return {str(project.id): (project, tracker) for project, tracker in rows}

    def _shared_flow_read(self, db: Session, *, flow: models.Flow) -> CiSharedFlowRead:
        """Project one flow's allowlist for a human administrator."""
        from preloop.services.read_only_review import read_only_review

        stored = flow.git_clone_config
        config: dict[str, Any] = stored if isinstance(stored, dict) else {}
        repositories = config.get("repositories")
        listed: list[UUID] = []
        for repo in repositories if isinstance(repositories, list) else []:
            try:
                project_id = UUID(str(repo.get("project_id")))
            except (AttributeError, ValueError):
                continue
            if project_id not in listed:
                listed.append(project_id)
        known = self._account_projects(
            db, account_id=flow.account_id, project_ids=listed
        )
        counts = dict(
            db.query(models.CiPrincipal.project_id, func.count(models.CiPrincipal.id))
            .filter(
                models.CiPrincipal.account_id == flow.account_id,
                models.CiPrincipal.flow_id == flow.id,
                models.CiPrincipal.is_active.is_(True),
            )
            .group_by(models.CiPrincipal.project_id)
            .all()
        )
        projects = []
        for project_id in listed:
            project = known.get(str(project_id), (None, None))[0]
            projects.append(
                CiSharedFlowProject(
                    project_id=project_id,
                    name=project.name if project is not None else None,
                    repository_slug=project.slug if project is not None else None,
                    identities=int(counts.get(project_id, 0)),
                )
            )
        return CiSharedFlowRead(
            flow_id=flow.id,
            name=str(flow.name),
            binding_mode=_binding_mode(config),
            read_only=config.get("enabled") is True and read_only_review(config),
            projects=projects,
        )

    def _account_flow(
        self, db: Session, *, actor: models.User, flow_id: UUID, lock: bool
    ) -> models.Flow:
        query = (
            db.query(models.Flow)
            .populate_existing()
            .filter(
                models.Flow.id == flow_id,
                models.Flow.account_id == actor.account_id,
            )
        )
        if lock:
            query = query.with_for_update()
        flow = query.first()
        if flow is None:
            raise LookupError("CI flow not found")
        return flow

    def shared_flow(
        self, db: Session, *, actor: models.User, flow_id: UUID
    ) -> CiSharedFlowRead:
        """Read the restricted CI project allowlist of one account-local flow.

        Args:
            db: Database session.
            actor: Human who may view CI administration.
            flow_id: Flow in the actor's account.

        Returns:
            The flow's binding mode and admitted projects.

        Raises:
            PermissionError: The actor may not view CI administration.
            LookupError: The flow is not in the actor's account.
        """
        self._authorize(db, actor=actor, operation="capabilities", grant=None)
        return self._shared_flow_read(
            db, flow=self._account_flow(db, actor=actor, flow_id=flow_id, lock=False)
        )

    def share_flow(
        self,
        db: Session,
        *,
        actor: models.User,
        flow_id: UUID,
        payload: CiSharedFlowUpdate,
    ) -> CiSharedFlowRead:
        """Replace the project allowlist of a shared read-only review flow.

        The flow must already be an enabled read-only review flow. Preloop
        derives one repository entry per project from the project's owning
        GitHub App tracker and writes the allowlist to the flow's trigger
        projects and clone repositories together, so the two cannot drift.
        Admitting a project grants nothing by itself: an identity must still
        be provisioned for that project. Removing a project takes effect on
        the next authorization of every identity bound to it.

        Args:
            db: Database session.
            actor: Human who may manage CI identities.
            flow_id: Flow in the actor's account.
            payload: The complete new allowlist.

        Returns:
            The stored allowlist.

        Raises:
            PermissionError: The actor may not manage CI identities.
            LookupError: The flow is not in the actor's account.
            CiSharedFlowError: The flow or a project cannot be admitted.
        """
        from preloop.models.schemas.flow import GitCloneConfig
        from preloop.services.publication_credentials import (
            validate_publication_tracker,
        )
        from preloop.services.read_only_review import (
            read_only_review,
            validate_read_only_flow,
        )

        self.require_available(db)
        self._authorize(
            db,
            actor=actor,
            operation="manage_capabilities",
            grant=None,
            audit_operation="share_flow",
        )
        flow = self._account_flow(db, actor=actor, flow_id=flow_id, lock=True)
        try:
            config = flow.git_clone_config
            if (
                not isinstance(config, dict)
                or config.get("enabled") is not True
                or not read_only_review(config)
            ):
                raise CiSharedFlowError(SHARED_FLOW_NOT_READ_ONLY)
            if config.get("ci_project_selection") is not True and (
                db.query(models.CiPrincipal.id)
                .filter(
                    models.CiPrincipal.account_id == flow.account_id,
                    models.CiPrincipal.flow_id == flow.id,
                )
                .first()
                is not None
            ):
                raise CiSharedFlowError(SHARED_FLOW_HAS_DEDICATED_IDENTITIES)
            current = config.get("repositories")
            existing = {
                repo["project_id"]: repo
                for repo in (current if isinstance(current, list) else [])
                if isinstance(repo, dict) and isinstance(repo.get("project_id"), str)
            }
            known = self._account_projects(
                db, account_id=flow.account_id, project_ids=list(payload.project_ids)
            )
            repositories: list[dict[str, Any]] = []
            for project_id in payload.project_ids:
                project, tracker = known.get(str(project_id), (None, None))
                if (
                    project is None
                    or tracker is None
                    or not project.slug
                    or not project.identifier
                ):
                    raise CiSharedFlowError(SHARED_FLOW_PROJECT_REFUSED)
                try:
                    validate_publication_tracker(tracker)
                except ValueError:
                    raise CiSharedFlowError(SHARED_FLOW_PROJECT_REFUSED) from None
                kept = existing.get(str(project.id))
                if (
                    kept is not None
                    and kept.get("tracker_id") == str(tracker.id)
                    and not kept.get("repository_url")
                ):
                    repositories.append(dict(kept))
                else:
                    repositories.append(
                        {"project_id": str(project.id), "tracker_id": str(tracker.id)}
                    )
            updated = {
                **config,
                "ci_project_selection": True,
                "repositories": repositories,
            }
            admitted = [str(project_id) for project_id in payload.project_ids]
            try:
                GitCloneConfig.model_validate(updated)
                flow.git_clone_config = updated
                flow.trigger_project_ids = admitted
                validate_read_only_flow(flow)
            except (ValidationError, ValueError):
                raise CiSharedFlowError(SHARED_FLOW_CONTRACT_REFUSED) from None
            CRUDAuditLog(models.AuditLog).log_action(
                db,
                account_id=flow.account_id,
                user_id=actor.id,
                action="ci_shared_flow_update",
                resource_type="flow",
                resource_id=str(flow.id),
                status="success",
                commit=False,
                details={
                    "flow_id": str(flow.id),
                    "project_ids": admitted,
                    "added": sorted(set(admitted) - set(existing)),
                    "removed": sorted(set(existing) - set(admitted)),
                },
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        return self._shared_flow_read(
            db, flow=self._account_flow(db, actor=actor, flow_id=flow_id, lock=False)
        )


crud_ci_administration = CRUDCiAdministration()
