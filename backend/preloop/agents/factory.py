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
