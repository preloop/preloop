"""Native Hermes conversations for the standalone control process.

Each command owns a fresh agent; gateway chats and their session state are never
borrowed. Native approval defaults are preserved (no oneshot/YOLO environment).
"""

from __future__ import annotations

import asyncio
import math
import threading
from typing import Any
from uuid import uuid4

from preloop.integrations.agent_control import (
    AgentControlCapabilities,
    AgentControlResult,
    OperatorCommand,
)


class NativeHermesRuntime:
    """Adapt Hermes's synchronous AIAgent API without blocking heartbeats."""

    def __init__(self, plugin: Any) -> None:
        from hermes_cli.plugins import (
            PluginContext,
            PluginManifest,
            discover_plugins,
            get_plugin_manager,
        )

        plugin.verify()
        discover_plugins()
        plugin.register(
            PluginContext(PluginManifest(name="preloop-control"), get_plugin_manager())
        )

    def capabilities(self) -> AgentControlCapabilities:
        """Only new text conversations and the installed approval gate work."""
        return AgentControlCapabilities(
            supports_new_session=True,
            supports_existing_session=False,
            supports_text=True,
            supports_tool_approval=True,
        )

    async def send_command(self, command: OperatorCommand) -> AgentControlResult:
        """Execute a fresh session, returning a durable correlation reference."""
        if command.session_mode != "new" or command.session_reference:
            raise ValueError("Standalone Hermes requires a new session")
        if command.input_mode != "text" or command.interrupt:
            raise ValueError("Standalone Hermes supports text without interruption")
        limits = command.metadata.get("run_limits") or {}
        if not isinstance(limits, dict):
            raise ValueError("run_limits must be an object")
        if command.metadata.get("gateway") or any(
            limits.get(key) is not None for key in ("max_usd", "max_total_tokens")
        ):
            raise ValueError(
                "Standalone Hermes does not support execution gateway or cost/token limits"
            )
        turns = limits.get("max_turns", 90)
        duration = limits.get("max_duration_seconds") or command.metadata.get(
            "timeout_seconds"
        )
        if type(turns) is not int or turns < 1:
            raise ValueError("max_turns must be a positive integer")
        if duration is not None and (
            type(duration) not in (int, float)
            or not math.isfinite(duration)
            or duration <= 0
        ):
            raise ValueError("timeout must be positive")
        return await asyncio.to_thread(self._run, command.text, turns, duration)

    def _run(self, text: str, turns: int, duration: float | None) -> AgentControlResult:
        from hermes_cli.env_loader import load_hermes_dotenv
        from hermes_cli.config import load_config
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from hermes_state import SessionDB
        from run_agent import AIAgent

        load_hermes_dotenv()
        config = load_config()
        model_config = config.get("model") or {}
        model = (
            model_config
            if isinstance(model_config, str)
            else model_config.get("default") or model_config.get("model") or ""
        )
        runtime = resolve_runtime_provider(target_model=model or None)
        session_id = str(uuid4())
        database = SessionDB()
        agent = None
        timer = None
        expired = threading.Event()
        try:
            agent = AIAgent(
                model=model,
                api_key=runtime.get("api_key"),
                base_url=runtime.get("base_url"),
                provider=runtime.get("provider"),
                api_mode=runtime.get("api_mode"),
                credential_pool=runtime.get("credential_pool"),
                enabled_toolsets=[],
                max_iterations=turns,
                skip_context_files=True,
                skip_memory=True,
                quiet_mode=True,
                platform="cli",
                session_id=session_id,
                session_db=database,
            )
            if agent.tools:
                raise RuntimeError(
                    "Hermes unexpectedly enabled tools for a text-only session"
                )
            if duration is not None:

                def deadline() -> None:
                    expired.set()
                    agent.interrupt("Preloop command deadline exceeded")

                timer = threading.Timer(duration, deadline)
                timer.daemon = True
                timer.start()
            result = agent.run_conversation(text)
            if expired.is_set():
                raise TimeoutError("Hermes command deadline exceeded")
            if not isinstance(result, dict):
                raise RuntimeError("Hermes returned an invalid conversation result")
            if (
                result.get("failed")
                or result.get("partial")
                or result.get("interrupted")
            ):
                raise RuntimeError(
                    "Hermes conversation failed or returned a partial result"
                )
            reply = result.get("final_response")
            if not isinstance(reply, str) or not reply.strip():
                raise RuntimeError("Hermes conversation produced no final response")
            usage = {}
            for source, target in (
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("total_tokens", "total_tokens"),
                ("estimated_cost_usd", "total_cost_usd"),
            ):
                value = result.get(source)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    usage[target] = value
            return AgentControlResult(
                reply_text=reply, session_reference=session_id, usage=usage
            )
        finally:
            if timer is not None:
                timer.cancel()
            try:
                if agent is not None:
                    agent.close()
            finally:
                database.close()
