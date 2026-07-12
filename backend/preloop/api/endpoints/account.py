"""Account-related endpoints."""

import html
import logging
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID
from typing import Annotated, Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.models.crud import (
    crud_account,
    crud_ai_model,
    crud_api_key,
    crud_approval_workflow,
    crud_managed_agent,
    crud_managed_agent_ai_model_binding,
    crud_managed_agent_credential,
    crud_managed_agent_enrollment,
    crud_runtime_session,
    crud_runtime_session_activity,
    crud_user,
)
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.user import User as UserModel
from preloop.schemas.gateway_usage import (
    AccountManagedAgentListResponse,
    AccountGatewayUsageSearchResponse,
    AccountGatewayUsageSummaryResponse,
    AccountRuntimeSessionDetailResponse,
    AccountRuntimeSessionListResponse,
    GatewayTokenUsage,
    ManagedAgentCredentialCreateRequest,
    ManagedAgentCredentialCreateResponse,
    ManagedAgentCredentialSummary,
    ManagedAgentDetailResponse,
    ManagedAgentEnrollmentCreateRequest,
    ManagedAgentEnrollmentRestoreRequest,
    ManagedAgentEnrollmentSummary,
    ManagedAgentModelBindingSummary,
    ManagedAgentModelBindingSyncRequest,
    ManagedAgentEnrollmentValidateRequest,
    ManagedAgentRegisterRequest,
    ManagedAgentServerActivitySummary,
    ManagedAgentSummary,
    ManagedAgentToolActivitySummary,
    ManagedAgentUpdateRequest,
    ManagedAgentUsageAggregate,
    RuntimeSessionActivityListResponse,
    RuntimeSessionInteractionSummary,
    RuntimeSessionRequestItem,
    RuntimeSessionRequestListResponse,
    RuntimeSessionRequestTool,
    RuntimeSessionSummaryInsight,
    RuntimeSessionSummary,
    RuntimeSessionUpdateRequest,
    DashboardTelemetryResponse,
)
from preloop.schemas.subject_governance import (
    SubjectGovernanceConfig,
    SubjectGovernanceResponse,
)
from preloop.services.account_realtime import (
    ACCOUNT_TOPIC_MANAGED_AGENTS,
    ACCOUNT_TOPIC_AUDIT,
    ACCOUNT_TOPIC_RUNTIME_SESSIONS,
    build_account_event,
    emit_account_event,
)
from preloop.services.account_governance_cache import (
    invalidate_account_governance_cache,
)
from preloop.services.model_gateway_usage import ModelGatewayUsageService
from preloop.services.runtime_session_explorer import RuntimeSessionExplorerService
from preloop.services.subject_governance import (
    SUBJECT_TYPE_MANAGED_AGENTS,
    get_subject_governance,
    set_subject_governance,
)
from preloop.utils.permissions import require_permission

logger = logging.getLogger(__name__)

router = APIRouter()
public_router = APIRouter()  # Public endpoints (no auth required)

AGENT_CONTROL_CAPABILITIES = [
    "send_text_prompt",
    "send_voice_transcript",
    "target_existing_session",
    "start_new_session",
]
AGENT_CONTROL_INPUT_MODES = ["text", "voice_transcript"]
AGENT_CONTROL_OUTPUT_MODES = ["event", "status", "text"]
AGENT_CONTROL_SUPPORTED_AGENT_KINDS = {"hermes", "openclaw"}
AGENT_CONTROL_STATE_UNSUPPORTED = "unsupported"
AGENT_CONTROL_STATE_INSTALL_PENDING = "install_pending"
AGENT_CONTROL_STATE_PLUGIN_CONFIGURED = "plugin_configured"
AGENT_CONTROL_STATE_PLUGIN_CONNECTED = "plugin_connected"


def _enrollment_section(enrollment: dict, key: str) -> dict:
    """Return a dict section from an enrollment payload, or an empty dict."""
    value = enrollment.get(key)
    return value if isinstance(value, dict) else {}


def _servers_dict_contains_preloop(servers: object) -> bool:
    """Return True when a servers map includes the Preloop MCP entry."""
    return isinstance(servers, dict) and "preloop" in servers


def _validation_mcp_proxy_configured(validation: dict) -> bool:
    """Return True when validation flags confirm MCP proxy wiring."""
    return bool(
        validation.get("preloop_server_present")
        or validation.get("nested_mcp_servers_ok")
    )


def _managed_config_mcp_proxy_configured(managed_config: dict) -> bool:
    """Return True when managed config includes a Preloop MCP server entry."""
    if _servers_dict_contains_preloop(managed_config.get("servers")):
        return True
    if _servers_dict_contains_preloop(managed_config.get("mcpServers")):
        return True
    if _servers_dict_contains_preloop(managed_config.get("mcp_servers")):
        return True
    mcp = managed_config.get("mcp")
    if isinstance(mcp, dict):
        if _servers_dict_contains_preloop(mcp.get("servers")):
            return True
        if "preloop" in mcp:
            return True
    return False


def _validation_gateway_configured(validation: dict) -> bool:
    """Return True when validation flags confirm gateway routing."""
    return bool(
        validation.get("gateway_provider_ok") and validation.get("gateway_base_url_ok")
    )


def _hermes_managed_gateway_configured(managed_config: dict) -> bool:
    """Return True for Hermes-style managed model gateway config."""
    model = managed_config.get("model")
    if not isinstance(model, dict):
        return False
    base_url = model.get("base_url")
    return (
        model.get("provider") == "custom"
        and isinstance(base_url, str)
        and "/openai/v1" in base_url
        and (
            isinstance(model.get("api_key"), str)
            or isinstance(model.get("apiKey"), str)
        )
        and (
            isinstance(model.get("default"), str) or isinstance(model.get("model"), str)
        )
    )


def _openclaw_managed_gateway_configured(managed_config: dict) -> bool:
    """Return True for OpenClaw-style Anthropic gateway env wiring."""
    env = managed_config.get("env")
    return (
        isinstance(env, dict)
        and isinstance(env.get("ANTHROPIC_BASE_URL"), str)
        and isinstance(env.get("ANTHROPIC_MODEL"), str)
    )


def _generic_managed_gateway_configured(managed_config: dict) -> bool:
    """Return True for other adapter managed gateway config shapes."""
    models = managed_config.get("models")
    if isinstance(models, dict) and isinstance(models.get("providers"), dict):
        if "preloop" in models["providers"]:
            return True
    if (
        managed_config.get("model_provider") == "preloop"
        and isinstance(managed_config.get("model_providers"), dict)
        and "preloop" in managed_config["model_providers"]
    ):
        return True
    provider = managed_config.get("provider")
    model = managed_config.get("model")
    if (
        isinstance(provider, dict)
        and "preloop" in provider
        and isinstance(model, str)
        and model.startswith("preloop/")
    ):
        return True
    base_url = managed_config.get("baseUrl")
    api_key = managed_config.get("apiKey")
    if isinstance(base_url, str) and isinstance(api_key, str):
        nested_model = managed_config.get("model")
        if isinstance(nested_model, dict) and isinstance(nested_model.get("name"), str):
            return True
        if isinstance(nested_model, str):
            return True
    return False


def _managed_config_gateway_configured(managed_config: dict) -> bool:
    """Return True when managed config routes models through Preloop."""
    return (
        _hermes_managed_gateway_configured(managed_config)
        or _openclaw_managed_gateway_configured(managed_config)
        or _generic_managed_gateway_configured(managed_config)
    )


def _live_validation_disables_gateway(validation: dict) -> bool:
    """Return True when live validation results invalidate gateway onboarding."""
    live_validation_status = str(validation.get("live_validation_status") or "").strip()
    # "throttled" is deliberately NOT treated as failed: an upstream 429 proves
    # the credential authenticated at the gateway and the request reached the
    # provider, so the gateway wiring works even though the probe was rejected.
    live_validation_failed = live_validation_status == "failed"
    live_validation_missing_gateway = (
        live_validation_status == "not_run"
        and isinstance(validation.get("live_validation_skip_reason"), str)
        and "gateway token" in validation["live_validation_skip_reason"].lower()
    )
    explicit_gateway_unavailable = (
        validation.get("gateway_provider_ok") is False
        or validation.get("gateway_base_url_ok") is False
        or validation.get("gateway_token_ok") is False
        or validation.get("model_provider_rewritten") is False
        or validation.get("gateway_model_configured") is False
    )
    return (live_validation_failed or live_validation_missing_gateway) and (
        explicit_gateway_unavailable
        or validation.get("live_validation_attempted") is True
    )


