"""Configured model consumers, kept separate from observed gateway usage."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.plugins.account_hooks import (
    AuthorizationContext,
    VISIBLE_AI_MODEL,
    VISIBLE_FLOW,
    VISIBLE_MANAGED_AGENT,
    authorize,
    filter_viewable,
)
from preloop.schemas.ai_model import (
    AIModelAgentLink,
    AIModelCiIdentityLink,
    AIModelFlowLink,
    AIModelSharedCredentialLink,
    AIModelUsedByResponse,
)
from preloop.schemas.ci_principal import CiGrant
from preloop.utils.permissions import user_holds_permission

MAX_CONSUMERS = 5000


def _can_view_consumers(db: Session, user: models.User, permission: str) -> bool:
    """Respect account authorizer restrictions as well as role permissions."""
    decision = authorize(
        AuthorizationContext(account_id=user.account_id, db=db, user=user),
        permission,
        None,
    )
    return decision.allowed and (
        bool(getattr(user, "is_superuser", False))
        or user_holds_permission(db, user, permission)
    )


def _parse_time(value: object) -> Optional[datetime]:
    """Read an ISO timestamp from the last-run map."""
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def flow_links(
    db: Session, *, user: models.User, flows: list[models.Flow]
) -> list[AIModelFlowLink]:
    """Describe flows with their enabled state and most recent run.

    One query for the last runs of all flows, whatever their number.

    Args:
        db: Database session.
        user: Caller; only flows it may view are described.
        flows: Flows already scoped to the caller's account.

    Returns:
        Links for the visible flows, sorted by name.
    """
    visible = filter_viewable(db, user, VISIBLE_FLOW, flows)
    last_runs = crud.crud_flow_execution.get_last_runs_for_flows(
        db, [flow.id for flow in visible]
    )
    links = []
    for flow in visible:
        last = last_runs.get(str(flow.id))
        last = last if isinstance(last, dict) else {}
        links.append(
            AIModelFlowLink(
                id=str(flow.id),
                name=flow.name,
                is_enabled=flow.is_enabled,
                last_execution_at=_parse_time(last.get("start_time")),
                last_execution_status=last.get("status"),
            )
        )
    links.sort(key=lambda item: (item.name.casefold(), item.id))
    return links


def _ci_identity_links(
    db: Session, *, user: models.User, flow_ids: list[str]
) -> list[AIModelCiIdentityLink]:
    """List CI identities bound to the flows that the caller may view."""
    bindings = crud.crud_ci_principal.list_bound_to_flows(
        db,
        account_id=user.account_id,
        flow_ids=[uuid.UUID(flow_id) for flow_id in flow_ids],
    )
    links = []
    for binding in bindings:
        principal = binding.principal
        try:
            crud.crud_ci_principal._authorize(
                db,
                actor=user,
                operation="view",
                grant=CiGrant.model_validate(principal.grant),
            )
        except (PermissionError, ValueError):
            continue
        links.append(
            AIModelCiIdentityLink(
                id=str(principal.id),
                name=principal.name,
                flow_id=str(principal.flow_id),
                is_active=principal.is_active,
                retired=principal.flow_id is None,
                active_key_count=binding.active_key_count,
                key_expires_at=binding.key_expires_at,
            )
        )
    return links


def shared_credential_links(
    db: Session,
    *,
    user: models.User,
    secret_id: Optional[uuid.UUID],
    exclude_model_id: Optional[str] = None,
) -> list[AIModelSharedCredentialLink]:
    """List the caller's visible models that resolve one credential secret."""
    if secret_id is None:
        return []
    rows = crud.crud_ai_model.list_sharing_secret(
        db, secret_id=secret_id, account_id=user.account_id
    )
    return [
        AIModelSharedCredentialLink(
            id=str(row.id),
            name=row.name,
            credentials_status=row.credentials_status,
        )
        for row in filter_viewable(db, user, VISIBLE_AI_MODEL, rows)
        if str(row.id) != exclude_model_id
    ]


