"""Base abstract class for agent execution."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from .failure_analysis import AgentFailureAnalysis

logger = logging.getLogger(__name__)


class AgentStatus(str, Enum):
    """Status of an agent execution."""

    PENDING = "PENDING"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


@dataclass
class AgentExecutionResult:
    """Result of an agent execution."""

    status: AgentStatus
    session_reference: str  # Container ID, job ID, process ID, etc.
    output_summary: Optional[str] = None
    error_message: Optional[str] = None
    actions_taken: Optional[list] = None
    artifacts: Optional[Dict[str, Any]] = None  # Generated files, logs, etc.
    exit_code: Optional[int] = None
    # First-pass failure classification made against the FULL container logs
    # (set when status is FAILED and logs were analysed). ``error_message``
    # keeps only the generated sentence, which cannot encode the
    # transient/terminal verdict — this carries it to the retry decision.
    failure_analysis: Optional["AgentFailureAnalysis"] = None


class AgentExecutor(ABC):
    """
    Abstract base class for executing AI agents in isolated environments.

    This provides a uniform interface for running different agent types
    (OpenHands, Claude Code, Aider, etc.) in various execution environments
    (Docker containers, Kubernetes pods, local processes).
    """

    #: Whether this runtime supports the one-shot completion-confirmation
    #: round ("nudge", layer 2 of the flow completion contract): a fresh,
    #: cheap invocation carrying prior context (original prompt + output
    #: tail) whose only job is to confirm or deny that the original task
    #: completed. Runtimes that cannot cheaply re-invoke with prior context
    #: (e.g. queued remote runners) leave this False and the orchestrator
    #: no-ops the nudge for them, keeping the fail-closed behaviour.
    #: The orchestrator checks ``is True`` strictly, so mocks without an
    #: explicit opt-in never trigger a nudge.
    supports_confirmation_nudge: bool = False

    def __init__(self, agent_type: str, config: Dict[str, Any]):
        """
        Initialize the agent executor.

        Args:
            agent_type: Type of agent (e.g., 'codex', 'gemini', 'openhands', 'aider')
            config: Agent-specific configuration
        """
        self.agent_type = agent_type
        self.config = config
        self.logger = logging.getLogger(f"{__name__}.{agent_type}")

    @abstractmethod
    async def start(
        self,
        execution_context: Dict[str, Any],
    ) -> str:
        """
        Start the agent execution.

        Args:
            execution_context: Context containing:
                - flow_id: Flow UUID
                - execution_id: Execution UUID
                - prompt: Resolved prompt for the agent
                - agent_config: Agent-specific configuration
                - model_identifier: AI model to use
                - model_provider: AI model provider
                - model_api_key: API key for the model
                - model_parameters: Model-specific parameters
                - allowed_mcp_servers: List of allowed MCP servers
                - allowed_mcp_tools: List of allowed MCP tools

        Returns:
            session_reference: Unique reference to the running agent session
                              (e.g., container ID, job ID, process ID)
        """
        pass

    @abstractmethod
    async def get_status(self, session_reference: str) -> AgentStatus:
        """
        Get the current status of an agent execution.

        Args:
            session_reference: Reference to the agent session

        Returns:
            Current status of the agent
        """
        pass

    @abstractmethod
    async def get_result(self, session_reference: str) -> AgentExecutionResult:
        """
        Get the result of an agent execution.

        Args:
            session_reference: Reference to the agent session

        Returns:
            Execution result including status, output, errors, etc.
        """
        pass

    @abstractmethod
    async def stop(self, session_reference: str) -> None:
        """
        Stop a running agent execution.

        Args:
            session_reference: Reference to the agent session
        """
        pass

    @abstractmethod
    async def get_logs(
        self, session_reference: str, tail: int | None = None
    ) -> list[str]:
        """
        Get logs from an agent execution.

        Args:
            session_reference: Reference to the agent session
            tail: Number of recent log lines to retrieve, or None for all logs

        Returns:
            List of log lines
        """
        pass
