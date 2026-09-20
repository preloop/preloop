"""Factory for creating agent executors."""

import logging
from typing import Any, Dict, Optional

from preloop.agents.errors import AgentStartError

from .base import AgentExecutor
from .openhands import OpenHandsAgent
from .aider import AiderAgent
from .codex import CodexAgent
from .gemini import GeminiAgent
from .opencode import OpenCodeAgent
from .harness import PiAgent, DeepSeekAgent

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
    "pi": PiAgent,
    "deepseek": DeepSeekAgent,
}

SUPPORTED_AGENT_TYPES = frozenset(_AGENT_EXECUTOR_REGISTRY)


def _persistent_agent_config(
    config: Dict[str, Any],
    *,
    flow: Any = None,
    execution_context: Dict[str, Any] | None = None,
) -> Optional[Dict[str, Any]]:
    """Return the unwrapped config when this execution is persistent.

    ``execution_path`` may live on ``config``, a nested ``agent_config``
    wrapper, ``flow.agent_config``, or the orchestrator context. Persistent
    must win before runner-pool resolution so a saved private pool does not
    send the run to a container host.

    Args:
        config: Agent configuration passed to the factory.
        flow: Optional flow row whose ``agent_config`` may carry the path.
        execution_context: Optional orchestrator context.

    Returns:
        The unwrapped persistent config, or None when this is not a
        persistent execution.
    """
    from preloop.services.runner_service import unwrap_agent_config

    sources: list[Any] = [config]
    if isinstance(config, dict):
        sources.append(config.get("agent_config"))
    if flow is not None:
        sources.append(getattr(flow, "agent_config", None))
    if execution_context:
        context_config = execution_context.get("agent_config")
        sources.append(context_config)
        if isinstance(context_config, dict):
            sources.append(context_config.get("agent_config"))
    for source in sources:
        unwrapped = unwrap_agent_config(source)
        if (
            isinstance(unwrapped, dict)
            and unwrapped.get("execution_path") == "persistent"
        ):
            return unwrapped
    return None


def create_executor_for_execution(
    agent_type: str,
    config: Dict[str, Any],
    *,
    flow: Any = None,
    execution: Any = None,
    db: Any = None,
    execution_context: Dict[str, Any] | None = None,
) -> AgentExecutor:
    """Return the executor for one flow execution.

    Persistent Agent Control targets are selected first so a saved runner
    pool cannot send them to a container. Private-pool and host-exec
    resolution follow; everything else uses the hosted harness registry.
    """
    from preloop.agents.agent_control import AgentControlExecutor
    from preloop.agents.remote_runner import RemoteRunnerExecutor
    from preloop.services.host_exec import (
        HOST_EXEC_AGENT_TYPE,
        host_exec_profile_name,
    )
    from preloop.services.runner_service import resolve_runner_pool

    persistent = _persistent_agent_config(
        config, flow=flow, execution_context=execution_context
    )
    if persistent is not None:
        target_id = str(persistent.get("target_agent_id") or "").strip()
        if not target_id:
            raise AgentStartError(
                "persistent target is missing; the flow will not start an "
                "ephemeral run",
                category="runner_error",
            )
        if db is None:
            raise AgentStartError(
                "persistent flow execution requires a database session",
                category="runner_error",
            )
        account_id = getattr(flow, "account_id", None) or (execution_context or {}).get(
            "account_id"
        )
        if account_id is None and execution is not None:
            account_id = getattr(execution, "account_id", None)
        return AgentControlExecutor(
            agent_type,
            persistent,
            db=db,
            account_id=account_id,
            flow=flow,
            execution=execution,
        )

    profile = host_exec_profile_name(config, execution_context)
    kind = (agent_type or "").strip().lower()
    pool = None
    if flow is not None:
        pool = resolve_runner_pool(flow, execution_context, db=db)
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
    if profile or kind == HOST_EXEC_AGENT_TYPE:
        raise ValueError(
            "host execution profiles cannot run on hosted compute; "
            "pin the flow to a private runner that advertises the profile"
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
