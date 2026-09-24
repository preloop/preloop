"""AgentExecutor that delivers a flow execution over Agent Control."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Dict, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.agents.base import AgentExecutionResult, AgentExecutor, AgentStatus
from preloop.agents.errors import AgentStartError
from preloop.models.crud import (
    crud_agent_control_command,
    crud_flow_execution,
    crud_managed_agent,
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
from preloop.services.persistent_workspace import workspace_metadata
from preloop.services.runner_service import unwrap_agent_config

logger = logging.getLogger(__name__)

_NOT_CONNECTED = "persistent target {name} is not connected to Agent Control"
_SESSION_PREFIX = "control"


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


def _flow_dispatch_metadata(
    execution_context: Dict[str, Any],
    *,
    timeout_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Build the send_message metadata a persistent flow attaches."""
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
        agent = crud_managed_agent.get_for_account(
            self.db,
            account_id=account_id,
            agent_id=target_id,
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
        if not agent_has_control_config(self.db, account_id=account_id, agent=agent):
            self._raise_not_connected(agent, target_id)
        if not control_heartbeat_is_fresh(agent.control_last_heartbeat_at):
            self._raise_not_connected(agent, target_id)
        return agent, target_id

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
        metadata = _flow_dispatch_metadata(
            dispatch_context,
            timeout_seconds=self._timeout_seconds(),
        )
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
            )
        except AgentControlDispatchError as exc:
            name = _target_display_name(agent, target_id)
            raise AgentStartError(
                f"{_NOT_CONNECTED.format(name=name)}: {exc}",
                category="runner_error",
            ) from exc
        reference = f"{_SESSION_PREFIX}:{agent.id}:{dispatched.command_id}"
        try:
            history_session = create_command_history_session(
                self.db,
                agent=agent,
                start_new_session=True,
            )
            if history_session is not None:
                crud_runtime_session_activity.log_agent_control_message(
                    self.db,
                    account_id=agent.account_id,
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
                        "source_metadata": metadata,
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
        return crud_agent_control_command.get_by_command_id(
            self.db,
            account_id=account_id,
            command_id=command_id,
            managed_agent_id=managed_agent_id,
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
            lines.append(f"[agent_control] result: {result_status}")
        elif record.status == "failed":
            reason = record.last_error or "failed"
            lines.append(f"[agent_control] result: failed ({reason})")
        elif record.status == "expired":
            lines.append("[agent_control] result: expired")
        session_id = record.runtime_session_id
        binding = self._binding(session_reference)
        history_id = binding.get("history_session_id")
        account_id = str(record.account_id)
        for runtime_session_id in (history_id, session_id):
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
        if tail is not None and tail >= 0:
            return lines[-tail:]
        return lines

    async def stop(self, session_reference: str) -> None:
        """Interrupt the agent's current session if delivery succeeds.

        Start opens a plugin-owned session the backend never learns the native
        id of, so stop does not target a tracking UUID. The interrupt uses
        ``session_mode=current`` (the agent's current session, which may not
        be this flow if another turn started). A failed interrupt leaves the
        command non-terminal so the operator can see the remote session is
        still live.
        """
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
                    session_mode="current",
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
