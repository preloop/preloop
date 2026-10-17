"""AgentExecutor that delivers a flow execution over Agent Control."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any, Dict, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.agents.base import AgentExecutionResult, AgentExecutor, AgentStatus
from preloop.agents.errors import AgentStartError
from preloop.utils.redaction import redact_dict
from preloop.utils.secret_scrubbing import scrub_secrets
from preloop.models.crud import (
    crud_agent_control_command,
    crud_audit_log,
    crud_flow_execution,
    crud_managed_agent,
    crud_runtime_session,
    crud_runtime_session_activity,
)
from preloop.services.agent_control_dispatch import (
    CONTROL_NEW_SESSION_UNSUPPORTED_KINDS,
    SUPPORTED_CONTROL_AGENT_KINDS,
    AgentControlDispatchError,
    agent_has_control_config,
    create_command_history_session,
    dispatch_operator_message,
)
from preloop.services.agent_control_presence import control_heartbeat_is_fresh
from preloop.services.persistent_workspace import (
    flow_workspace_metadata,
    workspace_metadata,
)
from preloop.services.runner_service import unwrap_agent_config

logger = logging.getLogger(__name__)

_NOT_CONNECTED = "persistent target {name} is not connected to Agent Control"
_SESSION_PREFIX = "control"
_CONSENT_PREFIX = "control-consent"


def parse_control_session_reference(
    session_reference: str,
) -> Tuple[str, str]:
    """Split ``control:{managed_agent_id}:{command_id}``.

    Args:
        session_reference: Executor session reference.

    Returns:
        ``(managed_agent_id, command_id)``.

    Raises:
        ValueError: When the reference is not a control session.
    """
    parts = session_reference.split(":")
    if len(parts) != 3 or parts[0] != _SESSION_PREFIX or not parts[1] or not parts[2]:
        raise ValueError(
            f"Invalid Agent Control session reference: {session_reference}"
        )
    return parts[1], parts[2]


def _target_display_name(agent: Any, fallback: str) -> str:
    name = getattr(agent, "display_name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    return fallback


def _aware_utc(value: datetime) -> datetime:
    """Return ``value`` as timezone-aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _command_expired_before_delivery(
    record: Any, *, now: Optional[datetime] = None
) -> bool:
    """True when a still-pending command has passed ``expires_at``."""
    if record is None or record.status != "pending":
        return False
    expires_at = record.expires_at
    if expires_at is None:
        return False
    current = now or datetime.now(UTC)
    return _aware_utc(expires_at) <= _aware_utc(current)


def _result_is_failure(payload: Optional[Dict[str, Any]]) -> bool:
    if not payload:
        return False
    status = str(payload.get("status") or "").strip().lower()
    if status in {"failed", "error"}:
        return True
    error = payload.get("error")
    return isinstance(error, str) and bool(error.strip()) and status != "completed"


_ANTHROPIC_PROVIDERS = frozenset({"anthropic", "claude"})

#: Persistent target kinds whose sidecar resolves ``agent_config.workspace``.
WORKSPACE_TARGET_KINDS = frozenset({"claude_code"})

# Same shape the Claude sidecar accepts for ``options.model``.
_SIDECAR_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}$")
_CLAUDE_CODE_NAME = re.compile(r"^claude code\s+(\S+)", re.IGNORECASE)


def _sidecar_model(value: Any) -> Optional[str]:
    """Return ``value`` when the sidecar will pass it as ``--model``."""
    if not isinstance(value, str):
        return None
    token = value.strip()
    if _SIDECAR_MODEL.fullmatch(token):
        return token
    return None


def _embedded_claude_code_model(display_name: Any) -> Optional[str]:
    """Alias carried in a ``Claude Code <alias>`` model row name."""
    if not isinstance(display_name, str):
        return None
    match = _CLAUDE_CODE_NAME.match(display_name.strip())
    if match is None:
        return None
    return _sidecar_model(match.group(1))


def _non_negative_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return float(value)


