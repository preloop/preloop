"""OpenClaw and Hermes adapter templates for Preloop agent control.

These classes are intentionally thin because the real runtime APIs are not
vendored in this repository. Runtime maintainers can wrap their native session
manager with these hooks and hand the instance to :class:`AgentControlClient`.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from preloop.integrations.agent_control.core import (
    AgentControlCapabilities,
    AgentControlClient,
    AgentControlConfig,
    AgentControlResult,
    OperatorCommand,
)

SendMessageHook = Callable[[OperatorCommand], Awaitable[AgentControlResult]]
InterruptHook = Callable[[str | None], Awaitable[None]]


@dataclass
class HookedAgentControlAdapter:
    """Generic adapter around a runtime's native session/message hooks."""

    send_message: SendMessageHook
    capabilities_value: AgentControlCapabilities
    interrupt_session: InterruptHook | None = None

    @classmethod
    def with_hooks(
        cls,
        *,
        send_message: SendMessageHook,
        interrupt_session: InterruptHook | None = None,
        supports_voice: bool = False,
        supports_new_session: bool = True,
        supports_existing_session: bool = True,
        supports_interrupt: bool | None = None,
        supports_tool_approval: bool = False,
    ) -> "HookedAgentControlAdapter":
        """Create a generic adapter from native runtime hook functions."""

        return cls(
            send_message=send_message,
            interrupt_session=interrupt_session,
            capabilities_value=AgentControlCapabilities(
                supports_new_session=supports_new_session,
                supports_existing_session=supports_existing_session,
                supports_text=True,
                supports_voice=supports_voice,
                supports_interrupt=(
                    interrupt_session is not None
                    if supports_interrupt is None
                    else supports_interrupt
                ),
                supports_tool_approval=supports_tool_approval,
            ),
        )

    def capabilities(self) -> AgentControlCapabilities:
        """Return runtime capability support."""

        return self.capabilities_value

    async def handle_send_message(self, command: OperatorCommand) -> AgentControlResult:
        """Forward the command into the runtime hook."""

        if command.interrupt and self.interrupt_session is not None:
            await self.interrupt_session(command.session_reference)
        return await self.send_message(command)


class OpenClawAgentControlAdapter(HookedAgentControlAdapter):
    """Template hook adapter for OpenClaw runtime integrations."""

    @classmethod
    def with_hooks(
        cls,
        *,
        send_message: SendMessageHook,
        interrupt_session: InterruptHook | None = None,
        supports_interrupt: bool = False,
        supports_voice: bool = False,
        supports_tool_approval: bool = False,
    ) -> "OpenClawAgentControlAdapter":
        """Create an OpenClaw adapter from native runtime hook functions."""

        return cls(
            send_message=send_message,
            interrupt_session=interrupt_session,
            capabilities_value=AgentControlCapabilities(
                supports_new_session=True,
                supports_existing_session=True,
                supports_text=True,
                supports_voice=supports_voice,
                supports_interrupt=supports_interrupt,
                supports_tool_approval=supports_tool_approval,
            ),
        )


class HermesAgentControlAdapter(HookedAgentControlAdapter):
    """Template hook adapter for Hermes runtime integrations."""

    @classmethod
    def with_hooks(
        cls,
        *,
        send_message: SendMessageHook,
        interrupt_session: InterruptHook | None = None,
        supports_interrupt: bool = False,
        supports_voice: bool = False,
        supports_tool_approval: bool = False,
    ) -> "HermesAgentControlAdapter":
        """Create a Hermes adapter from native runtime hook functions."""

        return cls(
            send_message=send_message,
            interrupt_session=interrupt_session,
            capabilities_value=AgentControlCapabilities(
                supports_new_session=True,
                supports_existing_session=True,
                supports_text=True,
                supports_voice=supports_voice,
                supports_interrupt=supports_interrupt,
                supports_tool_approval=supports_tool_approval,
            ),
        )


def load_agent_control_config(path: str | Path, *, runtime: str) -> AgentControlConfig:
    """Load ``preloop.control`` from a CLI-managed runtime config file."""

    path = Path(path)
    if path.suffix.lower() == ".json":
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    else:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "YAML control config loading requires PyYAML in the runtime"
            ) from exc
        with path.open("r", encoding="utf-8") as handle:
            document: Any = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise ValueError(f"{runtime} config root must be an object")
    return AgentControlConfig.from_document(document)


def load_openclaw_control_config(path: str | Path) -> AgentControlConfig:
    """Load ``preloop.control`` from a CLI-managed OpenClaw JSON config."""

    return load_agent_control_config(path, runtime="OpenClaw")


def load_hermes_control_config(path: str | Path) -> AgentControlConfig:
    """Load ``preloop.control`` from a CLI-managed Hermes YAML config."""

    return load_agent_control_config(path, runtime="Hermes")


def create_openclaw_agent_control_client(
    config_path: str | Path,
    *,
    send_message: SendMessageHook,
    interrupt_session: InterruptHook | None = None,
    supports_interrupt: bool = False,
    supports_voice: bool = False,
    supports_tool_approval: bool = False,
) -> AgentControlClient:
    """Create an always-open OpenClaw Agent Control client."""

    return AgentControlClient(
        load_openclaw_control_config(config_path),
        OpenClawAgentControlAdapter.with_hooks(
            send_message=send_message,
            interrupt_session=interrupt_session,
            supports_interrupt=supports_interrupt,
            supports_voice=supports_voice,
            supports_tool_approval=supports_tool_approval,
        ),
    )


def create_hermes_agent_control_client(
    config_path: str | Path,
    *,
    send_message: SendMessageHook,
    interrupt_session: InterruptHook | None = None,
    supports_interrupt: bool = False,
    supports_voice: bool = False,
    supports_tool_approval: bool = False,
) -> AgentControlClient:
    """Create an always-open Hermes Agent Control client."""

    return AgentControlClient(
        load_hermes_control_config(config_path),
        HermesAgentControlAdapter.with_hooks(
            send_message=send_message,
            interrupt_session=interrupt_session,
            supports_interrupt=supports_interrupt,
            supports_voice=supports_voice,
            supports_tool_approval=supports_tool_approval,
        ),
    )