def _managed_agent_onboarding_flags(
    latest_enrollment: Optional[dict],
) -> tuple[bool, bool, str]:
    if not latest_enrollment:
        return False, False, "incomplete"

    validation = _enrollment_section(latest_enrollment, "validation_result")
    managed_config = _enrollment_section(latest_enrollment, "managed_config")

    mcp_proxy_configured = _validation_mcp_proxy_configured(
        validation
    ) or _managed_config_mcp_proxy_configured(managed_config)
    cli_gateway_configured = _validation_gateway_configured(validation)
    model_gateway_configured = cli_gateway_configured or (
        bool(validation.get("gateway_model_configured"))
        or _managed_config_gateway_configured(managed_config)
    )
    if _live_validation_disables_gateway(validation):
        model_gateway_configured = False

    if mcp_proxy_configured and model_gateway_configured:
        return True, True, "fully_onboarded"
    if mcp_proxy_configured:
        return True, False, "mcp_proxy_only"
    if model_gateway_configured:
        return False, True, "gateway_only"
    return False, False, "incomplete"


def _lookup_nested_string(root: dict, *path: str) -> Optional[str]:
    current = root
    for key in path[:-1]:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if not isinstance(current, dict):
        return None
    value = current.get(path[-1])
    return value.strip() if isinstance(value, str) and value.strip() else None


def _normalize_gateway_model_alias(alias: Optional[str]) -> Optional[str]:
    if not isinstance(alias, str):
        return None
    trimmed = alias.strip()
    if not trimmed:
        return None
    if trimmed.lower().startswith("preloop/"):
        trimmed = trimmed.split("/", 1)[1].strip()
    return trimmed or None


def _managed_agent_configured_model_alias(
    latest_enrollment: Optional[dict],
) -> Optional[str]:
    if not latest_enrollment:
        return None

    managed_config = (
        latest_enrollment.get("managed_config")
        if isinstance(latest_enrollment.get("managed_config"), dict)
        else {}
    )
    candidates = [
        _lookup_nested_string(managed_config, "env", "ANTHROPIC_MODEL"),
        _lookup_nested_string(managed_config, "model"),
        _lookup_nested_string(managed_config, "model", "name"),
        _lookup_nested_string(managed_config, "agents", "defaults", "model"),
        _lookup_nested_string(managed_config, "agents", "defaults", "model", "primary"),
    ]
    for candidate in candidates:
        normalized = _normalize_gateway_model_alias(candidate)
        if normalized:
            return normalized
    return None


def _ai_model_meta_lookup(ai_model: Any, *path: str) -> Optional[str]:
    meta_data = ai_model.meta_data if isinstance(ai_model.meta_data, dict) else {}
    current: Any = meta_data
    for key in path[:-1]:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if not isinstance(current, dict):
        return None
    value = current.get(path[-1])
    return value.strip() if isinstance(value, str) and value.strip() else None


def _managed_agent_configured_model_id(
    db: Session,
    *,
    account_id: str,
    agent_id: str,
    configured_model_alias: Optional[str],
) -> Optional[str]:
    normalized_alias = _normalize_gateway_model_alias(configured_model_alias)
    models = crud_ai_model.get_by_account(db, account_id=account_id)

    best_match: Optional[Any] = None
    for ai_model in models:
        managed_agent_id = _ai_model_meta_lookup(ai_model, "managed_agent_id")
        gateway_alias = _normalize_gateway_model_alias(
            _ai_model_meta_lookup(ai_model, "gateway", "model_alias")
        )
        if managed_agent_id == agent_id and gateway_alias == normalized_alias:
            return str(ai_model.id)
        if managed_agent_id == agent_id and best_match is None:
            best_match = ai_model

    if best_match is not None:
        return str(best_match.id)

    if normalized_alias is None:
        return None

    alias_matches = [
        ai_model
        for ai_model in models
        if _normalize_gateway_model_alias(
            _ai_model_meta_lookup(ai_model, "gateway", "model_alias")
        )
        == normalized_alias
    ]
    if len(alias_matches) == 1:
        return str(alias_matches[0].id)
    return None


def _managed_agent_binding_summary(binding: Any) -> ManagedAgentModelBindingSummary:
    """Normalize one explicit binding row for API responses."""
    ai_model = getattr(binding, "ai_model", None)
    return ManagedAgentModelBindingSummary(
        id=str(binding.id),
        ai_model_id=str(binding.ai_model_id) if binding.ai_model_id else None,
        binding_type=binding.binding_type,
        config_key=binding.config_key,
        gateway_alias=binding.gateway_alias,
        is_primary=binding.is_primary,
        status=binding.status,
        provider_name=getattr(ai_model, "provider_name", None),
        model_identifier=getattr(ai_model, "model_identifier", None),
        ai_model_name=getattr(ai_model, "name", None),
        first_seen_at=binding.first_seen_at,
        last_seen_at=binding.last_seen_at,
    )


def _managed_agent_configured_models(
    db: Session,
    *,
    account_id: str,
    agent_id: str,
    latest_enrollment: Optional[dict],
) -> list[ManagedAgentModelBindingSummary]:
    """Return configured-model bindings with compatibility fallback."""
    binding_rows = crud_managed_agent_ai_model_binding.list_for_agent(
        db,
        account_id=account_id,
        agent_id=agent_id,
        include_inactive=False,
    )
    if binding_rows:
        return [_managed_agent_binding_summary(binding) for binding in binding_rows]

    configured_alias = _managed_agent_configured_model_alias(latest_enrollment)
    configured_model_id = _managed_agent_configured_model_id(
        db,
        account_id=account_id,
        agent_id=agent_id,
        configured_model_alias=configured_alias,
    )
    if configured_alias is None and configured_model_id is None:
        return []

    ai_model = None
    if configured_model_id is not None:
        try:
            ai_model = crud_ai_model.get(db, id=configured_model_id)
        except Exception:
            ai_model = None

    return [
        ManagedAgentModelBindingSummary(
            id=f"legacy-{agent_id}-{configured_alias or 'configured'}",
            ai_model_id=configured_model_id,
            binding_type="configured",
            config_key="legacy.configured_model",
            gateway_alias=configured_alias or "",
            is_primary=True,
            status="gateway_ready" if configured_model_id else "configured",
            provider_name=getattr(ai_model, "provider_name", None),
            model_identifier=getattr(ai_model, "model_identifier", None),
            ai_model_name=getattr(ai_model, "name", None),
        )
    ]


def _managed_agent_live_validation_state(
    latest_enrollment: Optional[dict],
) -> tuple[bool, Optional[bool], str, Optional[datetime]]:
    if not latest_enrollment:
        return False, None, "unsupported", None

    validation = (
        latest_enrollment.get("validation_result")
        if isinstance(latest_enrollment.get("validation_result"), dict)
        else {}
    )
    supported = bool(validation.get("live_validation_supported"))
    status = str(validation.get("live_validation_status") or "").strip()
    if not supported:
        return (
            False,
            None,
            status or "unsupported",
            latest_enrollment.get("last_validated_at"),
        )

    passed = validation.get("live_validation_passed")
    normalized_passed = passed if isinstance(passed, bool) else None
    if not status:
        if normalized_passed is True:
            status = "passed"
        elif normalized_passed is False:
            status = "failed"
        else:
            status = "not_run"
    return True, normalized_passed, status, latest_enrollment.get("last_validated_at")


def _managed_agent_control_config_flags(
    latest_enrollment: Optional[dict],
) -> tuple[bool, bool]:
    if not latest_enrollment:
        return False, False

    validation = (
        latest_enrollment.get("validation_result")
        if isinstance(latest_enrollment.get("validation_result"), dict)
        else {}
    )
    managed_config = (
        latest_enrollment.get("managed_config")
        if isinstance(latest_enrollment.get("managed_config"), dict)
        else {}
    )

    validation_control_ready = bool(
        validation.get("control_channel_configured")
        or (
            validation.get("control_plugin_verified")
            and validation.get("control_ws_url_ok")
            and validation.get("control_bearer_token_ok")
        )
    )
    control_config = _managed_agent_control_config_from_managed_config(managed_config)
    managed_control_ready = bool(
        control_config.get("enabled") is True
        and isinstance(control_config.get("control_ws_url"), str)
        and control_config["control_ws_url"].strip()
    )

    return validation_control_ready, managed_control_ready


def _managed_agent_control_config_from_managed_config(managed_config: dict) -> dict:
    """Return Agent Control config from legacy and runtime-plugin locations."""
    preloop_config = (
        managed_config.get("preloop")
        if isinstance(managed_config.get("preloop"), dict)
        else {}
    )
    control_config = (
        preloop_config.get("control")
        if isinstance(preloop_config.get("control"), dict)
        else {}
    )
    if control_config:
        return control_config

    plugins = (
        managed_config.get("plugins")
        if isinstance(managed_config.get("plugins"), dict)
        else {}
    )
    entries = plugins.get("entries") if isinstance(plugins.get("entries"), dict) else {}
    for plugin_id in (
        "preloop-plugin",
        "openclaw-plugin",
        "@preloop-ai/openclaw-plugin",
        "@preloop/openclaw-plugin",
    ):
        entry = (
            entries.get(plugin_id) if isinstance(entries.get(plugin_id), dict) else {}
        )
        config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
        if config:
            return config
    return {}


