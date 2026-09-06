"""AgentExecutor that leases work to a self-hosted CLI runner."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud import crud_flow, crud_flow_execution, crud_flow_execution_log
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.services.flow_runtime_token import create_flow_runtime_token
from preloop.services.runner_service import (
    DEFAULT_QUEUE_TIMEOUT,
    lease_job,
    mark_queued_or_fail,
)

from .base import AgentExecutionResult, AgentExecutor, AgentStatus
from .images import agent_config_has_image, default_agent_image

logger = logging.getLogger(__name__)


class RemoteRunnerExecutor(AgentExecutor):
    """Does not start a hosted container. Jobs wait for a matching runner."""

    # Runner WebSocket handlers already persist and publish log lines.
    streams_logs_externally = True

    def __init__(
        self,
        agent_type: str,
        config: Dict[str, Any],
        *,
        db: Session,
        pool: str,
        account_id: UUID,
        flow: Any = None,
        execution: Any = None,
    ):
        super().__init__(agent_type, config)
        self.db = db
        self.pool = pool
        self.account_id = account_id
        self.flow = flow
        self.execution = execution

    async def start(self, execution_context: Dict[str, Any]) -> str:
        execution_id = UUID(str(execution_context["execution_id"]))
        payload = self._lease_payload(
            execution_id=execution_id,
            flow_id=execution_context.get("flow_id"),
            prompt=execution_context.get("prompt"),
            execution_context=execution_context,
        )
        runner = lease_job(
            self.db,
            account_id=self.account_id,
            pool=self.pool,
            execution_id=execution_id,
            payload=payload,
        )
        execution = crud_flow_execution.get(self.db, id=execution_id)
        if runner:
            if execution:
                execution.runner_id = runner.id
                execution.agent_session_reference = f"runner:{runner.id}:{execution_id}"
                self.db.add(execution)
                self.db.commit()
            summary = payload_for_log(payload)
            logger.info(
                "Leased execution %s to runner %s (pool %s) agent_type=%s",
                execution_id,
                runner.id,
                self.pool,
                summary.get("agent_type"),
            )
            await _push_job(runner.id, payload)
            return f"runner:{runner.id}:{execution_id}"

        if execution:
            execution.agent_session_reference = (
                f"runner:queued:{self.pool}:{execution_id}"
            )
            self.db.add(execution)
            self.db.commit()
        logger.info(
            "No online runner for pool %s; queued execution %s",
            self.pool,
            execution_id,
        )
        return f"runner:queued:{self.pool}:{execution_id}"

    async def get_status(self, session_reference: str) -> AgentStatus:
        execution_id = _execution_id_from_ref(session_reference)
        execution = crud_flow_execution.get(self.db, id=execution_id, refresh=True)
        if execution and _map_status(execution.status) in (
            AgentStatus.SUCCEEDED,
            AgentStatus.FAILED,
            AgentStatus.STOPPED,
        ):
            return _map_status(execution.status)
        if session_reference.startswith("runner:queued:"):
            execution = execution or self.execution
            assigned_reference = getattr(execution, "agent_session_reference", None)
            if (
                isinstance(assigned_reference, str)
                and assigned_reference.startswith("runner:")
                and not assigned_reference.startswith("runner:queued:")
                and _execution_id_from_ref(assigned_reference) == execution_id
            ):
                return await self.get_status(assigned_reference)
            started = (
                execution.start_time
                if execution and execution.start_time
                else datetime.now(timezone.utc)
            )
            queued = mark_queued_or_fail(
                queued_since=started, timeout=DEFAULT_QUEUE_TIMEOUT
            )
            if queued == "FAILED":
                if execution:
                    execution.status = "FAILED"
                    execution.error_message = (
                        f"No matching self-hosted runner for pool "
                        f"{self.pool} within {DEFAULT_QUEUE_TIMEOUT}"
                    )
                    execution.end_time = datetime.now(timezone.utc)
                    self.db.add(execution)
                    self.db.commit()
                return AgentStatus.FAILED
            # A runner may have come online; try to lease now.
            if execution:
                flow = self._flow_for_execution(execution)
                payload = self._lease_payload(
                    execution_id=execution.id,
                    flow_id=execution.flow_id,
                    prompt=execution.resolved_input_prompt,
                    flow=flow,
                )
                runner = lease_job(
                    self.db,
                    account_id=self.account_id,
                    pool=self.pool,
                    execution_id=execution.id,
                    payload=payload,
                )
                if runner:
                    if flow is not None:
                        token, _ = create_flow_runtime_token(
                            self.db,
                            flow=flow,
                            execution_id=execution.id,
                        )
                        payload["account_api_token"] = token
                        if not token:
                            logger.warning(
                                "Could not create temporary API key record "
                                "for account %s",
                                getattr(flow, "account_id", self.account_id),
                            )
                    execution.runner_id = runner.id
                    execution.agent_session_reference = (
                        f"runner:{runner.id}:{execution.id}"
                    )
                    self.db.add(execution)
                    self.db.commit()
                    await _push_job(runner.id, payload)
                    return AgentStatus.STARTING
            return AgentStatus.PENDING

        runner_id = _runner_id_from_ref(session_reference)
        if runner_id:
            runner = crud_flow_runner.get(self.db, id=runner_id)
            if runner and runner.halt_requested:
                return AgentStatus.STOPPED
            if runner and runner.reported_status:
                return _map_status(runner.reported_status)
        if execution:
            return _map_status(execution.status)
        return AgentStatus.PENDING

    async def get_result(self, session_reference: str) -> AgentExecutionResult:
        status = await self.get_status(session_reference)
        execution_id = _execution_id_from_ref(session_reference)
        execution = crud_flow_execution.get(self.db, id=execution_id)
        return AgentExecutionResult(
            status=status,
            session_reference=session_reference,
            output_summary=execution.model_output_summary if execution else None,
            error_message=execution.error_message if execution else None,
            artifacts=execution.result if execution else None,
        )

    async def stop(self, session_reference: str) -> None:
        runner_id = _runner_id_from_ref(session_reference)
        if not runner_id:
            return
        runner = crud_flow_runner.get(self.db, id=runner_id)
        if not runner:
            return
        runner.halt_requested = True
        self.db.add(runner)
        self.db.commit()

    async def cleanup(self) -> None:
        return None

    def _lease_payload(
        self,
        *,
        execution_id: UUID,
        flow_id: Any,
        prompt: Any,
        execution_context: Optional[Dict[str, Any]] = None,
        flow: Any = None,
    ) -> Dict[str, Any]:
        """Build one complete payload for initial and delayed runner leases."""
        context = execution_context or {}
        flow = flow or self.flow
        try:
            ai_model = getattr(flow, "ai_model", None)
        except Exception:
            ai_model = None

        agent_config = context.get("agent_config")
        if agent_config is None and flow is not None:
            agent_config = getattr(flow, "agent_config", None)
        if agent_config is None:
            agent_config = self.config
        if (
            isinstance(agent_config, dict)
            and set(agent_config) == {"agent_config"}
            and isinstance(agent_config["agent_config"], dict)
        ):
            agent_config = agent_config["agent_config"]
        if isinstance(agent_config, dict):
            agent_config = dict(agent_config)
        else:
            agent_config = {}

        agent_type = (
            context.get("agent_type")
            or self.agent_type
            or (getattr(flow, "agent_type", None) if flow is not None else None)
        )
        if not agent_config_has_image(agent_config):
            image = default_agent_image(str(agent_type or ""))
            if image:
                agent_config["image"] = image

        def context_or_flow(key: str, default: Any = None) -> Any:
            value = context.get(key)
            if value is not None:
                return value
            return getattr(flow, key, default) if flow is not None else default

        payload: Dict[str, Any] = {
            "execution_id": str(execution_id),
            "flow_id": str(flow_id),
            "agent_type": agent_type,
            "agent_config": agent_config,
            "prompt": prompt,
            "model_identifier": context.get("model_identifier")
            or getattr(ai_model, "model_identifier", None)
            or self.config.get("model_identifier"),
            "model_provider": context.get("model_provider")
            or getattr(ai_model, "provider_name", None)
            or self.config.get("model_provider"),
            "account_api_token": context.get("account_api_token"),
            "allowed_mcp_servers": context_or_flow("allowed_mcp_servers", []) or [],
            "allowed_mcp_tools": context_or_flow("allowed_mcp_tools", []) or [],
            "git_clone_config": context_or_flow("git_clone_config"),
            "custom_commands": context_or_flow("custom_commands"),
        }
        resume_from = _resume_from_execution_id(context, self.execution)
        if resume_from:
            payload["resume_from"] = resume_from
        return payload

    def _flow_for_execution(self, execution: Any) -> Any:
        """Resolve the flow without depending on a loaded ORM relationship."""
        if self.flow is not None:
            return self.flow
        try:
            flow = getattr(execution, "flow", None)
        except Exception:
            flow = None
        if flow is None:
            flow_id = getattr(execution, "flow_id", None)
            if flow_id is not None:
                flow = crud_flow.get(self.db, id=flow_id)
        self.flow = flow
        return flow

    async def get_logs(
        self, session_reference: str, tail: int | None = None
    ) -> list[str]:
        execution_id = _execution_id_from_ref(session_reference)
        if not execution_id:
            return []
        rows = crud_flow_execution_log.get_by_execution_id(
            self.db, execution_id, tail=tail or 500, desc=False
        )
        lines: list[str] = []
        for row in rows:
            if row.message:
                lines.append(row.message)
        return lines


async def _push_job(runner_id: UUID, payload: Dict[str, Any]) -> None:
    """Push a leased job to a live WS if this process holds the socket."""
    try:
        from preloop.api.endpoints.runners import push_job_to_runner

        await push_job_to_runner(runner_id, payload)
    except Exception as exc:
        logger.debug("live job push skipped: %s", exc)


def _resume_from_execution_id(
    context: Dict[str, Any], execution: Any = None
) -> Optional[str]:
    """Prior execution id when this run resumes a PR-comment follow-up.

    Args:
        context: Execution context or empty dict.
        execution: Optional flow execution with trigger_event_details.

    Returns:
        Prior execution id string, or None when this is not a resume.
    """
    direct = context.get("resume_from")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    trigger = context.get("trigger_event_data")
    if not isinstance(trigger, dict) and execution is not None:
        trigger = getattr(execution, "trigger_event_details", None)
    if not isinstance(trigger, dict):
        return None
    resume = trigger.get("_resume") or {}
    if isinstance(resume, str) and resume.strip():
        return resume.strip()
    if isinstance(resume, dict):
        prior = resume.get("execution_id")
        if prior:
            return str(prior)
    return None


def payload_for_log(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Identifiers only — never tokens, prompt, git config, or agent_config."""

    agent_config = payload.get("agent_config")
    image = None
    if isinstance(agent_config, dict):
        raw = agent_config.get("image")
        if isinstance(raw, str) and raw.strip():
            image = raw.strip()
    return {
        "execution_id": payload.get("execution_id"),
        "agent_type": payload.get("agent_type"),
        "image": image,
    }


def _execution_id_from_ref(session_reference: str) -> Optional[UUID]:
    parts = (session_reference or "").split(":")
    if not parts:
        return None
    try:
        return UUID(parts[-1])
    except ValueError:
        return None


def _runner_id_from_ref(session_reference: str) -> Optional[UUID]:
    parts = (session_reference or "").split(":")
    if len(parts) >= 3 and parts[0] == "runner" and parts[1] != "queued":
        try:
            return UUID(parts[1])
        except ValueError:
            return None
    return None


def _map_status(raw: Optional[str]) -> AgentStatus:
    value = (raw or "PENDING").upper()
    mapping = {
        "PENDING": AgentStatus.PENDING,
        "STARTING": AgentStatus.STARTING,
        "INITIALIZING": AgentStatus.STARTING,
        "RUNNING": AgentStatus.RUNNING,
        "SUCCEEDED": AgentStatus.SUCCEEDED,
        "FAILED": AgentStatus.FAILED,
        "STOPPED": AgentStatus.STOPPED,
        "TIMEOUT": AgentStatus.FAILED,
        "CANCELLED": AgentStatus.STOPPED,
    }
    return mapping.get(value, AgentStatus.PENDING)
