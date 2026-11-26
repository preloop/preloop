"""Undo what an unfinished managed-agent onboarding left on the server.

``preloop agents onboard`` creates the managed agent, an agent credential and
AI models before it writes the local agent config. When that config cannot be
written (a read-only container) the CLI records a ``pending_local_config``
enrollment and waits for the user to apply the change by hand. Three callers
share :func:`cleanup_onboarding_resources` so no path leaves an active agent
without a usable key, a stored provider credential or an orphan model:

* cancelling a pending enrollment,
* the rollback endpoint the CLI calls when onboarding fails or is aborted,
* the hourly ``expire_pending_enrollments`` sweep
  (:func:`run_pending_enrollment_expiry_once`).

The cleanup removes only what the onboarding reported as created, and only
when it belongs to the account and the agent. Ids of anything else are
ignored. All database access goes through ``preloop.models.crud``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import (
    crud_ai_model,
    crud_api_key,
    crud_audit_log,
    crud_flow,
    crud_managed_agent,
    crud_managed_agent_ai_model_binding,
    crud_managed_agent_credential,
    crud_managed_agent_enrollment,
    crud_runtime_session,
)
from preloop.models.crud.billing import billing
from preloop.models.crud.managed_agent_enrollment import (
    ENROLLMENT_STATUS_EXPIRED,
    ENROLLMENT_STATUS_PENDING_LOCAL_CONFIG,
    pending_enrollment_expires_at,
    pending_enrollment_ttl,
)
from preloop.services.account_realtime import (
    ACCOUNT_TOPIC_AUDIT,
    ACCOUNT_TOPIC_MANAGED_AGENTS,
    build_account_event,
    emit_account_event,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AGENT_KEY_PLACEHOLDER",
    "AUDIT_ACTION_CANCELLED",
    "AUDIT_ACTION_EXPIRED",
    "AUDIT_ACTION_ROLLED_BACK",
    "CreatedResources",
    "OnboardingCleanupResult",
    "cleanup_onboarding_resources",
    "deactivate_agent_keys",
    "emit_agent_decommissioned",
    "emit_onboarding_cleanup_events",
    "parse_created_resources",
    "pending_enrollment_expires_at",
    "redact_agent_keys",
    "remove_agent_registry_entry",
    "revoke_agent_runtime_access",
    "run_pending_enrollment_expiry_once",
    "settings",
]

AGENT_KEY_PLACEHOLDER = "<AGENT_KEY>"
AUDIT_ACTION_CANCELLED = "managed_agent_enrollment_cancelled"
AUDIT_ACTION_EXPIRED = "managed_agent_enrollment_expired"
AUDIT_ACTION_ROLLED_BACK = "managed_agent_onboarding_rolled_back"

AGENT_DELETED = "deleted"
AGENT_DECOMMISSIONED = "decommissioned"
AGENT_KEPT = "kept"

# Agent credentials are ``agt_`` plus 32 url-safe random bytes. Shorter
# strings (a prefix shown in the console, a placeholder) are left alone.
_AGENT_KEY_PATTERN = re.compile(r"agt_[A-Za-z0-9_\-]{16,}")
_MAX_IDS = 200
_EXPIRY_BATCH = 200


def redact_agent_keys(value: Any) -> Any:
    """Replace anything that looks like a real agent key with the placeholder.

    Manual-apply instructions are stored and shown again later, so they must
    only ever carry ``<AGENT_KEY>``. This is a defensive pass over what the
    CLI sent: dict keys, strings and nested containers are all covered.

    Args:
        value: Any JSON-like value.

    Returns:
        A copy of ``value`` with agent keys replaced.
    """
    if isinstance(value, str):
        return _AGENT_KEY_PATTERN.sub(AGENT_KEY_PLACEHOLDER, value)
    if isinstance(value, dict):
        return {
            redact_agent_keys(key) if isinstance(key, str) else key: redact_agent_keys(
                item
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_agent_keys(item) for item in value]
    return value


@dataclass(frozen=True)
class CreatedResources:
    """What one onboarding run created, as opposed to reused.

    Attributes:
        agent_created: The run created the managed agent.
        agent_revived: The run reactivated a decommissioned agent.
        credential_ids: Agent credentials the run issued.
        ai_model_ids: AI models the run created.
    """

    agent_created: bool = False
    agent_revived: bool = False
    credential_ids: tuple[str, ...] = ()
    ai_model_ids: tuple[str, ...] = ()


def _uuid_strings(raw: Any) -> tuple[str, ...]:
    """Return the well-formed UUIDs of a list, deduplicated, in order."""
    if not isinstance(raw, (list, tuple)):
        return ()
    seen: dict[str, None] = {}
    for item in raw[:_MAX_IDS]:
        if not isinstance(item, str):
            continue
        try:
            seen.setdefault(str(UUID(item.strip())), None)
        except ValueError:
            continue
    return tuple(seen)


def parse_created_resources(raw: Any) -> CreatedResources:
    """Read a ``created_resources`` object leniently.

    The value comes from a request body or from an enrollment's stored JSON,
    so anything malformed is dropped instead of failing the cleanup.
    ``mcp_server_ids`` is accepted and ignored: MCP servers are never removed.

    Args:
        raw: The ``created_resources`` value, in any shape.

    Returns:
        The parsed resources; empty when ``raw`` is not an object.
    """
    if not isinstance(raw, dict):
        return CreatedResources()
    return CreatedResources(
        agent_created=raw.get("agent_created") is True,
        agent_revived=raw.get("agent_revived") is True,
        credential_ids=_uuid_strings(raw.get("credential_ids")),
        ai_model_ids=_uuid_strings(raw.get("ai_model_ids")),
    )


@dataclass
class OnboardingCleanupResult:
    """Outcome of one cleanup.

    Attributes:
        credentials: Agent credentials revoked.
        ai_models: AI models deleted.
        agent: ``deleted``, ``decommissioned`` or ``kept``.
        agent_id: The agent the cleanup ran for.
        session_source_type: The agent's runtime principal type.
        session_source_id: The agent's runtime principal id.
    """

    credentials: int = 0
    ai_models: int = 0
    agent: str = AGENT_KEPT
    agent_id: str = ""
    session_source_type: Optional[str] = None
    session_source_id: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        """Return the ``removed`` object of the API responses."""
        return {
            "credentials": self.credentials,
            "ai_models": self.ai_models,
            "agent": self.agent,
        }


def revoke_agent_runtime_access(
    db: Session,
    *,
    account_id: Any,
    agent: models.ManagedAgent,
    reason: str,
    bound_runtime_session_id: Optional[str],
    revoked_at: Optional[datetime] = None,
) -> int:
    """Revoke every credential and runtime key of a decommissioned agent.

    Shared by ``PATCH /agents/{id}`` (``lifecycle_action=decommission``) and
    the onboarding cleanup. Only this agent's keys are revoked, plus legacy
    keys with no agent binding: several registry entries can share one runtime
    principal, and a principal-wide sweep would also kill sibling agents'
    durable credentials. Flushes without committing.

    Args:
        db: Database session.
        account_id: Owning account id.
        agent: The agent being decommissioned.
        reason: Recorded on each revoked credential.
        bound_runtime_session_id: The session the agent was bound to before
            the lifecycle change unbound it, if any. It is ended.
        revoked_at: Revocation timestamp; defaults to now.

    Returns:
        Number of managed credentials revoked.
    """
    timestamp = revoked_at or datetime.now(UTC)
    revoked = crud_managed_agent_credential.revoke_all_for_agent(
        db,
        account_id=str(account_id),
        agent_id=str(agent.id),
        reason=reason,
        revoked_at=timestamp,
    )
    crud_api_key.deactivate_runtime_keys_for_managed_agent(
        db,
        account_id=account_id,
        managed_agent_id=str(agent.id),
        commit=False,
    )
    crud_api_key.deactivate_unbound_runtime_keys_for_principal(
        db,
        account_id=account_id,
        runtime_principal_type=agent.session_source_type,
        runtime_principal_id=agent.session_source_id,
        commit=False,
    )
    if bound_runtime_session_id is not None:
        crud_runtime_session.update_operator_state(
            db,
            account_id=str(account_id),
            runtime_session_id=bound_runtime_session_id,
            ended_at=timestamp,
            commit=False,
        )
    return revoked


def deactivate_agent_keys(
    db: Session, *, account_id: Any, agent: models.ManagedAgent
) -> int:
    """Deactivate the API keys of an agent that is about to be removed.

    Shared by ``DELETE /agents/{id}`` and the onboarding cleanup. Only this
    agent's keys are deactivated, plus legacy keys with no agent binding (see
    :func:`revoke_agent_runtime_access`). Flushes without committing.

    Args:
        db: Database session.
        account_id: Owning account id.
        agent: The agent being removed.

    Returns:
        Number of API keys deactivated.
    """
    legacy_keys = crud_api_key.deactivate_unbound_runtime_keys_for_principal(
        db,
        account_id=account_id,
        runtime_principal_type=agent.session_source_type,
        runtime_principal_id=agent.session_source_id,
        commit=False,
    )
    bound_keys = crud_api_key.deactivate_runtime_keys_for_managed_agent(
        db,
        account_id=account_id,
        managed_agent_id=str(agent.id),
        commit=False,
    )
    return len(legacy_keys) + len(bound_keys)


def remove_agent_registry_entry(
    db: Session, *, account_id: Any, agent: models.ManagedAgent
) -> int:
    """Deactivate an agent's keys and delete its registry entry.

    The same two steps as ``DELETE /agents/{id}``. Flushes without committing.

    Args:
        db: Database session.
        account_id: Owning account id.
        agent: The agent to remove.

    Returns:
        Number of API keys deactivated.
    """
    revoked = deactivate_agent_keys(db, account_id=account_id, agent=agent)
    crud_managed_agent.remove_registry_entry(db, agent=agent, commit=False)
    return revoked


def _remove_created_model(
    db: Session, *, account_id: str, agent_id: str, model_id: str
) -> bool:
    """Delete one AI model the onboarding created, if nothing else uses it.

    Applies the refusals of ``DELETE /ai-models/{id}`` (a flow still selects
    the model) plus the onboarding-specific ones: the model must be owned by
    the account, must carry this agent's onboarding tag
    (``meta_data.managed_agent_id``) and must not be bound to another agent.
    ``crud_ai_model.remove`` drops the stored credential secret once no model
    references it.

    Returns:
        True when the model was deleted, False when it was left alone.
    """
    model = crud_ai_model.get(db, id=UUID(model_id))
    if model is None or model.account_id is None:
        return False
    if str(model.account_id) != account_id:
        return False
    # Onboarding tags every model it creates with the agent it created it
    # for. Requiring that tag keeps this path, which needs only
    # ``manage_agents``, from deleting account models that
    # ``DELETE /ai-models/{id}`` guards with ``delete_ai_models``.
    tagged_agent = (model.meta_data or {}).get("managed_agent_id")
    if not tagged_agent or str(tagged_agent) != agent_id:
        return False
    if crud_managed_agent_ai_model_binding.model_bound_to_other_agent(
        db, account_id=account_id, ai_model_id=model_id, agent_id=agent_id
    ):
        return False
    if crud_flow.list_configured_for_model(
        db, account_id=account_id, model_id=model_id, limit=1
    ):
        return False
    crud_ai_model.remove(db, id=model.id, commit=False)
    return True


def _remove_created_models(
    db: Session, *, account_id: str, agent_id: str, model_ids: tuple[str, ...]
) -> int:
    """Delete the removable models; one that fails is skipped, not fatal."""
    removed = 0
    for model_id in model_ids:
        try:
            with db.begin_nested():
                if _remove_created_model(
                    db, account_id=account_id, agent_id=agent_id, model_id=model_id
                ):
                    removed += 1
        except Exception as error:
            logger.warning(
                "Onboarding cleanup skipped an AI model: %s", type(error).__name__
            )
    return removed


def _settle_agent(
    db: Session,
    *,
    agent: models.ManagedAgent,
    resources: CreatedResources,
    revoked_credentials: int,
    reason: str,
) -> str:
    """Delete, decommission or keep the agent after its resources are gone.

    Rules, in order:

    1. An agent this onboarding created, with no validated or restored
       enrollment and no recorded usage, is deleted.
    2. An agent this onboarding revived is decommissioned again, whatever
       older enrollments it carries: those belong to a finished onboarding.
    3. An agent this onboarding touched (created it or issued one of the
       revoked credentials) that is left with no active credential and was
       never onboarded is decommissioned.
    4. Anything else is kept. An agent the onboarding changed nothing on is
       never decommissioned, even without a durable credential: it may run on
       runtime session tokens.

    Returns:
        ``deleted``, ``decommissioned`` or ``kept``.
    """
    account_id = str(agent.account_id)
    agent_id = str(agent.id)
    onboarded = crud_managed_agent_enrollment.has_onboarded_enrollment(
        db, account_id=account_id, agent_id=agent_id
    )
    if (
        resources.agent_created
        and not onboarded
        and not crud_managed_agent.has_recorded_usage(db, agent=agent)
    ):
        remove_agent_registry_entry(db, account_id=agent.account_id, agent=agent)
        return AGENT_DELETED

    touched = resources.agent_created or revoked_credentials > 0
    left_without_key = (
        touched
        and not onboarded
        and crud_managed_agent_credential.count_active_for_agent(
            db, account_id=account_id, agent_id=agent_id
        )
        == 0
    )
    if not (resources.agent_revived or left_without_key):
        return AGENT_KEPT
    if agent.lifecycle_state == "decommissioned":
        return AGENT_DECOMMISSIONED
    bound_runtime_session_id = (
        str(agent.runtime_session_id) if agent.runtime_session_id is not None else None
    )
    crud_managed_agent.update_operator_state(
        db,
        account_id=account_id,
        agent_id=agent_id,
        lifecycle_state="decommissioned",
        lifecycle_reason=reason,
        commit=False,
    )
    revoke_agent_runtime_access(
        db,
        account_id=agent.account_id,
        agent=agent,
        reason=reason,
        bound_runtime_session_id=bound_runtime_session_id,
    )
    return AGENT_DECOMMISSIONED


def cleanup_onboarding_resources(
    db: Session,
    *,
    account_id: Any,
    agent_id: str,
    created_resources: Any,
    reason: str,
    audit_action: str,
    actor_user_id: Optional[UUID] = None,
    audit_details: Optional[dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> Optional[OnboardingCleanupResult]:
    """Remove what one onboarding created for one agent, and audit it.

    Strictly scoped: a credential is revoked only when it belongs to this
    agent in this account, a model is deleted only when the account owns it
    and nothing else uses it, and ids that match neither are ignored. Flushes
    without committing so the caller commits the cleanup together with its own
    writes (the enrollment status).

    Args:
        db: Database session.
        account_id: Account the agent belongs to.
        agent_id: Managed agent id.
        created_resources: The onboarding's ``created_resources`` object, in
            any shape (see :func:`parse_created_resources`).
        reason: Recorded on revoked credentials and a decommissioned agent.
        audit_action: Audit log action name.
        actor_user_id: The user who asked for the cleanup; ``None`` for the
            expiry sweep.
        audit_details: Extra audit details (for example the enrollment id).
        ip_address: Caller address for the audit row.
        user_agent: Caller user agent for the audit row.

    Returns:
        The cleanup result, or ``None`` when the agent is not in the account.
    """
    account_key = str(account_id)
    resources = parse_created_resources(created_resources)
    # Same lock order as credential issuance and the lifecycle transitions.
    billing.lock_account(db, account_key)
    agent = crud_managed_agent.get_for_account(
        db, account_id=account_key, agent_id=agent_id, for_update=True
    )
    if agent is None:
        return None
    result = OnboardingCleanupResult(
        agent_id=str(agent.id),
        session_source_type=agent.session_source_type,
        session_source_id=agent.session_source_id,
    )
    display_name = agent.display_name

    for credential_id in resources.credential_ids:
        credential = crud_managed_agent_credential.get_for_agent(
            db,
            account_id=account_key,
            agent_id=result.agent_id,
            credential_id=credential_id,
        )
        if credential is None or credential.status != "active":
            continue
        crud_managed_agent_credential.revoke_for_agent(
            db,
            account_id=account_key,
            agent_id=result.agent_id,
            credential_id=credential_id,
            reason=reason,
            commit=False,
        )
        result.credentials += 1

    result.ai_models = _remove_created_models(
        db,
        account_id=account_key,
        agent_id=result.agent_id,
        model_ids=resources.ai_model_ids,
    )
    result.agent = _settle_agent(
        db,
        agent=agent,
        resources=resources,
        revoked_credentials=result.credentials,
        reason=reason,
    )
    crud_audit_log.log_action(
        db,
        account_id=account_key,
        user_id=actor_user_id,
        action=audit_action,
        resource_type="managed_agent",
        resource_id=result.agent_id,
        status="success",
        ip_address=ip_address,
        user_agent=user_agent,
        details={
            **(audit_details or {}),
            "agent_id": result.agent_id,
            "display_name": display_name,
            "reason": reason,
            "agent_created": resources.agent_created,
            "agent_revived": resources.agent_revived,
            "requested_credentials": len(resources.credential_ids),
            "requested_ai_models": len(resources.ai_model_ids),
            "removed": result.as_dict(),
        },
        commit=False,
    )
    return result


def emit_onboarding_cleanup_events(
    *,
    account_id: Any,
    result: OnboardingCleanupResult,
    action: str,
    enrollment_id: Optional[str] = None,
) -> None:
    """Publish the realtime events of a committed cleanup. Never raises.

    Args:
        account_id: Account the agent belongs to.
        result: The committed cleanup result.
        action: Audit action name.
        enrollment_id: The enrollment that was cancelled or expired, if any.
    """
    emit_account_event(
        build_account_event(
            account_id=str(account_id),
            topic=ACCOUNT_TOPIC_AUDIT,
            event_type="audit_event",
            payload={
                "action": action,
                "agent_id": result.agent_id,
                "enrollment_id": enrollment_id,
                "removed": result.as_dict(),
            },
        )
    )
    if result.agent == AGENT_DELETED:
        emit_account_event(
            build_account_event(
                account_id=str(account_id),
                topic=ACCOUNT_TOPIC_MANAGED_AGENTS,
                event_type="managed_agent_removed",
                payload={
                    "agent_id": result.agent_id,
                    "session_source_type": result.session_source_type,
                    "session_source_id": result.session_source_id,
                },
            )
        )


def emit_agent_decommissioned(db: Session, *, account_id: Any, agent_id: str) -> None:
    """Publish the agent summary after a cleanup decommissioned it.

    The same event ``PATCH /agents/{id}`` publishes for a decommission, so an
    open console updates without a reload. Best effort: never raises.

    Args:
        db: Database session, after the cleanup was committed.
        account_id: Account the agent belongs to.
        agent_id: The decommissioned agent.
    """
    try:
        # Imported here: the endpoint module imports this one.
        from preloop.api.endpoints.account import (
            _build_managed_agent_detail_response,
        )

        detail = _build_managed_agent_detail_response(
            db, account_id=str(account_id), agent_id=agent_id
        )
        if detail is None:
            return
        emit_account_event(
            build_account_event(
                account_id=str(account_id),
                topic=ACCOUNT_TOPIC_MANAGED_AGENTS,
                event_type="managed_agent_decommissioned",
                payload=detail.agent.model_dump(mode="json"),
                runtime_session_id=detail.agent.runtime_session_id,
            )
        )
    except Exception as error:
        db.rollback()
        logger.warning(
            "Publishing a decommissioned agent failed: %s", type(error).__name__
        )


def _expire_one(
    db: Session, *, account_id: str, agent_id: str, enrollment_id: str
) -> Optional[OnboardingCleanupResult]:
    """Expire one enrollment in its own transaction, if it is still pending."""
    billing.lock_account(db, account_id)
    enrollment = crud_managed_agent_enrollment.get_for_agent(
        db,
        account_id=account_id,
        agent_id=agent_id,
        enrollment_id=enrollment_id,
        for_update=True,
    )
    if (
        enrollment is None
        or enrollment.status != ENROLLMENT_STATUS_PENDING_LOCAL_CONFIG
    ):
        # Resumed or cancelled since the sweep listed it.
        db.rollback()
        return None
    created_resources = (enrollment.backup_metadata or {}).get("created_resources")
    crud_managed_agent_enrollment.close_pending(
        db, enrollment=enrollment, status=ENROLLMENT_STATUS_EXPIRED
    )
    result = cleanup_onboarding_resources(
        db,
        account_id=account_id,
        agent_id=agent_id,
        created_resources=created_resources,
        reason="pending onboarding expired",
        audit_action=AUDIT_ACTION_EXPIRED,
        audit_details={"enrollment_id": enrollment_id},
    )
    db.commit()
    return result


def run_pending_enrollment_expiry_once(
    db: Session, now: Optional[datetime] = None
) -> int:
    """Expire pending enrollments older than the TTL and clean up after them.

    Each enrollment is handled in its own transaction: its status becomes
    ``expired`` and the resources its onboarding recorded are removed. A
    failure on one enrollment is logged and does not stop the pass.

    Args:
        db: Database session.
        now: Current time, for tests. Defaults to the current UTC time.

    Returns:
        Number of enrollments expired by this pass.
    """
    moment = now or datetime.now(UTC)
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    stale = [
        (str(row.account_id), str(row.managed_agent_id), str(row.id))
        for row in crud_managed_agent_enrollment.list_stale_pending(
            db,
            created_before=moment - pending_enrollment_ttl(),
            limit=_EXPIRY_BATCH,
        )
    ]
    expired = 0
    for account_id, agent_id, enrollment_id in stale:
        try:
            result = _expire_one(
                db,
                account_id=account_id,
                agent_id=agent_id,
                enrollment_id=enrollment_id,
            )
        except Exception as error:
            db.rollback()
            logger.error(
                "Expiring pending enrollment %s failed: %s",
                enrollment_id,
                type(error).__name__,
            )
            continue
        if result is None:
            continue
        expired += 1
        emit_onboarding_cleanup_events(
            account_id=account_id,
            result=result,
            action=AUDIT_ACTION_EXPIRED,
            enrollment_id=enrollment_id,
        )
        if result.agent == AGENT_DECOMMISSIONED:
            emit_agent_decommissioned(db, account_id=account_id, agent_id=agent_id)
    if expired:
        logger.info("Expired %s pending managed-agent enrollment(s)", expired)
    return expired