def _managed_agent_control_fields(
    summary: dict,
    latest_enrollment: Optional[dict],
    control_enrollment: Optional[dict] = None,
) -> dict:
    """Expose Agent Control only after an explicit runtime control enrollment."""
    agent_kind = str(
        summary.get("agent_kind") or summary.get("session_source_type") or ""
    ).lower()
    validation_control_ready, managed_control_ready = (
        _managed_agent_control_config_flags(latest_enrollment)
    )
    runtime_validation_ready, runtime_managed_ready = (
        _managed_agent_control_config_flags(control_enrollment)
    )
    validation_control_ready = validation_control_ready or runtime_validation_ready
    managed_control_ready = managed_control_ready or runtime_managed_ready
    supported_agent_kind = agent_kind in AGENT_CONTROL_SUPPORTED_AGENT_KINDS
    control_configured = bool(supported_agent_kind and validation_control_ready)
    control_enabled = bool(
        summary.get("lifecycle_state") == "active" and control_configured
    )
    control_online = bool(
        control_enabled
        and summary.get("runtime_session_id")
        and summary.get("ended_at") is None
    )
    if control_online:
        control_state = AGENT_CONTROL_STATE_PLUGIN_CONNECTED
    elif control_enabled:
        control_state = AGENT_CONTROL_STATE_PLUGIN_CONFIGURED
    elif supported_agent_kind and managed_control_ready:
        control_state = AGENT_CONTROL_STATE_INSTALL_PENDING
    else:
        control_state = AGENT_CONTROL_STATE_UNSUPPORTED
    return {
        "control_feature_name": "Agent Control",
        "control_capabilities": (
            list(AGENT_CONTROL_CAPABILITIES) if control_enabled else []
        ),
        "control_state": control_state,
        "control_enabled": control_enabled,
        "control_online": control_online,
        "supports_new_session": control_enabled,
        "supports_existing_session": control_enabled,
        "supports_voice": control_enabled,
        "supports_interrupt": False,
        "supported_input_modes": (
            list(AGENT_CONTROL_INPUT_MODES) if control_enabled else []
        ),
        "supported_output_modes": (
            list(AGENT_CONTROL_OUTPUT_MODES) if control_enabled else []
        ),
    }


def _enrich_managed_agent_summary(
    db: Session, *, account_id: str, summary: dict
) -> dict:
    cli_enrollment = crud_managed_agent_enrollment.get_latest_for_agent_by_type(
        db,
        account_id=account_id,
        agent_id=summary["id"],
        enrollment_type="cli_managed_config",
    )
    control_enrollment = crud_managed_agent_enrollment.get_latest_for_agent_by_type(
        db,
        account_id=account_id,
        agent_id=summary["id"],
        enrollment_type="runtime_plugin_control",
    )
    latest_enrollment = (
        cli_enrollment
        or control_enrollment
        or crud_managed_agent_enrollment.get_latest_for_agent(
            db, account_id=account_id, agent_id=summary["id"]
        )
    )
    latest_enrollment_summary = (
        crud_managed_agent_enrollment._to_summary(latest_enrollment)
        if latest_enrollment is not None
        else None
    )
    control_enrollment_summary = (
        crud_managed_agent_enrollment._to_summary(control_enrollment)
        if control_enrollment is not None
        else None
    )
    (
        summary["mcp_proxy_configured"],
        summary["model_gateway_configured"],
        summary["onboarding_state"],
    ) = _managed_agent_onboarding_flags(latest_enrollment_summary)
    (
        summary["live_validation_supported"],
        summary["live_validation_passed"],
        summary["live_validation_status"],
        summary["last_validated_at"],
    ) = _managed_agent_live_validation_state(latest_enrollment_summary)
    summary["configured_model_alias"] = _managed_agent_configured_model_alias(
        latest_enrollment_summary
    )
    summary["configured_models"] = [
        binding.model_dump(mode="json")
        for binding in _managed_agent_configured_models(
            db,
            account_id=account_id,
            agent_id=summary["id"],
            latest_enrollment=latest_enrollment_summary,
        )
    ]
    primary_binding = next(
        (
            binding
            for binding in summary["configured_models"]
            if binding.get("is_primary")
        ),
        None,
    )
    if primary_binding and primary_binding.get("gateway_alias"):
        summary["configured_model_alias"] = primary_binding["gateway_alias"]
    summary["configured_model_id"] = _managed_agent_configured_model_id(
        db,
        account_id=account_id,
        agent_id=summary["id"],
        configured_model_alias=summary["configured_model_alias"],
    )
    if primary_binding and primary_binding.get("ai_model_id"):
        summary["configured_model_id"] = primary_binding["ai_model_id"]
    summary.update(
        _managed_agent_control_fields(
            summary,
            latest_enrollment_summary,
            control_enrollment_summary,
        )
    )
    return summary


def _build_managed_agent_detail_response(
    db: Session,
    *,
    account_id: str,
    agent_id: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    background_tasks: Optional[BackgroundTasks] = None,
) -> Optional[ManagedAgentDetailResponse]:
    if start_date and start_date.tzinfo:
        start_date = start_date.astimezone(UTC).replace(tzinfo=None)
    if end_date and end_date.tzinfo:
        end_date = end_date.astimezone(UTC).replace(tzinfo=None)
    summary = crud_managed_agent.get_summary_for_account(
        db, account_id=account_id, agent_id=agent_id
    )
    if summary is None:
        return None
    summary = _enrich_managed_agent_summary(db, account_id=account_id, summary=summary)
    aggregate = crud_managed_agent.get_usage_aggregate_for_account(
        db,
        account_id=account_id,
        agent_id=agent_id,
        start_date=start_date,
        end_date=end_date,
    )
    usage_by_model = crud_managed_agent.get_usage_by_model_for_account(
        db,
        account_id=account_id,
        agent_id=agent_id,
        start_date=start_date,
        end_date=end_date,
    )
    activity_by_server = crud_runtime_session_activity.get_server_summary_for_principal(
        db,
        account_id=account_id,
        runtime_principal_type=summary["session_source_type"],
        runtime_principal_id=summary["session_source_id"],
    )
    activity_by_tool = crud_runtime_session_activity.get_tool_summary_for_principal(
        db,
        account_id=account_id,
        runtime_principal_type=summary["session_source_type"],
        runtime_principal_id=summary["session_source_id"],
    )
    sessions = crud_runtime_session.list_account_sessions(
        db,
        account_id=account_id,
        runtime_principal_type=summary["session_source_type"],
        runtime_principal_id=summary["session_source_id"],
        min_requests=1,
        status="all",
        limit=20,
        offset=0,
    )
    RuntimeSessionExplorerService(db).schedule_missing_session_titles(
        account_id=account_id,
        rows=sessions["items"],
        background_tasks=background_tasks,
    )
    return ManagedAgentDetailResponse(
        agent=ManagedAgentSummary(**summary),
        aggregate=ManagedAgentUsageAggregate(
            session_count=aggregate["session_count"] if aggregate else 0,
            total_requests=aggregate["total_requests"] if aggregate else 0,
            successful_requests=aggregate["successful_requests"] if aggregate else 0,
            failed_requests=aggregate["failed_requests"] if aggregate else 0,
            token_usage=GatewayTokenUsage(
                prompt_tokens=aggregate["prompt_tokens"] if aggregate else 0,
                completion_tokens=aggregate["completion_tokens"] if aggregate else 0,
                total_tokens=aggregate["total_tokens"] if aggregate else 0,
            ),
            estimated_cost=aggregate["estimated_cost"] if aggregate else 0.0,
            latest_model_alias=aggregate["latest_model_alias"] if aggregate else None,
            latest_provider_name=(
                aggregate["latest_provider_name"] if aggregate else None
            ),
            last_request_at=aggregate["last_request_at"] if aggregate else None,
        ),
        usage_by_model=[
            ModelGatewayUsageService._model_row_to_schema(row) for row in usage_by_model
        ],
        activity_by_server=[
            ManagedAgentServerActivitySummary(**row) for row in activity_by_server
        ],
        activity_by_tool=[
            ManagedAgentToolActivitySummary(**row) for row in activity_by_tool
        ],
        sessions=[
            RuntimeSessionExplorerService._summary_row_to_schema(item)
            for item in sessions["items"]
        ],
        credentials=[
            ManagedAgentCredentialSummary(**item)
            for item in crud_managed_agent_credential.list_for_agent(
                db, account_id=account_id, agent_id=agent_id
            )
        ],
        enrollments=[
            ManagedAgentEnrollmentSummary(**item)
            for item in crud_managed_agent_enrollment.list_for_agent(
                db, account_id=account_id, agent_id=agent_id
            )
        ],
    )


class AccountDetailsResponse(BaseModel):
    """Account details response."""

    id: str
    organization_name: Optional[str] = None
    created_at: str
    updated_at: str


class AccountDetailsUpdate(BaseModel):
    """Account details update request."""

    organization_name: Optional[str] = None


class AccountDeletionRequest(BaseModel):
    """Account deletion request from user."""

    email: EmailStr
    username: str
    account_id: str
    org_name: Optional[str] = None
    reason: Optional[str] = None


