"""Preloop agent-control integration kit for external runtimes."""

from preloop.integrations.agent_control.adapters import (
    HermesAgentControlAdapter,
    HookedAgentControlAdapter,
    OpenClawAgentControlAdapter,
    create_hermes_agent_control_client,
    create_openclaw_agent_control_client,
    load_agent_control_config,
    load_hermes_control_config,
    load_openclaw_control_config,
)
from preloop.integrations.agent_control.core import (
    EVICTION_CLOSE_CODE,
    AgentControlCapabilities,
    AgentControlClient,
    AgentControlConfig,
    AgentControlResult,
    AgentControlRuntimeHooks,
    ConnectionEvictedError,
    HookedDesktopInstaller,
    OperatorCommand,
    dispatch_operator_command,
    run_desktop_enable,
)

__all__ = [
    "EVICTION_CLOSE_CODE",
    "AgentControlCapabilities",
    "AgentControlClient",
    "AgentControlConfig",
    "AgentControlResult",
    "AgentControlRuntimeHooks",
    "ConnectionEvictedError",
    "HermesAgentControlAdapter",
    "HookedAgentControlAdapter",
    "HookedDesktopInstaller",
    "OpenClawAgentControlAdapter",
    "create_hermes_agent_control_client",
    "create_openclaw_agent_control_client",
    "OperatorCommand",
    "dispatch_operator_command",
    "run_desktop_enable",
    "load_agent_control_config",
    "load_hermes_control_config",
    "load_openclaw_control_config",
]
