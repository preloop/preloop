"""Factory for creating agent executors."""

import logging
from typing import Any, Dict

from .base import AgentExecutor
from .openhands import OpenHandsAgent
from .aider import AiderAgent
from .codex import CodexAgent
from .gemini import GeminiAgent
from .opencode import OpenCodeAgent

logger = logging.getLogger(__name__)

# Single registry of supported agent harnesses. API-level validation (e.g.
# matrix triggers in api/endpoints/flows.py) derives its allowed set from
# SUPPORTED_AGENT_TYPES so adding a harness here is sufficient everywhere.
_AGENT_EXECUTOR_REGISTRY: Dict[str, type[AgentExecutor]] = {
    "openhands": OpenHandsAgent,
    "aider": AiderAgent,
    "codex": CodexAgent,
    "gemini": GeminiAgent,
    "opencode": OpenCodeAgent,
}

SUPPORTED_AGENT_TYPES = frozenset(_AGENT_EXECUTOR_REGISTRY)


def create_executor_for_execution(
    agent_type: str,
    config: Dict[str, Any],
    *,
    flow: Any = None,
    execution: Any = None,
    db: Any = None,
    execution_context: Dict[str, Any] | None = None,
) -> AgentExecutor:
    """Return a remote runner executor when the flow is pinned to a pool."""
    from preloop.agents.remote_runner import RemoteRunnerExecutor
    from preloop.services.runner_service import resolve_runner_pool

    pool = None
    if flow is not None:
        pool = resolve_runner_pool(flow, execution_context)
    ref = getattr(execution, "agent_session_reference", None) if execution else None
    if not pool and isinstance(ref, str) and ref.startswith("runner:"):
        parts = ref.split(":")
        pool = parts[2] if len(parts) >= 4 and parts[1] == "queued" else "default"
    if pool and db is not None:
        account_id = getattr(flow, "account_id", None) or (execution_context or {}).get(
            "account_id"
        )
        if account_id is None and execution is not None:
            account_id = getattr(execution, "account_id", None)
        if account_id is None and flow is not None:
            account_id = getattr(flow, "account_id", None)
        remote_config = (
            getattr(flow, "agent_config", None) if flow is not None else None
        )
        if remote_config is None:
            remote_config = config
            if set(config) == {"agent_config"} and isinstance(
                config["agent_config"], dict
            ):
                remote_config = config["agent_config"]
        return RemoteRunnerExecutor(
            agent_type,
            remote_config,
            db=db,
            pool=str(pool),
            account_id=account_id,
            flow=flow,
            execution=execution,
        )
    return create_agent_executor(agent_type, config)


def create_agent_executor(agent_type: str, config: Dict[str, Any]) -> AgentExecutor:
    """
    Create an agent executor based on agent type.

    Args:
        agent_type: Type of agent (e.g., 'openhands', 'aider', 'codex', 'gemini')
        config: Agent-specific configuration

    Returns:
        AgentExecutor instance for the specified agent type

    Raises:
        ValueError: If agent_type is not supported
    """
    executor_cls = _AGENT_EXECUTOR_REGISTRY.get(agent_type.lower())
    if executor_cls is None:
        raise ValueError(
            f"Unsupported agent type: {agent_type}. "
            f"Supported types: {', '.join(sorted(SUPPORTED_AGENT_TYPES))}"
        )
    return executor_cls(config)