@router.get("/account/details", response_model=AccountDetailsResponse)
async def get_account_details(
    account: Annotated[Account, Depends(get_account_for_user)],
):
    """Get current account details.

    Returns:
        Account details including organization name
    """
    return AccountDetailsResponse(
        id=str(account.id),
        organization_name=account.organization_name,
        created_at=account.created_at.isoformat(),
        updated_at=account.updated_at.isoformat(),
    )


@router.patch("/account/details", response_model=AccountDetailsResponse)
async def update_account_details(
    update_data: AccountDetailsUpdate,
    account: Annotated[Account, Depends(get_account_for_user)],
    db: Session = Depends(get_db_session),
):
    """Update current account details.

    Args:
        update_data: Account update data
        account: Current user's account
        db: Database session

    Returns:
        Updated account details
    """
    # Update account
    update_dict = update_data.model_dump(exclude_unset=True)
    updated_account = crud_account.update(db=db, db_obj=account, obj_in=update_dict)
    db.commit()
    db.refresh(updated_account)

    return AccountDetailsResponse(
        id=str(updated_account.id),
        organization_name=updated_account.organization_name,
        created_at=updated_account.created_at.isoformat(),
        updated_at=updated_account.updated_at.isoformat(),
    )


@router.get(
    "/account/gateway-usage/summary",
    response_model=AccountGatewayUsageSummaryResponse,
)
@require_permission("view_cost")
def get_account_gateway_usage_summary(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    runtime_principal_id: Optional[str] = Query(None),
    include_breakdown: bool = Query(True),
):
    """Get account-scoped model gateway usage summary."""
    return ModelGatewayUsageService(db).get_account_summary(
        account=account,
        start_date=start_date,
        end_date=end_date,
        runtime_principal_id=runtime_principal_id,
        include_breakdown=include_breakdown,
    )