def configured_model_consumers(
    db: Session,
    *,
    user: models.User,
    model_id: str,
    credentials_secret_id: Optional[uuid.UUID] = None,
) -> AIModelUsedByResponse:
    """Read tenant-owned configuration in batches and apply resource visibility.

    Inactive bindings and slots created by gateway traffic do not establish
    configuration. Legacy enrollment config is consulted only when an agent
    has no binding rows at all, so deactivated bindings cannot reappear.
    Flows carry their enabled state and last run; CI identities bound to
    those flows and other models sharing ``credentials_secret_id`` are
    listed alongside.
    """
    # Reuse the agent API's compatibility parser for older CLI enrollments.
    from preloop.api.endpoints.account import (
        _ai_model_meta_lookup,
        _managed_agent_configured_model_alias,
        _managed_agent_configured_models_from_cache,
        _normalize_gateway_model_alias,
    )

    account_id = str(user.account_id)
    result = AIModelUsedByResponse()
    if _can_view_consumers(db, user, "view_agents"):
        agents = crud.crud_managed_agent.get_multi(
            db, account_id=account_id, limit=MAX_CONSUMERS + 1
        )
        result.truncated = len(agents) > MAX_CONSUMERS
        agents = filter_viewable(
            db, user, VISIBLE_MANAGED_AGENT, agents[:MAX_CONSUMERS]
        )
        bindings = crud.crud_managed_agent_ai_model_binding.list_for_agents(
            db,
            account_id=account_id,
            agent_ids=[str(agent.id) for agent in agents],
            include_inactive=True,
        )
        legacy_ids = [str(a.id) for a in agents if not bindings.get(str(a.id))]
        enrollments = (
            crud.crud_managed_agent_enrollment.list_latest_by_agents(
                db, account_id=account_id, agent_ids=legacy_ids
            )
            if legacy_ids
            else {}
        )
        legacy_config: dict[str, dict] = {}
        for agent_id, picks in enrollments.items():
            enrollment = (
                picks.get("cli_managed_config")
                or picks.get("runtime_plugin_control")
                or picks.get("any")
            )
            if enrollment is not None:
                summary = crud.crud_managed_agent_enrollment._to_summary(enrollment)
                if _managed_agent_configured_model_alias(summary) is not None:
                    legacy_config[agent_id] = summary
        account_models = (
            crud.crud_ai_model.get_by_account(db, account_id=account_id)
            if legacy_config
            else []
        )
        for agent in agents:
            agent_id = str(agent.id)
            rows = bindings.get(agent_id, [])
            configured_ids = {
                str(row.ai_model_id)
                for row in rows
                if row.status != "inactive"
                and row.binding_type == "configured"
                and not row.config_key.startswith("gateway.autoregister.")
            }
            if not rows and agent_id in legacy_config:
                configured_alias = _managed_agent_configured_model_alias(
                    legacy_config[agent_id]
                )
                legacy_rows = _managed_agent_configured_models_from_cache(
                    agent_id=agent_id,
                    binding_rows=[],
                    latest_enrollment=legacy_config[agent_id],
                    account_models=[
                        model
                        for model in account_models
                        if _normalize_gateway_model_alias(
                            _ai_model_meta_lookup(model, "gateway", "model_alias")
                        )
                        == configured_alias
                    ],
                )
                configured_ids = {
                    str(row.ai_model_id) for row in legacy_rows if row.ai_model_id
                }
            if model_id in configured_ids:
                result.agents.append(
                    AIModelAgentLink(
                        id=agent_id,
                        name=agent.display_name,
                        agent_kind=agent.agent_kind,
                    )
                )
    if _can_view_consumers(db, user, "view_flows"):
        flows = crud.crud_flow.list_configured_for_model(
            db,
            account_id=account_id,
            model_id=model_id,
            limit=MAX_CONSUMERS + 1,
        )
        result.truncated = result.truncated or len(flows) > MAX_CONSUMERS
        result.flows = flow_links(db, user=user, flows=flows[:MAX_CONSUMERS])
        result.ci_identities = _ci_identity_links(
            db, user=user, flow_ids=[flow.id for flow in result.flows]
        )
    result.shared_credential_models = shared_credential_links(
        db,
        user=user,
        secret_id=credentials_secret_id,
        exclude_model_id=model_id,
    )
    result.enabled_flow_count = sum(1 for flow in result.flows if flow.is_enabled)
    result.disabled_flow_count = len(result.flows) - result.enabled_flow_count
    result.agents.sort(key=lambda item: (item.name.casefold(), item.id))
    return result