def control_reply_usage(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Token and cost totals from a sidecar command reply.

    Args:
        payload: Persisted ``command_result`` payload.

    Returns:
        ``input_tokens``, ``output_tokens``, ``total_tokens`` and
        ``estimated_cost`` when the reply carried them, else None.
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("usage")
    usage_in = raw if isinstance(raw, dict) else {}
    parsed: Dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        number = _non_negative_number(usage_in.get(key))
        if number is not None:
            parsed[key] = int(number)
    if "total_tokens" not in parsed and (
        "input_tokens" in parsed or "output_tokens" in parsed
    ):
        parsed["total_tokens"] = int(parsed.get("input_tokens", 0)) + int(
            parsed.get("output_tokens", 0)
        )
    cost = _non_negative_number(usage_in.get("total_cost_usd"))
    if cost is None:
        cost = _non_negative_number(payload.get("total_cost_usd"))
    if cost is not None:
        parsed["estimated_cost"] = cost
    if not parsed:
        return None
    return parsed


def settle_control_usage(
    metrics: Dict[str, Any],
    control_usage: Optional[Dict[str, Any]],
    *,
    total_tokens: int,
    estimated_cost: Optional[float],
) -> Tuple[int, Optional[float]]:
    """Choose the token and cost totals written on a finished execution.

    Gateway attribution wins when this execution recorded any request.
    Otherwise the persistent sidecar reply supplies the turn's tokens and,
    when the reply priced itself, the cost.

    Args:
        metrics: ``ExecutionMetricsService.get_execution_metrics`` result.
        control_usage: Usage from the sidecar command reply, if any.
        total_tokens: Tokens already counted on the orchestrator.
        estimated_cost: Cost already counted on the orchestrator.

    Returns:
        ``(total_tokens, estimated_cost)`` to persist.
    """
    gateway_requests = metrics.get("api_requests") or 0
    if (
        isinstance(gateway_requests, int)
        and not isinstance(gateway_requests, bool)
        and gateway_requests > 0
    ) or not isinstance(control_usage, dict):
        token_usage = metrics.get("token_usage")
        reported = (
            token_usage.get("total_tokens") if isinstance(token_usage, dict) else None
        )
        tokens = reported if isinstance(reported, int) else total_tokens
        cost = metrics.get("estimated_cost", estimated_cost)
        priced = _non_negative_number(cost)
        return tokens, estimated_cost if priced is None and cost is not None else priced

    reported_tokens = control_usage.get("total_tokens")
    tokens = (
        reported_tokens
        if isinstance(reported_tokens, int)
        and not isinstance(reported_tokens, bool)
        and reported_tokens >= 0
        else total_tokens
    )
    priced = _non_negative_number(control_usage.get("estimated_cost"))
    if priced is not None:
        return tokens, priced
    return tokens, estimated_cost


def persistent_model_name(execution_context: Dict[str, Any]) -> Optional[str]:
    """Model name a persistent Claude Code target should run the flow with.

    The flow's managed model is sent only when it is a Claude model, so a
    flow saved with an unrelated model (ignored on this path before) does
    not start failing on the host. Gateway-routed models use the gateway
    alias, which the host reaches through its onboarded gateway URL. A row
    named ``Claude Code <alias>`` still contributes that alias when the
    gateway flag did not copy it onto the execution context.

    Args:
        execution_context: Orchestrator context with the resolved model.

    Returns:
        The model alias or identifier, or None to keep the host default.
    """
    gateway = bool(execution_context.get("model_gateway_enabled"))
    alias = execution_context.get("model_gateway_model_alias") if gateway else None
    identifier = execution_context.get("model_identifier")
    display_name = execution_context.get("model_name")
    embedded = _embedded_claude_code_model(display_name)
    provider = str(execution_context.get("model_provider_name") or "").lower()
    looks_claude = any(
        isinstance(value, str) and "claude" in value.lower()
        for value in (alias, identifier, display_name, embedded)
    )
    if provider not in _ANTHROPIC_PROVIDERS and not looks_claude:
        return None
    for candidate in (alias, embedded, identifier):
        token = _sidecar_model(candidate)
        if token is not None:
            return token
    return None


def _flow_dispatch_metadata(
    execution_context: Dict[str, Any],
    *,
    timeout_seconds: Optional[int] = None,
    agent_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the send_message metadata a persistent flow attaches.

    Args:
        execution_context: Orchestrator context for this execution.
        timeout_seconds: The flow timeout, sent so the host can bound a turn.
        agent_config: The flow's persistent agent configuration. Its
            ``workspace`` names a directory on the agent host.

    Returns:
        Metadata without None values.

    Raises:
        AgentStartError: When ``agent_config.workspace`` is malformed.
    """
    metadata: Dict[str, Any] = {
        "source": "flow_execution",
        "flow_id": execution_context.get("flow_id"),
        "flow_execution_id": execution_context.get("execution_id"),
        "flow_name": execution_context.get("flow_name"),
    }
    trigger = execution_context.get("trigger_event_data")
    payload: Any = trigger
    if isinstance(trigger, dict):
        metadata["trigger_event_source"] = trigger.get("source") or trigger.get(
            "event_source"
        )
        nested = trigger.get("payload")
        payload = nested if isinstance(nested, dict) else trigger
    if isinstance(payload, dict):
        repository = payload.get("repository")
        if isinstance(repository, dict):
            repo_name = repository.get("full_name") or repository.get("name")
            if repo_name:
                metadata["repository"] = repo_name
        elif isinstance(repository, str) and repository.strip():
            metadata["repository"] = repository.strip()
        ref = payload.get("ref")
        pull = payload.get("pull_request")
        if isinstance(pull, dict):
            head = pull.get("head")
            if isinstance(head, dict):
                ref = ref or head.get("ref") or head.get("sha")
        attributes = payload.get("object_attributes")
        if isinstance(attributes, dict):
            ref = (
                ref
                or attributes.get("source_branch")
                or attributes.get("ref")
                or attributes.get("sha")
            )
        if ref:
            metadata["ref"] = ref
    resolved_timeout = timeout_seconds or execution_context.get("timeout_seconds")
    if resolved_timeout is not None:
        metadata["timeout_seconds"] = resolved_timeout
    model = persistent_model_name(execution_context)
    if model:
        metadata["model"] = model
    try:
        host_workspace = flow_workspace_metadata(agent_config or {})
    except ValueError as exc:
        raise AgentStartError(str(exc), category="runner_error") from exc
    if host_workspace is not None:
        # A named host directory replaces the managed checkout.
        metadata["workspace"] = host_workspace
        return {key: value for key, value in metadata.items() if value is not None}
    git_config = execution_context.get("git_clone_config")
    try:
        metadata["workspace"] = workspace_metadata(
            git_clone_config=git_config,
            trigger_event_data=execution_context.get("trigger_event_data"),
        )
    except Exception:
        logger.warning(
            "workspace metadata failed; sending clone_less",
            exc_info=True,
        )
        metadata["workspace"] = {"mode": "clone_less"}
    return {key: value for key, value in metadata.items() if value is not None}


class AgentControlExecutor(AgentExecutor):
    """Deliver one flow execution as an audited Agent Control send_message."""

    supports_confirmation_nudge = False
    supports_inplace_completion_nudge = False

    def __init__(
        self,
        agent_type: str,
        config: Dict[str, Any],
        *,
        db: Session,
        account_id: Any,
        flow: Any = None,
        execution: Any = None,
    ) -> None:
        unwrapped = unwrap_agent_config(config)
        if isinstance(unwrapped, dict):
            config = unwrapped
        super().__init__(agent_type, config)
        self.db = db
        self.account_id = account_id
        self.flow = flow
        self.execution = execution
        self._consent_queue_context: Optional[Dict[str, Any]] = None
        self._consent_command_reference: Optional[str] = None

    async def cleanup(self) -> None:
        """Persistent dispatch has no hosted runtime to tear down."""
        return None

    def _account_id(self, execution_context: Dict[str, Any]) -> str:
        account_id = (
            self.account_id
            or execution_context.get("account_id")
            or getattr(self.flow, "account_id", None)
            or getattr(self.execution, "account_id", None)
        )
        if account_id is None:
            raise AgentStartError(
                "persistent target is missing an account",
                category="runner_error",
            )
        return str(account_id)

    def _execution_id(self, execution_context: Dict[str, Any]) -> Optional[UUID]:
        raw = execution_context.get("execution_id") or getattr(
            self.execution, "id", None
        )
        if raw is None:
            return None
        return raw if isinstance(raw, UUID) else UUID(str(raw))

    def _timeout_seconds(self) -> Optional[int]:
        configured = getattr(self.flow, "timeout_seconds", None)
        try:
            return int(configured) if configured is not None else None
        except (TypeError, ValueError):
            return None

    def _raise_not_connected(self, agent: Any, target_id: str) -> None:
        name = _target_display_name(agent, target_id)
        raise AgentStartError(
            _NOT_CONNECTED.format(name=name),
            category="runner_error",
        )

    def _resolve_target(self, execution_context: Dict[str, Any]) -> Tuple[Any, str]:
        target_id = str(self.config.get("target_agent_id") or "").strip()
        if not target_id:
            raise AgentStartError(
                "persistent target is missing; the flow will not start an "
                "ephemeral run",
                category="runner_error",
            )
        account_id = self._account_id(execution_context)
        agent = crud_managed_agent.get_visible_target(
            self.db, account_id=account_id, agent_id=target_id
        )
        if agent is None:
            raise AgentStartError(
                f"persistent target {target_id} was not found in this account",
                category="runner_error",
            )
        if agent.lifecycle_state != "active":
            raise AgentStartError(
                f"persistent target {_target_display_name(agent, target_id)} "
                "is not active",
                category="runner_error",
            )
        agent_kind = str(
            getattr(agent, "agent_kind", None)
            or getattr(agent, "session_source_type", None)
            or ""
        ).lower()
        if agent_kind not in SUPPORTED_CONTROL_AGENT_KINDS:
            self._raise_not_connected(agent, target_id)
        if agent_kind in CONTROL_NEW_SESSION_UNSUPPORTED_KINDS:
            raise AgentStartError(
                f"persistent target {_target_display_name(agent, target_id)} "
                "supports text messages to active sessions only",
                category="runner_error",
            )
        if not agent_has_control_config(
            self.db, account_id=str(agent.account_id), agent=agent
        ):
            self._raise_not_connected(agent, target_id)
        if not control_heartbeat_is_fresh(agent.control_last_heartbeat_at):
            self._raise_not_connected(agent, target_id)
        return agent, target_id

    async def _flow_owner_consent(
        self, agent: Any, execution_context: Dict[str, Any], *, consume: bool
    ) -> bool:
        """Gate automated new sessions without guessing a human actor."""
        mode = getattr(agent, "others_may_start_sessions", "ask_me")
        execution_id = self._execution_id(execution_context)
        if mode == "allow":
            if consume:
                crud_audit_log.log_action(
                    self.db,
                    account_id=agent.account_id,
                    action="agent_flow_session_consent",
                    resource_type="managed_agent",
                    resource_id=str(agent.id),
                    status="success",
                    details={"decision": "allow", "execution_id": str(execution_id)},
                )
            return True
        owner_id = getattr(agent, "owner_user_id", None)
        if mode != "ask_me" or owner_id is None or execution_id is None:
            crud_audit_log.log_action(
                self.db,
                account_id=agent.account_id,
                action="agent_flow_session_consent",
                resource_type="managed_agent",
                resource_id=str(agent.id),
                status="denied",
                details={"decision": "deny", "execution_id": str(execution_id)},
            )
            raise AgentStartError(
                "The agent owner does not permit this flow to start a session",
                category="runner_error",
            )
        from preloop.models.crud.owner_consent import consume as consume_approval
        from preloop.services.owner_consent import (
            check_owner_consent,
            notify_owner_consent,
        )

        try:
            consent = check_owner_consent(
                self.db,
                account_id=agent.account_id,
                owner_user_id=owner_id,
                subject_id=str(agent.id),
                action="agent_flow_session",
                request_key=f"{execution_id}:{agent.id}:{owner_id}",
                details={"execution_id": str(execution_id), "agent_id": str(agent.id)},
                execution_id=str(execution_id),
                managed_agent_id=agent.id,
            )
        except ValueError as exc:
            raise AgentStartError(
                "Agent session consent could not be verified", category="runner_error"
            ) from exc
        if consent.created:
            try:
                await asyncio.wait_for(
                    notify_owner_consent(consent.approval_id), timeout=25
                )
            except Exception:
                logger.warning("Agent flow owner notification deferred", exc_info=True)
        if consent.status == "pending":
            return False
        if consent.status != "approved" or (
            consume and not consume_approval(self.db, approval_id=consent.approval_id)
        ):
            raise AgentStartError(
                "Agent owner did not authorize this flow session",
                category="runner_error",
            )
        if consume:
            crud_audit_log.log_action(
                self.db,
                account_id=agent.account_id,
                action="agent_flow_session_consent",
                resource_type="managed_agent",
                resource_id=str(agent.id),
                status="success",
                details={"decision": "approved", "execution_id": str(execution_id)},
            )
        return True

    async def _consent_queue_status(self, session_reference: str) -> AgentStatus:
        """Resume an approved flow once; durable binding wins after delivery."""
        if self._consent_command_reference is not None:
            return await self.get_status(self._consent_command_reference)
        execution_id = UUID(session_reference.split(":")[-1])
        execution = crud_flow_execution.get(self.db, id=execution_id, refresh=True)
        if execution is None:
            return AgentStatus.FAILED
        if execution.status in {"FAILED", "TIMEOUT", "CANCELLED", "STOPPED"}:
            return (
                AgentStatus.STOPPED
                if execution.status in {"CANCELLED", "STOPPED"}
                else AgentStatus.FAILED
            )
        bound = getattr(execution, "agent_session_reference", None)
        if isinstance(bound, str) and bound.startswith(f"{_SESSION_PREFIX}:"):
            self._consent_command_reference = bound
            return await self.get_status(bound)
        stop_request = crud_flow_execution.get_stop_request(
            self.db, execution_id=execution_id
        )
        if isinstance(stop_request, dict):
            return AgentStatus.STOPPED
        target_id = session_reference.split(":")[1]
        agent = crud_managed_agent.get_visible_target(
            self.db, account_id=str(self.account_id), agent_id=target_id
        )
        if agent is None or agent.lifecycle_state != "active":
            return AgentStatus.FAILED
        context = self._consent_queue_context or {"execution_id": str(execution_id)}
        try:
            approved = await self._flow_owner_consent(agent, context, consume=False)
            if not approved:
                return AgentStatus.PENDING
            if self._consent_queue_context is None:
                raise AgentStartError(
                    "Restart this flow to rebuild its temporary runtime credentials",
                    category="runner_error",
                )
            reference = await self.start(self._consent_queue_context)
        except AgentStartError as exc:
            from preloop.models.schemas.flow_execution import FlowExecutionUpdate

            crud_flow_execution.update(
                self.db,
                db_obj=execution,
                obj_in=FlowExecutionUpdate(
                    status="FAILED", end_time=datetime.now(UTC), error_message=str(exc)
                ),
            )
            return AgentStatus.FAILED
        if reference.startswith(f"{_CONSENT_PREFIX}:"):
            return AgentStatus.PENDING
        self._consent_command_reference = reference
        self._consent_queue_context = None
        return AgentStatus.STARTING

    async def start(self, execution_context: Dict[str, Any]) -> str:
        """Dispatch the rendered flow prompt to the target managed agent.

        Args:
            execution_context: Orchestrator context. Uses the already-rendered
                ``prompt`` string; this executor does not re-render templates.

        Returns:
            Session reference ``control:{managed_agent_id}:{command_id}``.

        Raises:
            AgentStartError: When the target cannot receive Agent Control
                commands. Never falls back to an ephemeral container.
        """
        agent, target_id = self._resolve_target(execution_context)
        prompt = str(execution_context.get("prompt") or "").strip()
        if not prompt:
            raise AgentStartError(
                "persistent flow execution is missing a rendered prompt",
                category="runner_error",
            )
        dispatch_context = execution_context
        # Copy the flow clone config only when the context omitted it.
        # A confirmation nudge sets the key to None on purpose so the
        # nudge does not repeat the original checkout. That path does not
        # reach this executor while supports_confirmation_nudge is False.
        if "git_clone_config" not in execution_context and self.flow is not None:
            flow_git = getattr(self.flow, "git_clone_config", None)
            if flow_git is not None:
                dispatch_context = {
                    **execution_context,
                    "git_clone_config": flow_git,
                }
        agent_kind = str(
            getattr(agent, "agent_kind", None)
            or getattr(agent, "session_source_type", None)
            or ""
        ).lower()
        if (
            self.config.get("workspace") is not None
            and agent_kind not in WORKSPACE_TARGET_KINDS
        ):
            # Other sidecars ignore an authorized directory and would run
            # in their default directory instead of the one the flow names.
            raise AgentStartError(
                "agent_config.workspace is supported only on Claude Code "
                f"targets; {_target_display_name(agent, target_id)} is "
                f"{agent_kind or 'unknown'}",
                category="runner_error",
            )
        metadata = _flow_dispatch_metadata(
            dispatch_context,
            timeout_seconds=self._timeout_seconds(),
            agent_config=self.config,
        )
        from preloop.services.flow_execution_limits import parse_execution_limits

        limits = parse_execution_limits(self.config).as_dict()
        if limits:
            metadata["run_limits"] = {
                **limits,
                "max_duration_seconds": self._timeout_seconds(),
                "max_history_chars": min(
                    limits.get("max_total_tokens", 32768) * 4, 64000
                ),
            }
        if str(agent.account_id) != self._account_id(execution_context):
            metadata["gateway"] = {
                "api_key": dispatch_context.get("model_gateway_token"),
                "base_url": dispatch_context.get("model_gateway_url"),
                "model": dispatch_context.get("model_gateway_model_alias"),
            }
        trigger = dispatch_context.get("trigger_event_data")
        employee = trigger.get("employee") if isinstance(trigger, dict) else None
        if isinstance(employee, dict):
            # Intake owns this identity; payload text cannot redirect a task.
            if str(employee.get("managed_agent_id")) != str(agent.id):
                raise AgentStartError(
                    "Employee task belongs to another agent", category="runner_error"
                )
            metadata["employee_task_key"] = employee.get("task_key")
            gateway = {
                "api_key": dispatch_context.get("model_gateway_token"),
                "base_url": dispatch_context.get("model_gateway_url"),
                "model": dispatch_context.get("model_gateway_model_alias"),
            }
            if not dispatch_context.get("model_gateway_enabled") or not all(
                gateway.values()
            ):
                raise AgentStartError(
                    "Employee tasks require an execution-scoped model gateway",
                    category="runner_error",
                )
            from preloop.config import settings

            gateway["api_url"] = settings.preloop_url
            metadata["gateway"] = gateway
            metadata["mcp_enabled"] = bool(
                dispatch_context.get("allowed_mcp_servers")
                or dispatch_context.get("allowed_mcp_tools")
            )
            metadata["run_limits"]["timeout_seconds"] = self._timeout_seconds()
        execution_id = self._execution_id(execution_context)
        if execution_id is not None and not crud_flow_execution.admit_runtime_start(
            self.db, execution_id=execution_id, commit=False
        ):
            raise AgentStartError("Flow execution is stopped", category="runner_error")
        if not await self._flow_owner_consent(agent, execution_context, consume=True):
            from preloop.models.schemas.flow_execution import FlowExecutionUpdate

            self._consent_queue_context = dict(execution_context)
            reference = f"{_CONSENT_PREFIX}:{agent.id}:{execution_id}"
            execution = crud_flow_execution.get(self.db, id=execution_id)
            if execution is not None:
                crud_flow_execution.update(
                    self.db,
                    db_obj=execution,
                    obj_in=FlowExecutionUpdate(
                        agent_session_reference=reference,
                        error_message="Waiting for agent owner",
                    ),
                )
            return reference
        # Consuming approval commits its own transaction. Re-admit under the
        # account halt lock immediately before creating/delivering a command.
        if execution_id is not None and not crud_flow_execution.admit_runtime_start(
            self.db, execution_id=execution_id, commit=False
        ):
            raise AgentStartError("Flow execution is stopped", category="runner_error")
        try:
            dispatched = await dispatch_operator_message(
                self.db,
                managed_agent=agent,
                text=prompt,
                metadata=metadata,
                start_new_session=True,
                source="flow_execution",
                input_mode="text",
                session_mode="new",
                require_delivery=True,
                consuming_account_id=self._account_id(execution_context),
            )
        except AgentControlDispatchError as exc:
            name = _target_display_name(agent, target_id)
            raise AgentStartError(
                f"{_NOT_CONNECTED.format(name=name)}: {exc}",
                category="runner_error",
            ) from exc
        reference = f"{_SESSION_PREFIX}:{agent.id}:{dispatched.command_id}"
        try:
            if getattr(dispatched, "history_session_id", None) is not None:
                history_session = crud_runtime_session.get_account_session(
                    self.db,
                    account_id=self._account_id(execution_context),
                    runtime_session_id=str(dispatched.history_session_id),
                )
            else:
                history_session = create_command_history_session(
                    self.db,
                    agent=agent,
                    start_new_session=True,
                )
            if history_session is not None:
                crud_runtime_session_activity.log_agent_control_message(
                    self.db,
                    account_id=self._account_id(execution_context),
                    runtime_session_id=history_session.id,
                    message=prompt,
                    status="delivered" if dispatched.local_delivery else "queued",
                    metadata={
                        "command_id": dispatched.command_id,
                        "managed_agent_id": str(agent.id),
                        "agent_name": agent.display_name,
                        "input_mode": "text",
                        "session_mode": "new",
                        "start_new_session": True,
                        "source_metadata": redact_dict(metadata),
                        "local_delivery": dispatched.local_delivery,
                        "published": dispatched.subject is not None,
                        "subject": dispatched.subject,
                    },
                )
            execution_id = self._execution_id(execution_context)
            if execution_id is not None:
                crud_flow_execution.bind_agent_control_command(
                    self.db,
                    execution_id=execution_id,
                    command_id=dispatched.command_id,
                    managed_agent_id=agent.id,
                    runtime_session_id=agent.runtime_session_id,
                    history_session_id=(
                        history_session.id if history_session is not None else None
                    ),
                    session_reference=reference,
                    commit=True,
                )
        except Exception:
            logger.exception(
                "Failed to bind persistent flow command %s after delivery; "
                "the execution remains observable via %s",
                dispatched.command_id,
                reference,
            )
        return reference

    def _load_command(self, session_reference: str) -> Optional[Any]:
        if session_reference.startswith(f"{_CONSENT_PREFIX}:"):
            if self._consent_command_reference is None:
                execution = crud_flow_execution.get(
                    self.db, id=UUID(session_reference.split(":")[-1]), refresh=True
                )
                bound = getattr(execution, "agent_session_reference", None)
                if isinstance(bound, str) and bound.startswith(f"{_SESSION_PREFIX}:"):
                    self._consent_command_reference = bound
            if self._consent_command_reference is None:
                return None
            session_reference = self._consent_command_reference
        try:
            managed_agent_id, command_id = parse_control_session_reference(
                session_reference
            )
        except ValueError:
            return None
        account_id = str(
            self.account_id
            or getattr(self.flow, "account_id", None)
            or getattr(self.execution, "account_id", None)
            or ""
        )
        if not account_id:
            return None
        own = crud_agent_control_command.get_by_command_id(
            self.db,
            account_id=account_id,
            command_id=command_id,
            managed_agent_id=managed_agent_id,
        )
        return (
            own
            if own is not None
            else crud_agent_control_command.get_for_consumer(
                self.db,
                account_id=account_id,
                command_id=command_id,
                managed_agent_id=managed_agent_id,
            )
        )

    def _binding(self, session_reference: str) -> Dict[str, Any]:
        execution = self.execution
        execution_id = getattr(execution, "id", None)
        if execution_id is not None:
            execution = crud_flow_execution.get(self.db, id=execution_id) or execution
        details = getattr(execution, "trigger_event_details", None) or {}
        binding = details.get("_agent_control") if isinstance(details, dict) else None
        if isinstance(binding, dict):
            return binding
        try:
            managed_agent_id, command_id = parse_control_session_reference(
                session_reference
            )
        except ValueError:
            return {}
        return {
            "managed_agent_id": managed_agent_id,
            "command_id": command_id,
        }

    async def get_status(self, session_reference: str) -> AgentStatus:
        """Map the bound command row onto the executor status enum."""
        if session_reference.startswith(f"{_CONSENT_PREFIX}:"):
            return await self._consent_queue_status(session_reference)
        record = self._load_command(session_reference)
        if record is None:
            return AgentStatus.FAILED
        now = datetime.now(UTC)
        if _command_expired_before_delivery(record, now=now):
            return AgentStatus.FAILED
        result = crud_agent_control_command.command_result_payload(record)
        if record.status in {"pending", "delivered"}:
            return AgentStatus.RUNNING
        if record.status == "acked":
            if result:
                return (
                    AgentStatus.FAILED
                    if _result_is_failure(result)
                    else AgentStatus.SUCCEEDED
                )
            return AgentStatus.RUNNING
        if record.status in {"failed", "expired"}:
            return AgentStatus.FAILED
        if record.status == "cancelled":
            return AgentStatus.STOPPED
        return AgentStatus.RUNNING

    async def get_result(self, session_reference: str) -> AgentExecutionResult:
        """Return the runtime reply as the execution output."""
        status = await self.get_status(session_reference)
        record = self._load_command(session_reference)
        payload = crud_agent_control_command.command_result_payload(record)
        output: Optional[str] = None
        error_message: Optional[str] = None
        if payload:
            reply = payload.get("reply_text")
            result = payload.get("result")
            if isinstance(reply, str) and reply.strip():
                output = reply.strip()
            elif isinstance(result, str) and result.strip():
                output = result.strip()
            elif result is not None and not isinstance(result, (dict, list)):
                output = str(result)
            error = payload.get("error")
            if isinstance(error, str) and error.strip():
                error_message = error.strip()
        if status == AgentStatus.FAILED and not error_message:
            if _command_expired_before_delivery(record):
                error_message = "Agent Control command expired before delivery"
            elif record is not None and record.last_error:
                error_message = record.last_error
            elif record is not None and record.status == "expired":
                error_message = "Agent Control command expired"
        return AgentExecutionResult(
            status=status,
            session_reference=session_reference,
            output_summary=output,
            error_message=error_message,
            usage=control_reply_usage(payload),
        )

    async def get_logs(
        self, session_reference: str, tail: int | None = None
    ) -> list[str]:
        """Return command lifecycle lines plus session activity since dispatch."""
        record = self._load_command(session_reference)
        lines: list[str] = []
        if record is None:
            return lines
        created = record.created_at
        if created is not None:
            lines.append(f"[agent_control] queued at {created.isoformat()}")
        if record.delivered_at is not None:
            lines.append(
                f"[agent_control] delivered at {record.delivered_at.isoformat()}"
            )
        if record.acked_at is not None:
            lines.append(f"[agent_control] acked at {record.acked_at.isoformat()}")
        payload = crud_agent_control_command.command_result_payload(record)
        if payload:
            result_status = payload.get("status") or (
                "failed" if record.status == "failed" else "completed"
            )
            detail = payload.get("error") or payload.get("reply_text")
            suffix = f": {detail}" if isinstance(detail, str) and detail else ""
            lines.append(f"[agent_control] result: {result_status}{suffix}")
        elif record.status == "failed":
            reason = record.last_error or "failed"
            lines.append(f"[agent_control] result: failed ({reason})")
        elif record.status == "expired":
            lines.append("[agent_control] result: expired")
        session_id = record.runtime_session_id
        binding = self._binding(session_reference)
        history_id = binding.get("history_session_id")
        account_id = str(
            getattr(record, "consuming_account_id", None) or record.account_id
        )
        for runtime_session_id in dict.fromkeys(
            str(value) for value in (history_id, session_id) if value is not None
        ):
            if runtime_session_id is None:
                continue
            activities = crud_runtime_session_activity.list_for_runtime_session(
                self.db,
                account_id=account_id,
                runtime_session_id=str(runtime_session_id),
                limit=200,
            )
            since = created
            chronological = list(reversed(activities))
            live_shared = (
                session_id is not None
                and str(runtime_session_id) == str(session_id)
                and (history_id is None or str(history_id) != str(session_id))
            )
            for activity in chronological:
                if live_shared:
                    meta = getattr(activity, "metadata_", None) or {}
                    command_id = (
                        meta.get("command_id") if isinstance(meta, dict) else None
                    )
                    if str(command_id or "") != str(record.command_id):
                        continue
                stamped = activity.timestamp
                if since is not None and stamped is not None:
                    activity_at = stamped
                    created_at = since
                    if activity_at.tzinfo is None:
                        activity_at = activity_at.replace(tzinfo=UTC)
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=UTC)
                    if activity_at < created_at:
                        continue
                summary = activity.summary or activity.activity_type
                lines.append(f"[agent_control] {summary}")
        lines = [(scrub_secrets(line) or "")[:2000] for line in lines]
        if tail is not None and tail >= 0:
            return lines[-tail:] if tail else []
        return lines

    async def stop(self, session_reference: str) -> None:
        """Interrupt the command-owned session without targeting unrelated work.

        Native runtime IDs come from the persisted command result. While a
        command is still running, the runtime resolves target_command_id to
        its owned task. Failure leaves the command visibly non-terminal.
        """
        if (
            session_reference.startswith(f"{_CONSENT_PREFIX}:")
            and self._load_command(session_reference) is None
        ):
            from preloop.models.schemas.flow_execution import FlowExecutionUpdate

            execution_id = UUID(session_reference.split(":")[-1])
            execution = crud_flow_execution.get(self.db, id=execution_id, refresh=True)
            if execution is not None:
                crud_flow_execution.update(
                    self.db,
                    db_obj=execution,
                    obj_in=FlowExecutionUpdate(
                        status="STOPPED", end_time=datetime.now(UTC)
                    ),
                )
            self._consent_queue_context = None
            return
        record = self._load_command(session_reference)
        binding = self._binding(session_reference)
        # `_binding()` already parses a missing `_agent_control` dict. Keep
        # this fallback so a partial binding without managed_agent_id still
        # interrupts from the session reference.
        managed_agent_id = binding.get("managed_agent_id")
        if not managed_agent_id:
            try:
                managed_agent_id, _ = parse_control_session_reference(session_reference)
            except ValueError:
                managed_agent_id = None
        account_id = str(
            getattr(record, "account_id", None)
            or self.account_id
            or getattr(self.flow, "account_id", None)
            or ""
        )
        agent = None
        if managed_agent_id and account_id:
            agent = crud_managed_agent.get_for_account(
                self.db,
                account_id=account_id,
                agent_id=str(managed_agent_id),
            )
        interrupted = False
        if agent is not None:
            try:
                await dispatch_operator_message(
                    self.db,
                    managed_agent=agent,
                    text="Stop this flow execution.",
                    metadata={
                        "source": "flow_execution",
                        "target_command_id": getattr(record, "command_id", None),
                        "flow_execution_id": str(
                            getattr(self.execution, "id", "")
                            or binding.get("command_id")
                            or ""
                        ),
                    },
                    start_new_session=False,
                    target_session_id=None,
                    source="flow_execution",
                    interrupt=True,
                    session_mode="existing",
                    session_identity={
                        "session_reference": (
                            (
                                crud_agent_control_command.command_result_payload(
                                    record
                                )
                                or {}
                            ).get("native_session_id")
                            or (
                                crud_agent_control_command.command_result_payload(
                                    record
                                )
                                or {}
                            ).get("session_id")
                            or ""
                        ),
                    },
                    require_delivery=True,
                )
                interrupted = True
            except AgentControlDispatchError:
                logger.warning(
                    "Failed to interrupt persistent flow command %s; "
                    "leaving the command non-terminal",
                    session_reference,
                    exc_info=True,
                )
        if interrupted and record is not None:
            crud_agent_control_command.mark_terminal_result(
                self.db,
                account_id=record.account_id,
                managed_agent_id=record.managed_agent_id,
                command_id=record.command_id,
                result_payload={"status": "failed", "error": "stopped"},
                failed=True,
                error="stopped",
                commit=True,
            )

    async def is_stopped(self, session_reference: str) -> bool:
        """True after stop has marked the bound command failed."""
        if (
            session_reference.startswith(f"{_CONSENT_PREFIX}:")
            and self._load_command(session_reference) is None
        ):
            execution = crud_flow_execution.get(
                self.db, id=UUID(session_reference.split(":")[-1]), refresh=True
            )
            return execution is not None and execution.status == "STOPPED"
        record = self._load_command(session_reference)
        if record is None:
            return False
        payload = crud_agent_control_command.command_result_payload(record)
        if record.status == "failed" and (
            record.last_error == "stopped" or (payload or {}).get("error") == "stopped"
        ):
            return True
        return False

    async def stream_logs(self, session_reference: str):
        """Yield command lifecycle lines until the bound command is terminal.

        Args:
            session_reference: ``control:{managed_agent_id}:{command_id}``.

        Yields:
            Log lines already formatted by :meth:`get_logs`.
        """
        seen = 0
        while True:
            lines = await self.get_logs(session_reference)
            for line in lines[seen:]:
                yield line
            seen = len(lines)
            status = await self.get_status(session_reference)
            if status in {
                AgentStatus.SUCCEEDED,
                AgentStatus.FAILED,
                AgentStatus.STOPPED,
            }:
                return
            await asyncio.sleep(2)