@router.get(
    "/account/gateway-usage/search",
    response_model=AccountGatewayUsageSearchResponse,
)
@require_permission("view_cost")
def search_account_gateway_usage(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    query: Optional[str] = Query(None, min_length=1),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    provider_name: Optional[str] = Query(None),
    model_alias: Optional[str] = Query(None),
    flow_id: Optional[str] = Query(None),
    runtime_session_id: Optional[str] = Query(None),
    runtime_principal_id: Optional[str] = Query(None),
    api_key_id: Optional[str] = Query(None),
    session_source_type: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Search or list indexed gateway interactions for the current account."""
    return ModelGatewayUsageService(db).search_account_interactions(
        account=account,
        query=query,
        start_date=start_date,
        end_date=end_date,
        provider_name=provider_name,
        model_alias=model_alias,
        flow_id=flow_id,
        runtime_session_id=runtime_session_id,
        runtime_principal_id=runtime_principal_id,
        api_key_id=api_key_id,
        session_source_type=session_source_type,
        limit=limit,
        offset=offset,
    )


@router.get("/agents", response_model=AccountManagedAgentListResponse)
@require_permission("view_agents")
def list_account_managed_agents(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    query: Optional[str] = Query(None, min_length=1),
    agent_kind: Optional[str] = Query(None),
    last_seen_after: Optional[datetime] = Query(None),
    status: str = Query("all", pattern="^(all|active|ended)$"),
    owner_username: Optional[str] = Query(None),
    tags: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List enrolled external agents for the current account."""
    parsed_tags = None
    if tags:
        import json

        try:
            parsed_tags = json.loads(tags)
        except json.JSONDecodeError:
            # Fallback for simple key=value pairs or generic strings not properly json encoded if passed directly from some clients, although we expect valid JSON.
            pass

    result = crud_managed_agent.list_for_account(
        db,
        account_id=str(account.id),
        query=query,
        agent_kind=agent_kind,
        last_seen_after=last_seen_after,
        status=status,
        owner_username=owner_username,
        tags=parsed_tags,
        limit=limit,
        offset=offset,
    )
    return AccountManagedAgentListResponse(
        query=query,
        agent_kind=agent_kind,
        last_seen_after=last_seen_after,
        status=status,
        total=result["total"],
        limit=limit,
        offset=offset,
        items=[
            _enrich_managed_agent_summary(
                db, account_id=str(account.id), summary=dict(item)
            )
            for item in result["items"]
        ],
    )


@router.get("/agents/control", response_model=AccountManagedAgentListResponse)
@require_permission("view_agents")
def list_account_controllable_agents(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    online_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List agents that expose the Agent Control product surface."""
    result = crud_managed_agent.list_for_account(
        db,
        account_id=str(account.id),
        status="all",
        limit=limit,
        offset=offset,
    )
    items = [
        _enrich_managed_agent_summary(
            db, account_id=str(account.id), summary=dict(item)
        )
        for item in result["items"]
    ]
    items = [
        item
        for item in items
        if item["control_enabled"]
        or item.get("control_state") == AGENT_CONTROL_STATE_INSTALL_PENDING
    ]
    if online_only:
        items = [item for item in items if item["control_online"]]
    return AccountManagedAgentListResponse(
        status="active",
        total=len(items),
        limit=limit,
        offset=offset,
        items=items,
    )


class AgentNameExtractionRequest(BaseModel):
    """Request to extract an agent's name from IDENTITY.md content."""

    identity_content: str


class AgentNameExtractionResponse(BaseModel):
    """Extracted agent name."""

    name: str


@router.post("/agents/extract-name", response_model=AgentNameExtractionResponse)
@require_permission("manage_agents")
async def extract_agent_name(
    request: AgentNameExtractionRequest,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Extract agent name from IDENTITY.md content using LLM.

    If the CLI regex parsing fails, it falls back to this endpoint which uses
    the account's default configured AI model to intelligently infer the name.
    """
    import asyncio
    import litellm

    default_model = crud_ai_model.get_default_active_model(
        db, account_id=str(account.id)
    )
    if not default_model:
        all_models = crud_ai_model.get_by_account(db, account_id=str(account.id))
        if not all_models:
            raise HTTPException(status_code=400, detail="No AI models configured")
        default_model = sorted(all_models, key=lambda m: m.created_at, reverse=True)[0]

    from preloop.services.policy_generation import _PROVIDER_PREFIX

    provider = (default_model.provider_name or "openai").lower()
    prefix = _PROVIDER_PREFIX.get(provider, provider)
    litellm_model = default_model.model_identifier
    if "/" not in litellm_model:
        litellm_model = f"{prefix}/{litellm_model}"

    kwargs = {
        "model": litellm_model,
        "messages": [
            {
                "role": "system",
                "content": "You are a helpful assistant. Extract the name of the AI agent from the provided markdown identity file content. Return ONLY the agent's name as plain text, nothing else. If you cannot determine the name, return 'Unknown Agent'.",
            },
            {"role": "user", "content": request.identity_content},
        ],
        "temperature": 0.0,
        "max_tokens": 100,
    }
    if default_model.api_key:
        kwargs["api_key"] = default_model.api_key
    if default_model.api_endpoint:
        kwargs["api_base"] = default_model.api_endpoint

    def _call():
        response = litellm.completion(**kwargs)
        return response.choices[0].message.content.strip()

    try:
        name = await asyncio.to_thread(_call)
    except Exception as exc:
        logger.warning(f"Failed to extract agent name via LLM: {exc}")
        name = "Unknown Agent"

    # Remove any markdown wrapping if the LLM added it
    name = name.strip("`").strip("*").strip('"').strip("'").strip()
    return AgentNameExtractionResponse(name=name)


@router.get("/agents/{agent_id}", response_model=ManagedAgentDetailResponse)
@require_permission("view_agents")
def get_account_managed_agent(
    agent_id: str,
    account: Annotated[Account, Depends(get_account_for_user)],
    background_tasks: BackgroundTasks,
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
):
    """Return one enrolled external agent for the current account."""
    response = _build_managed_agent_detail_response(
        db,
        account_id=str(account.id),
        agent_id=agent_id,
        start_date=start_date,
        end_date=end_date,
        background_tasks=background_tasks,
    )
    if response is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Managed agent not found"
        )
    return response


@router.get(
    "/agents/{agent_id}/model-bindings",
    response_model=list[ManagedAgentModelBindingSummary],
)
@require_permission("view_agents")
async def list_account_managed_agent_model_bindings(
    agent_id: str,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """List explicit AI model bindings for one managed agent."""
    agent = crud_managed_agent.get_for_account(
        db, account_id=str(account.id), agent_id=agent_id
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Managed agent not found"
        )
    return _managed_agent_configured_models(
        db,
        account_id=str(account.id),
        agent_id=agent_id,
        latest_enrollment=None,
    )


@router.put(
    "/agents/{agent_id}/model-bindings",
    response_model=list[ManagedAgentModelBindingSummary],
)
@require_permission("manage_agents")
async def replace_account_managed_agent_model_bindings(
    agent_id: str,
    payload: ManagedAgentModelBindingSyncRequest,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Replace explicit AI model bindings for one managed agent."""
    agent = crud_managed_agent.get_for_account(
        db, account_id=str(account.id), agent_id=agent_id
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Managed agent not found"
        )

    for binding in payload.bindings:
        ai_model = crud_ai_model.get(db, id=binding.ai_model_id)
        if ai_model is None or str(ai_model.account_id) != str(account.id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"AI model {binding.ai_model_id} does not belong to the current account",
            )

    rows = crud_managed_agent_ai_model_binding.replace_for_agent(
        db,
        account_id=str(account.id),
        agent_id=agent_id,
        bindings=[binding.model_dump() for binding in payload.bindings],
        commit=True,
    )
    return [_managed_agent_binding_summary(binding) for binding in rows]


@router.get(
    "/agents/{agent_id}/governance",
    response_model=SubjectGovernanceResponse,
)
@require_permission("view_agents")
async def get_account_managed_agent_governance(
    agent_id: str,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    agent = crud_managed_agent.get_for_account(
        db, account_id=str(account.id), agent_id=agent_id
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Managed agent not found"
        )
    return SubjectGovernanceResponse(
        subject_type=SUBJECT_TYPE_MANAGED_AGENTS,
        subject_id=agent_id,
        config=SubjectGovernanceConfig.model_validate(
            get_subject_governance(
                account.meta_data or {},
                subject_type=SUBJECT_TYPE_MANAGED_AGENTS,
                subject_id=agent_id,
            )
        ),
    )


@router.put(
    "/agents/{agent_id}/governance",
    response_model=SubjectGovernanceResponse,
)
@require_permission("manage_agents")
async def update_account_managed_agent_governance(
    agent_id: str,
    payload: SubjectGovernanceConfig,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    agent = crud_managed_agent.get_for_account(
        db, account_id=str(account.id), agent_id=agent_id
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Managed agent not found"
        )
    if payload.approval_workflow_id:
        try:
            workflow_id = UUID(payload.approval_workflow_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid approval workflow id",
            )
        workflow = crud_approval_workflow.get(
            db, id=workflow_id, account_id=str(account.id)
        )
        if workflow is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Approval workflow not found in this account",
            )
    account.meta_data = set_subject_governance(
        account.meta_data or {},
        subject_type=SUBJECT_TYPE_MANAGED_AGENTS,
        subject_id=agent_id,
        config=payload.model_dump(),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    invalidate_account_governance_cache(str(account.id))
    return SubjectGovernanceResponse(
        subject_type=SUBJECT_TYPE_MANAGED_AGENTS,
        subject_id=agent_id,
        config=SubjectGovernanceConfig.model_validate(
            get_subject_governance(
                account.meta_data or {},
                subject_type=SUBJECT_TYPE_MANAGED_AGENTS,
                subject_id=agent_id,
            )
        ),
    )


@router.post(
    "/agents",
    response_model=ManagedAgentSummary,
    status_code=status.HTTP_201_CREATED,
)
@require_permission("manage_agents")
async def create_account_managed_agent(
    payload: ManagedAgentRegisterRequest,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ManagedAgentSummary:
    """Register a custom managed agent the discovery CLI cannot find.

    Creates a durable ManagedAgent row under the reserved ``custom`` source
    type with a generated ``session_source_id`` and ``lifecycle_state`` of
    ``active`` so the operator can immediately mint a gateway credential for it
    via ``POST /agents/{agent_id}/credentials``.

    Duplicate ``display_name`` values are allowed within an account: each custom
    agent is keyed by a unique generated ``session_source_id``, so two agents
    sharing a display name remain distinct registry entries. Operators may
    legitimately run several copies of the same agent, so we do not reject this.

    Args:
        payload: Display name and optional description for the new agent.
        account: Resolved account for the authenticated user.
        current_user: Authenticated active user performing the registration.
        db: Database session.

    Returns:
        The managed-agent summary for the newly registered agent.
    """
    agent = crud_managed_agent.create_custom_agent(
        db,
        account_id=account.id,
        display_name=payload.display_name,
        description=payload.description,
        owner_user_id=current_user.id,
        commit=True,
    )
    summary = crud_managed_agent.get_summary_for_account(
        db, account_id=str(account.id), agent_id=str(agent.id)
    )
    if summary is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load registered managed agent",
        )
    summary = _enrich_managed_agent_summary(
        db, account_id=str(account.id), summary=summary
    )
    emit_account_event(
        build_account_event(
            account_id=str(account.id),
            topic=ACCOUNT_TOPIC_AUDIT,
            event_type="audit_event",
            payload={
                "action": "managed_agent_registered",
                "agent_id": str(agent.id),
                "display_name": agent.display_name,
                "session_source_type": agent.session_source_type,
                "registered_by_user_id": str(current_user.id),
            },
        )
    )
    return ManagedAgentSummary(**summary)


@router.get(
    "/agents/{agent_id}/credentials",
    response_model=list[ManagedAgentCredentialSummary],
)
@require_permission("view_agents")
async def list_account_managed_agent_credentials(
    agent_id: str,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """List durable credentials for one managed agent."""
    agent = crud_managed_agent.get_for_account(
        db, account_id=str(account.id), agent_id=agent_id
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Managed agent not found"
        )
    return [
        ManagedAgentCredentialSummary(**item)
        for item in crud_managed_agent_credential.list_for_agent(
            db, account_id=str(account.id), agent_id=agent_id
        )
    ]


@router.post(
    "/agents/{agent_id}/credentials",
    response_model=ManagedAgentCredentialCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
@require_permission("manage_agents")
async def create_account_managed_agent_credential(
    agent_id: str,
    payload: ManagedAgentCredentialCreateRequest,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Create a durable credential for one managed agent."""
    agent = crud_managed_agent.get_for_account(
        db, account_id=str(account.id), agent_id=agent_id
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Managed agent not found"
        )
    if agent.lifecycle_state != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Durable credentials can only be created for active agents",
        )
    existing_names = {
        item["name"]
        for item in crud_managed_agent_credential.list_for_agent(
            db, account_id=str(account.id), agent_id=agent_id
        )
    }
    if payload.name in existing_names:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Managed agent credential with this name already exists",
        )

    expires_at = None
    if payload.expires_in_days is not None:
        expires_at = datetime.now(UTC) + timedelta(days=payload.expires_in_days)
    token_value = f"agt_{secrets.token_urlsafe(32)}"
    from preloop.api.auth.router import _normalize_runtime_session_scopes

    normalized_scopes = _normalize_runtime_session_scopes(payload.scopes)
    api_key, presented_token = crud_api_key.create_runtime_key(
        db,
        name=f"Managed Agent Credential: {agent.display_name} / {payload.name}",
        account_id=current_user.account_id,
        user_id=current_user.id,
        scopes=normalized_scopes,
        expires_at=expires_at,
        key_value=token_value,
        commit=False,
        context_data={
            "managed_agent_id": str(agent.id),
            "credential_kind": "managed_agent_durable",
            "allowed_mcp_servers": agent.managed_mcp_servers,
            "runtime_principal": {
                "type": agent.session_source_type,
                "id": agent.session_source_id,
                "name": agent.display_name,
                "user_id": str(current_user.id),
                "username": current_user.username,
            },
        },
    )
    credential = crud_managed_agent_credential.create_for_agent(
        db,
        account_id=account.id,
        agent_id=agent.id,
        api_key_id=api_key.id,
        created_by_user_id=current_user.id,
        name=payload.name,
        description=payload.description,
        scopes=normalized_scopes,
        key_prefix=api_key.key_prefix,
        commit=True,
    )
    emit_account_event(
        build_account_event(
            account_id=str(account.id),
            topic=ACCOUNT_TOPIC_AUDIT,
            event_type="audit_event",
            payload={
                "action": "managed_agent_credential_created",
                "agent_id": str(agent.id),
                "credential_id": str(credential.id),
                "credential_name": credential.name,
            },
            runtime_session_id=str(agent.runtime_session_id)
            if agent.runtime_session_id
            else None,
        )
    )
    return ManagedAgentCredentialCreateResponse(
        credential=ManagedAgentCredentialSummary(
            **crud_managed_agent_credential._row_to_summary(credential, api_key)
        ),
        token=presented_token,
    )


@router.delete(
    "/agents/{agent_id}/credentials/{credential_id}",
    response_model=ManagedAgentCredentialSummary,
)
@require_permission("manage_agents")
async def revoke_account_managed_agent_credential(
    agent_id: str,
    credential_id: str,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Revoke one durable credential for a managed agent."""
    credential = crud_managed_agent_credential.revoke_for_agent(
        db,
        account_id=str(account.id),
        agent_id=agent_id,
        credential_id=credential_id,
        reason="revoked by operator",
        commit=True,
    )
    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Managed agent credential not found",
        )
    emit_account_event(
        build_account_event(
            account_id=str(account.id),
            topic=ACCOUNT_TOPIC_AUDIT,
            event_type="audit_event",
            payload={
                "action": "managed_agent_credential_revoked",
                "agent_id": agent_id,
                "credential_id": credential_id,
            },
        )
    )
    return ManagedAgentCredentialSummary(
        **crud_managed_agent_credential._row_to_summary(credential, credential.api_key)
    )


@router.get(
    "/agents/{agent_id}/enrollments",
    response_model=list[ManagedAgentEnrollmentSummary],
)
@require_permission("view_agents")
async def list_account_managed_agent_enrollments(
    agent_id: str,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """List durable enrollment records for one managed agent."""
    agent = crud_managed_agent.get_for_account(
        db, account_id=str(account.id), agent_id=agent_id
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Managed agent not found"
        )
    return [
        ManagedAgentEnrollmentSummary(**item)
        for item in crud_managed_agent_enrollment.list_for_agent(
            db, account_id=str(account.id), agent_id=agent_id
        )
    ]


@router.post(
    "/agents/{agent_id}/enrollments",
    response_model=ManagedAgentEnrollmentSummary,
    status_code=status.HTTP_201_CREATED,
)
@require_permission("manage_agents")
async def create_account_managed_agent_enrollment(
    agent_id: str,
    payload: ManagedAgentEnrollmentCreateRequest,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Persist one enrollment record for a managed agent."""
    agent = crud_managed_agent.get_for_account(
        db, account_id=str(account.id), agent_id=agent_id
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Managed agent not found"
        )
    enrollment = crud_managed_agent_enrollment.create_for_agent(
        db,
        account_id=account.id,
        agent_id=agent.id,
        created_by_user_id=current_user.id,
        enrollment_type=payload.enrollment_type,
        adapter_key=payload.adapter_key,
        status=payload.status,
        target_config_path=payload.target_config_path,
        discovered_config=payload.discovered_config,
        managed_config=payload.managed_config,
        backup_metadata=payload.backup_metadata,
        validation_result=payload.validation_result,
        restore_available=payload.restore_available,
        last_applied_at=payload.last_applied_at,
        last_validated_at=payload.last_validated_at,
        last_restored_at=payload.last_restored_at,
        commit=True,
    )
    emit_account_event(
        build_account_event(
            account_id=str(account.id),
            topic=ACCOUNT_TOPIC_AUDIT,
            event_type="audit_event",
            payload={
                "action": "managed_agent_enrollment_created",
                "agent_id": str(agent.id),
                "enrollment_id": str(enrollment.id),
                "enrollment_type": enrollment.enrollment_type,
                "status": enrollment.status,
            },
            runtime_session_id=str(agent.runtime_session_id)
            if agent.runtime_session_id
            else None,
        )
    )
    return ManagedAgentEnrollmentSummary(
        **crud_managed_agent_enrollment._to_summary(enrollment)
    )


@router.post(
    "/agents/{agent_id}/enrollments/{enrollment_id}/validate",
    response_model=ManagedAgentEnrollmentSummary,
)
@require_permission("manage_agents")
async def validate_account_managed_agent_enrollment(
    agent_id: str,
    enrollment_id: str,
    payload: ManagedAgentEnrollmentValidateRequest,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Persist validation state for one managed-agent enrollment."""
    enrollment = crud_managed_agent_enrollment.mark_validated(
        db,
        account_id=str(account.id),
        agent_id=agent_id,
        enrollment_id=enrollment_id,
        validation_result=payload.validation_result,
        status=payload.status,
        commit=True,
    )
    if enrollment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Managed agent enrollment not found",
        )
    emit_account_event(
        build_account_event(
            account_id=str(account.id),
            topic=ACCOUNT_TOPIC_AUDIT,
            event_type="audit_event",
            payload={
                "action": "managed_agent_enrollment_validated",
                "agent_id": agent_id,
                "enrollment_id": enrollment_id,
                "status": enrollment.status,
            },
        )
    )
    return ManagedAgentEnrollmentSummary(
        **crud_managed_agent_enrollment._to_summary(enrollment)
    )


@router.post(
    "/agents/{agent_id}/enrollments/{enrollment_id}/restore",
    response_model=ManagedAgentEnrollmentSummary,
)
@require_permission("manage_agents")
async def restore_account_managed_agent_enrollment(
    agent_id: str,
    enrollment_id: str,
    payload: ManagedAgentEnrollmentRestoreRequest,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Persist restore state for one managed-agent enrollment."""
    enrollment = crud_managed_agent_enrollment.mark_restored(
        db,
        account_id=str(account.id),
        agent_id=agent_id,
        enrollment_id=enrollment_id,
        backup_metadata=payload.backup_metadata,
        validation_result=payload.validation_result,
        status=payload.status,
        commit=True,
    )
    if enrollment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Managed agent enrollment not found",
        )
    emit_account_event(
        build_account_event(
            account_id=str(account.id),
            topic=ACCOUNT_TOPIC_AUDIT,
            event_type="audit_event",
            payload={
                "action": "managed_agent_enrollment_restored",
                "agent_id": agent_id,
                "enrollment_id": enrollment_id,
                "status": enrollment.status,
            },
        )
    )
    return ManagedAgentEnrollmentSummary(
        **crud_managed_agent_enrollment._to_summary(enrollment)
    )


@router.patch("/agents/{agent_id}", response_model=ManagedAgentSummary)
@require_permission("manage_agents")
async def update_account_managed_agent(
    agent_id: str,
    update: ManagedAgentUpdateRequest,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Update managed-agent ownership or lifecycle controls."""
    agent = crud_managed_agent.get_for_account(
        db, account_id=str(account.id), agent_id=agent_id
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Managed agent not found"
        )

    set_owner = "owner_user_id" in update.model_fields_set
    set_display_name = "display_name" in update.model_fields_set
    set_tags = "tags" in update.model_fields_set
    owner_user_id = None
    if set_owner and update.owner_user_id:
        owner = crud_user.get(db, id=update.owner_user_id)
        if owner is None or str(owner.account_id) != str(account.id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Owner must belong to the current account",
            )
        owner_user_id = owner.id

    lifecycle_state = None
    if (
        "lifecycle_action" in update.model_fields_set
        and update.lifecycle_action is not None
    ):
        lifecycle_map = {
            "suspend": "suspended",
            "resume": "active",
            "decommission": "decommissioned",
            "reenroll": "active",
        }
        lifecycle_state = lifecycle_map.get(update.lifecycle_action)
        if lifecycle_state is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid lifecycle_action",
            )

    bound_runtime_session_id = (
        str(agent.runtime_session_id) if agent.runtime_session_id is not None else None
    )
    should_revoke_runtime_access = lifecycle_state in {"suspended", "decommissioned"}
    revoke_timestamp = datetime.now(UTC) if should_revoke_runtime_access else None
    updated = crud_managed_agent.update_operator_state(
        db,
        account_id=str(account.id),
        agent_id=agent_id,
        owner_user_id=owner_user_id,
        set_owner=set_owner,
        display_name=update.display_name,
        set_display_name=set_display_name,
        lifecycle_state=lifecycle_state,
        lifecycle_reason=update.reason,
        tags=update.tags,
        set_tags=set_tags,
        commit=False,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Managed agent not found"
        )
    if should_revoke_runtime_access and revoke_timestamp is not None:
        crud_api_key.deactivate_runtime_keys_for_principal(
            db,
            account_id=account.id,
            runtime_principal_type=agent.session_source_type,
            runtime_principal_id=agent.session_source_id,
            commit=False,
        )
        if bound_runtime_session_id is not None:
            crud_runtime_session.update_operator_state(
                db,
                account_id=str(account.id),
                runtime_session_id=bound_runtime_session_id,
                ended_at=revoke_timestamp,
                commit=False,
            )
    db.commit()
    db.refresh(updated)

    detail = _build_managed_agent_detail_response(
        db, account_id=str(account.id), agent_id=agent_id
    )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Managed agent not found"
        )

    emit_account_event(
        build_account_event(
            account_id=str(account.id),
            topic=ACCOUNT_TOPIC_MANAGED_AGENTS,
            event_type=(
                "managed_agent_updated"
                if updated.lifecycle_state == "active"
                else f"managed_agent_{updated.lifecycle_state}"
            ),
            payload=detail.agent.model_dump(mode="json"),
            runtime_session_id=detail.agent.runtime_session_id,
        )
    )
    emit_account_event(
        build_account_event(
            account_id=str(account.id),
            topic=ACCOUNT_TOPIC_AUDIT,
            event_type="audit_event",
            payload={
                "action": "managed_agent_updated",
                "agent_id": detail.agent.id,
                "display_name": detail.agent.display_name,
                "owner_user_id": detail.agent.owner_user_id,
                "owner_username": detail.agent.owner_username,
                "lifecycle_state": detail.agent.lifecycle_state,
                "lifecycle_reason": detail.agent.lifecycle_reason,
            },
            runtime_session_id=detail.agent.runtime_session_id,
        )
    )
    return detail.agent


@router.delete("/agents/{agent_id}", status_code=status.HTTP_200_OK)
@require_permission("manage_agents")
async def delete_account_managed_agent(
    agent_id: str,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Remove one managed-agent registry entry without touching the actual agent."""
    agent = crud_managed_agent.get_for_account(
        db, account_id=str(account.id), agent_id=agent_id
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Managed agent not found"
        )

    crud_api_key.deactivate_runtime_keys_for_principal(
        db,
        account_id=account.id,
        runtime_principal_type=agent.session_source_type,
        runtime_principal_id=agent.session_source_id,
        commit=False,
    )
    crud_api_key.deactivate_runtime_keys_for_managed_agent(
        db,
        account_id=account.id,
        managed_agent_id=str(agent.id),
        commit=False,
    )
    db.delete(agent)
    db.commit()

    emit_account_event(
        build_account_event(
            account_id=str(account.id),
            topic=ACCOUNT_TOPIC_MANAGED_AGENTS,
            event_type="managed_agent_removed",
            payload={
                "agent_id": agent_id,
                "session_source_type": agent.session_source_type,
                "session_source_id": agent.session_source_id,
            },
        )
    )
    return {"message": "Managed agent removed"}


@router.get("/runtime-sessions", response_model=AccountRuntimeSessionListResponse)
@require_permission("view_runtime_sessions")
async def list_account_runtime_sessions(
    account: Annotated[Account, Depends(get_account_for_user)],
    background_tasks: BackgroundTasks,
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    query: Optional[str] = Query(None, min_length=1),
    session_source_type: Optional[str] = Query(None),
    status: str = Query("all", pattern="^(all|active|ended)$"),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List runtime sessions for the current account."""
    return RuntimeSessionExplorerService(db).list_account_sessions(
        account=account,
        query=query,
        session_source_type=session_source_type,
        status=status,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
        background_tasks=background_tasks,
    )


@router.get(
    "/runtime-sessions/{runtime_session_id}",
    response_model=AccountRuntimeSessionDetailResponse,
)
@require_permission("view_runtime_sessions")
async def get_account_runtime_session_detail(
    runtime_session_id: str,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
):
    """Return one runtime session detail summary without heavy arrays."""
    return RuntimeSessionExplorerService(db).get_account_session_detail(
        account=account,
        runtime_session_id=runtime_session_id,
        start_date=start_date,
        end_date=end_date,
    )


@router.get(
    "/runtime-sessions/{runtime_session_id}/interactions",
    response_model=AccountGatewayUsageSearchResponse,
)
@require_permission("view_runtime_sessions")
async def get_account_session_interactions(
    runtime_session_id: str,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    interaction_query: Optional[str] = Query(None, min_length=1),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    interaction_limit: int = Query(50, ge=1, le=200),
    interaction_offset: int = Query(0, ge=0),
):
    """Paginated search across captured interactions for this session."""
    return RuntimeSessionExplorerService(db).get_account_session_interactions(
        account=account,
        runtime_session_id=runtime_session_id,
        interaction_query=interaction_query,
        start_date=start_date,
        end_date=end_date,
        interaction_limit=interaction_limit,
        interaction_offset=interaction_offset,
    )


@router.get(
    "/runtime-sessions/{runtime_session_id}/activity",
    response_model=RuntimeSessionActivityListResponse,
)
@require_permission("view_runtime_sessions")
async def get_account_session_activity_timeline(
    runtime_session_id: str,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Activity timeline overview for this session."""
    return RuntimeSessionExplorerService(db).get_account_session_activity_timeline(
        account=account,
        runtime_session_id=runtime_session_id,
    )


def _request_row_to_item(row: Any) -> RuntimeSessionRequestItem:
    """Convert an ApiUsage row into a unified-timeline request item.

    Args:
        row: One ``ApiUsage`` ORM row for a gateway request.

    Returns:
        The serialized per-request timeline item, including its tools (from
        ``meta_data.tools_meta``) and their per-tool schema token estimates.
    """
    meta = row.meta_data or {}
    tools_meta = meta.get("tools_meta") if isinstance(meta, dict) else None
    tools: list[RuntimeSessionRequestTool] = []
    tools_total = 0
    if isinstance(tools_meta, list):
        for entry in tools_meta:
            if not isinstance(entry, dict):
                continue
            schema_tokens = int(entry.get("schema_tokens_estimate") or 0)
            tools_total += schema_tokens
            tools.append(
                RuntimeSessionRequestTool(
                    name=entry.get("name"),
                    source=entry.get("source"),
                    schema_tokens_estimate=schema_tokens,
                    stripped=bool(entry.get("stripped", False)),
                )
            )
    status_code = int(row.status_code or 0)
    return RuntimeSessionRequestItem(
        id=str(row.id),
        timestamp=row.timestamp,
        model_alias=row.model_alias,
        provider_name=row.provider_name,
        status_code=status_code,
        is_error=status_code >= 400,
        finish_reason=(meta.get("finish_reason") if isinstance(meta, dict) else None),
        is_retry=bool(meta.get("is_retry", False)) if isinstance(meta, dict) else False,
        prompt_tokens=int(row.prompt_tokens or 0),
        completion_tokens=int(row.completion_tokens or 0),
        total_tokens=int(row.total_tokens or 0),
        estimated_cost=float(row.estimated_cost or 0.0),
        endpoint=row.endpoint,
        tools=tools,
        tools_total_schema_tokens=tools_total,
    )


@router.get(
    "/runtime-sessions/{runtime_session_id}/requests",
    response_model=RuntimeSessionRequestListResponse,
)
@require_permission("view_runtime_sessions")
async def list_account_runtime_session_requests(
    runtime_session_id: str,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    failed_only: bool = Query(False),
    event_ids: Optional[list[str]] = Query(None),
) -> RuntimeSessionRequestListResponse:
    """Return per-request gateway rows for one runtime session.

    This powers the unified session timeline by reading the real per-request
    ``ApiUsage`` rows (one per gateway request) rather than the sparse captured
    gateway events. Each item carries its tokens, estimated spend, status, and
    the tools it included with their per-tool schema token cost.
    """
    from preloop.models.crud.api_usage import crud_api_usage

    session = crud_runtime_session.get_account_session(
        db, account_id=str(account.id), runtime_session_id=runtime_session_id
    )
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Runtime session not found"
        )

    rows = crud_api_usage.list_session_request_rows(
        db,
        account_id=account.id,
        runtime_session_id=runtime_session_id,
        limit=limit,
        offset=offset,
        failed_only=failed_only,
        event_ids=event_ids,
    )
    total = crud_api_usage.count_session_request_rows(
        db,
        account_id=account.id,
        runtime_session_id=runtime_session_id,
        failed_only=failed_only,
        event_ids=event_ids,
    )
    failed_count = crud_api_usage.count_session_request_rows(
        db,
        account_id=account.id,
        runtime_session_id=runtime_session_id,
        failed_only=True,
    )
    items = [_request_row_to_item(row) for row in rows]
    next_offset = offset + len(items)
    return RuntimeSessionRequestListResponse(
        items=items,
        total=total,
        failed_count=failed_count,
        limit=limit,
        offset=offset,
        next_offset=next_offset if next_offset < total else None,
        has_more=next_offset < total,
    )


@router.post(
    "/runtime-sessions/{runtime_session_id}/summaries",
    response_model=RuntimeSessionSummaryInsight,
)
@require_permission("view_runtime_sessions")
async def summarize_account_runtime_session(
    runtime_session_id: str,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Summarize one runtime session without hidden inspection spend."""
    return RuntimeSessionExplorerService(db).get_account_session_summary_insight(
        account=account,
        runtime_session_id=runtime_session_id,
    )


@router.get(
    "/runtime-sessions/{runtime_session_id}/gateway-events",
)
@require_permission("view_runtime_sessions")
async def get_account_runtime_session_gateway_events(
    runtime_session_id: str,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    tail: int | None = Query(None),
    limit: int = Query(25, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    metadata_only: bool = Query(False),
) -> dict[str, Any]:
    """Return one compact page of stored gateway event metadata for a session."""
    session = crud_runtime_session.get_account_session(
        db, account_id=str(account.id), runtime_session_id=runtime_session_id
    )
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Runtime session not found"
        )

    from preloop.models.crud.runtime_session_activity import (
        crud_runtime_session_activity,
    )

    rows = crud_runtime_session_activity.list_model_gateway_calls_for_session(
        db,
        account_id=account.id,
        runtime_session_id=runtime_session_id,
        tail=tail,
        limit=limit,
        offset=offset,
        metadata_only=metadata_only,
    )
    total = crud_runtime_session_activity.count_model_gateway_calls_for_session(
        db,
        account_id=account.id,
        runtime_session_id=runtime_session_id,
    )
    page_limit = min(tail, 200) if tail else min(limit, 5000 if metadata_only else 100)

    events = [
        {
            "id": str(row.id),
            "timestamp": row.timestamp.isoformat() if row.timestamp else None,
            "type": row.activity_type,
            "payload": row.metadata_,
        }
        for row in rows
    ]
    next_offset = offset + len(events)
    return {
        "source": "database",
        "logs": events,
        "pagination": {
            "limit": page_limit,
            "offset": offset,
            "next_offset": next_offset if next_offset < total else None,
            "total": total,
            "has_more": next_offset < total,
        },
    }


@router.get(
    "/runtime-sessions/{runtime_session_id}/gateway-events/{activity_id}",
)
@require_permission("view_runtime_sessions")
async def get_account_runtime_session_gateway_event_detail(
    runtime_session_id: str,
    activity_id: str,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Return the raw, massive stored gateway event JSON detail for one runtime session activity."""
    session = crud_runtime_session.get_account_session(
        db, account_id=str(account.id), runtime_session_id=runtime_session_id
    )
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Runtime session not found"
        )

    from preloop.models.crud.runtime_session_activity import (
        crud_runtime_session_activity,
    )

    activity = crud_runtime_session_activity.get_model_gateway_call_for_session(
        db,
        account_id=account.id,
        runtime_session_id=runtime_session_id,
        activity_id=activity_id,
    )

    if activity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Runtime session activity not found",
        )

    return {
        "id": str(activity.id),
        "timestamp": activity.timestamp.isoformat() if activity.timestamp else None,
        "type": activity.activity_type,
        "payload": activity.metadata_,
    }


@router.post(
    "/runtime-sessions/{runtime_session_id}/gateway-events/{activity_id}/summary",
    response_model=RuntimeSessionInteractionSummary,
)
@require_permission("view_runtime_sessions")
async def summarize_account_runtime_session_gateway_event(
    runtime_session_id: str,
    activity_id: str,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Summarize one gateway interaction on demand using the account default model."""
    return await RuntimeSessionExplorerService(
        db
    ).summarize_account_runtime_session_interaction(
        account=account,
        runtime_session_id=runtime_session_id,
        activity_id=activity_id,
    )


@router.patch(
    "/runtime-sessions/{runtime_session_id}",
    response_model=RuntimeSessionSummary,
)
@require_permission("manage_runtime_sessions")
async def update_account_runtime_session(
    runtime_session_id: str,
    update: RuntimeSessionUpdateRequest,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Update runtime-session lifecycle controls for the current account."""
    session = crud_runtime_session.get_account_session(
        db, account_id=str(account.id), runtime_session_id=runtime_session_id
    )
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Runtime session not found"
        )

    if update.action != "end":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported runtime session action",
        )

    ended_at = session.ended_at or datetime.now(UTC)
    updated = crud_runtime_session.update_operator_state(
        db,
        account_id=str(account.id),
        runtime_session_id=runtime_session_id,
        ended_at=ended_at,
        commit=True,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Runtime session not found"
        )

    crud_api_key.deactivate_runtime_keys_for_session(
        db,
        account_id=str(account.id),
        runtime_session_id=runtime_session_id,
        commit=True,
    )

    managed_agent_summary = None
    if updated.runtime_principal_type and updated.runtime_principal_id:
        managed_agent = crud_managed_agent.clear_runtime_session_binding(
            db,
            account_id=str(account.id),
            session_source_type=updated.runtime_principal_type,
            session_source_id=updated.runtime_principal_id,
            runtime_session_id=updated.id,
            commit=True,
        )
        if managed_agent is not None:
            managed_agent_summary = crud_managed_agent.get_summary_for_account(
                db, account_id=str(account.id), agent_id=str(managed_agent.id)
            )

    summary_row = crud_runtime_session.get_account_session_summary(
        db, account_id=str(account.id), runtime_session_id=runtime_session_id
    )
    if summary_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Runtime session not found"
        )
    summary = RuntimeSessionExplorerService._summary_row_to_schema(summary_row)

    try:
        from preloop.plugins.base import get_plugin_manager

        plugin_manager = get_plugin_manager()
        audit_service = plugin_manager.get_service("audit_service")
        if audit_service:
            audit_service.log_runtime_session_event(
                db=db,
                account_id=account.id,
                runtime_session_id=updated.id,
                event="ended",
                session_source_type=updated.session_source_type,
                session_source_id=updated.session_source_id,
                session_reference=updated.session_reference,
                runtime_principal_type=updated.runtime_principal_type,
                runtime_principal_id=updated.runtime_principal_id,
                runtime_principal_name=updated.runtime_principal_name,
            )
    except Exception:
        logger.debug("Failed to audit runtime session operator action", exc_info=True)

    emit_account_event(
        build_account_event(
            account_id=str(account.id),
            topic=ACCOUNT_TOPIC_RUNTIME_SESSIONS,
            event_type="runtime_session_ended",
            payload=summary.model_dump(mode="json"),
            runtime_session_id=summary.id,
            flow_id=summary.flow_id,
            execution_id=summary.flow_execution_id,
        )
    )
    emit_account_event(
        build_account_event(
            account_id=str(account.id),
            topic=ACCOUNT_TOPIC_AUDIT,
            event_type="audit_event",
            payload={
                "action": "runtime_session_ended",
                "runtime_session_id": summary.id,
                "session_source_type": summary.session_source_type,
                "session_source_id": summary.session_source_id,
                "session_reference": summary.session_reference,
                "runtime_principal_type": summary.runtime_principal_type,
                "runtime_principal_id": summary.runtime_principal_id,
                "runtime_principal_name": summary.runtime_principal_name,
                "reason": update.reason,
            },
            runtime_session_id=summary.id,
            flow_id=summary.flow_id,
            execution_id=summary.flow_execution_id,
        )
    )
    if managed_agent_summary is not None:
        emit_account_event(
            build_account_event(
                account_id=str(account.id),
                topic=ACCOUNT_TOPIC_MANAGED_AGENTS,
                event_type="managed_agent_updated",
                payload=managed_agent_summary,
                runtime_session_id=summary.id,
                flow_id=summary.flow_id,
                execution_id=summary.flow_execution_id,
            )
        )

    return summary


@public_router.post("/account/deletion-request")
async def request_account_deletion(
    deletion_request: AccountDeletionRequest,
):
    """Public endpoint to notify admins of account deletion request.

    This endpoint is called from the public delete-account page and sends
    notifications to admins via email and configured webhooks (Slack/Mattermost).

    Args:
        deletion_request: Account deletion request details

    Returns:
        Success message
    """
    from preloop.sync.tasks import notify_admins

    # Build notification message
    subject = f"Account Deletion Request: {deletion_request.username}"

    message_parts = [
        f"User: {deletion_request.username}",
        f"Email: {deletion_request.email}",
        f"Account ID: {deletion_request.account_id}",
    ]

    if deletion_request.org_name:
        message_parts.append(f"Organization: {deletion_request.org_name}")

    if deletion_request.reason:
        message_parts.append(f"\nReason: {deletion_request.reason}")

    message = "\n".join(message_parts)

    # Build HTML version for email
    # Escape user-controlled input to prevent HTML injection
    safe_username = html.escape(deletion_request.username)
    safe_email = html.escape(deletion_request.email)
    safe_account_id = html.escape(deletion_request.account_id)

    message_html = f"""
    <h2>Account Deletion Request</h2>
    <p><strong>User:</strong> {safe_username}</p>
    <p><strong>Email:</strong> {safe_email}</p>
    <p><strong>Account ID:</strong> {safe_account_id}</p>
    """

    if deletion_request.org_name:
        safe_org_name = html.escape(deletion_request.org_name)
        message_html += f"<p><strong>Organization:</strong> {safe_org_name}</p>"

    if deletion_request.reason:
        safe_reason = html.escape(deletion_request.reason)
        message_html += f"<p><strong>Reason:</strong> {safe_reason}</p>"

    # Send notifications
    try:
        notify_admins(subject, message, message_html)
        logger.info(
            f"Account deletion request notification sent for account {deletion_request.account_id}"
        )
        return {"status": "success", "message": "Deletion request received"}
    except Exception as e:
        logger.error(
            f"Failed to send account deletion notification: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=500, detail="Failed to process deletion request"
        )


@router.get(
    "/account/telemetry/dashboard",
    response_model=DashboardTelemetryResponse,
)
async def get_dashboard_telemetry(
    account: Annotated[Account, Depends(get_account_for_user)],
    db: Session = Depends(get_db_session),
):
    """Aggregate high-level metrics for the new global dashboard."""
    from datetime import datetime, timedelta, timezone
    from preloop.models.crud.runtime_session import crud_runtime_session
    from preloop.models.crud.api_usage import crud_api_usage

    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(days=1)

    active_sessions = crud_runtime_session.count_active_sessions(
        db, account_id=str(account.id)
    )

    usage_stats = crud_api_usage.get_dashboard_usage_stats(
        db, account_id=str(account.id), since=day_ago
    )

    cost = usage_stats.get("estimated_cost", 0.0)
    total_calls = usage_stats.get("total_calls", 0)
    success_calls = usage_stats.get("success_calls", 0)

    success_rate = (success_calls / total_calls * 100.0) if total_calls > 0 else 0.0

    return DashboardTelemetryResponse(
        active_agents=active_sessions,
        total_tool_calls=total_calls,
        daily_cost=cost,
        success_rate=success_rate,
    )
