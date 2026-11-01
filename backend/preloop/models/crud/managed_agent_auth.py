"""Account-owned managed-agent identity, independent of creator membership."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models


@dataclass(frozen=True)
class ManagedAgentPrincipal:
    """Runtime actor; id is deliberately not a human membership identifier."""

    account_id: UUID
    principal_id: UUID
    created_by_user_id: UUID | None
    username: str
    _auth_api_key: models.ApiKey = field(repr=False)
    id: None = None
    person_id: None = None
    email: str = ""
    is_active: bool = True
    is_superuser: bool = False


def authenticate(
    db: Session,
    *,
    account_id: UUID,
    key_id: UUID,
    agent_id: UUID | str,
    touch: bool = True,
) -> ManagedAgentPrincipal | None:
    """Validate all tenant bindings and current lifecycle without human authority."""
    try:
        agent_id = UUID(str(agent_id))
    except (ValueError, TypeError, AttributeError):
        return None
    row = db.execute(
        select(models.ApiKey, models.ManagedAgentCredential, models.ManagedAgent)
        .join(
            models.ManagedAgentCredential,
            models.ManagedAgentCredential.api_key_id == models.ApiKey.id,
        )
        .join(
            models.ManagedAgent,
            models.ManagedAgent.id == models.ManagedAgentCredential.managed_agent_id,
        )
        .join(models.Account, models.Account.id == models.ApiKey.account_id)
        .where(
            models.ApiKey.id == key_id,
            models.ApiKey.account_id == account_id,
            models.ManagedAgentCredential.account_id == account_id,
            models.ManagedAgentCredential.managed_agent_id == agent_id,
            models.ManagedAgent.account_id == account_id,
            models.ManagedAgentCredential.status == "active",
            models.ManagedAgent.lifecycle_state == "active",
            models.Account.is_active.is_(True),
            models.ApiKey.is_active.is_(True),
        )
        .execution_options(populate_existing=True)
    ).first()
    if row is None:
        return None
    key, credential, agent = row
    context = key.context_data if isinstance(key.context_data, dict) else {}
    if (
        key.is_expired
        or context.get("credential_kind") != "managed_agent_durable"
        or str(context.get("managed_agent_id")) != str(agent.id)
    ):
        return None
    # Scopes remain credential-bound; runtime adapters apply their own route,
    # resource, tool and model policies after this identity check.
    if touch:
        key.last_used_at = datetime.now(UTC)
        db.flush()
    return ManagedAgentPrincipal(
        account_id=account_id,
        principal_id=agent.id,
        created_by_user_id=credential.created_by_user_id,
        username=agent.display_name,
        _auth_api_key=key,
    )


def actor_user_id(actor: models.User | ManagedAgentPrincipal) -> UUID | None:
    """Creator attribution is retained separately and never grants authority."""
    return (
        actor.created_by_user_id
        if isinstance(actor, ManagedAgentPrincipal)
        else actor.id
    )
