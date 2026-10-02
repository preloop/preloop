"""Governed embedding of the pinned Nanobot public Python interfaces."""

from __future__ import annotations

import asyncio
import contextvars
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp

from preloop.integrations.agent_control import (
    AgentControlCapabilities,
    AgentControlClient,
    AgentControlConfig,
    AgentControlResult,
    OperatorCommand,
)

_session = contextvars.ContextVar("nanobot_session", default="")


def validate_document(document: dict[str, Any]) -> AgentControlConfig:
    """Reject missing credentials and off-gateway endpoints before starting."""
    config = AgentControlConfig.from_document(document)
    endpoint = urlsplit(config.control_ws_url)
    if (
        endpoint.scheme not in {"ws", "wss"}
        or not endpoint.hostname
        or endpoint.username
    ):
        raise ValueError("invalid control endpoint")
    if endpoint.scheme == "ws" and endpoint.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("remote control requires TLS")
    if any(c in config.bearer_token for c in "\r\n"):
        raise ValueError("invalid runtime credential")
    return config


class BoundedProvider:
    """Bound all model calls including Nanobot memory consolidation."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.remaining_tokens = 100000

    def __getattr__(self, name: str) -> Any:
        return getattr(self.provider, name)

    async def chat(self, *args: Any, **kwargs: Any) -> Any:
        messages = kwargs.get("messages", args[0] if args else [])
        if len(json.dumps(messages)) > 100000 or self.remaining_tokens <= 0:
            raise ValueError("model context/token budget exceeded")
        prompt_bound = (
            len(
                json.dumps(
                    {"messages": messages, "tools": kwargs.get("tools", [])}
                ).encode("utf-8")
            )
            + 1024
        )
        available_output = self.remaining_tokens - prompt_bound
        if available_output <= 0:
            raise ValueError("model context/token budget exceeded")
        kwargs["max_tokens"] = min(kwargs.get("max_tokens", 4096), available_output)
        # AsyncOpenAI.with_options is a public SDK seam. Attribute access here
        # is pinned to Nanobot CustomProvider, whose client owns model routing.
        if hasattr(self.provider, "_client"):
            self.provider._client = self.provider._client.with_options(
                default_headers={"X-Preloop-Session-Id": _session.get()}
            )
        result = await self.provider.chat(*args, **kwargs)
        if result.finish_reason == "error":
            raise RuntimeError("Nanobot gateway model request failed")
        usage = result.usage or {}
        used = usage.get("total_tokens", prompt_bound + kwargs["max_tokens"])
        if type(used) is not int or used < 0:
            raise ValueError("invalid model usage")
        self.remaining_tokens -= used
        return result


class GovernedTools:
    """Intercept every native and dynamically registered MCP tool execution."""

    def __init__(self, registry: Any, config: AgentControlConfig) -> None:
        self.registry = registry
        self.config = config

    def __getattr__(self, name: str) -> Any:
        return getattr(self.registry, name)

    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """Execute only after a well-formed, explicit allow decision."""
        if name in {"spawn", "cron", "message"}:
            return "Error: background agents, scheduling and outbound messaging are disabled"
        if not isinstance(params, dict) or not _session.get():
            return "Error: invalid tool arguments or unowned session"
        endpoint = urlsplit(self.config.control_ws_url)
        scheme = "https" if endpoint.scheme == "wss" else "http"
        url = f"{scheme}://{endpoint.netloc}/api/v1/agents/permission-check"
        try:
            async with aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.config.bearer_token}"},
                timeout=aiohttp.ClientTimeout(total=300),
            ) as client:
                async with client.post(
                    url,
                    json={
                        "source": "nanobot",
                        "tool_name": name,
                        "tool_input": params,
                        "session_id": _session.get(),
                        "client_decision": "ask",
                    },
                ) as response:
                    response.raise_for_status()
                    decision = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return "Error: Preloop permission service unavailable"
        if (
            not isinstance(decision, dict)
            or decision.get("decision") != "allow"
            or decision.get("timed_out", False) is not False
            or ("reason" in decision and not isinstance(decision["reason"], str))
        ):
            return "Error: tool permission denied or malformed"
        return await self.registry.execute(name, params)


class NanobotRuntime:
    """Own session identities and bounded turns for one enrolled principal."""

    def __init__(self, config: AgentControlConfig, loop: Any, state: Path) -> None:
        self.config, self.loop, self.state = config, loop, state
        self.loop.tools = GovernedTools(loop.tools, config)
        self.active: dict[str, asyncio.Task[Any]] = {}
        self.current: str | None = None
        self.command_sessions: dict[str, str] = {}
        self.lock = asyncio.Lock()
        self.owner = config.runtime_principal_id
        self.sessions: set[str] = set()
        if state.exists():
            document = json.loads(state.read_text())
            if document.get("owner") != self.owner:
                raise ValueError("session store belongs to another principal")
            self.sessions = set(document["sessions"])

    def capabilities(self) -> AgentControlCapabilities:
        """Advertise only lifecycle features implemented here."""
        return AgentControlCapabilities(
            supports_interrupt=True, supports_tool_approval=True
        )

    def _save(self) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.state.with_name(self.state.name + f".{uuid4()}.tmp")
        temporary.write_text(
            json.dumps({"owner": self.owner, "sessions": sorted(self.sessions)})
        )
        temporary.chmod(0o600)
        temporary.replace(self.state)

    async def handle_send_message(self, command: OperatorCommand) -> AgentControlResult:
        """Start or resume an owned session; refuse foreign references."""
        if command.interrupt:
            reference = command.session_reference or self.command_sessions.get(
                command.metadata.get("target_command_id", "")
            )
            await self.interrupt(reference)
            return AgentControlResult(status="accepted", session_reference=reference)
        if command.input_mode != "text":
            raise ValueError("Nanobot supports text input only")
        if len(command.text) > 32000:
            raise ValueError("input exceeds bounded conversation size")
        limits = command.metadata.get("run_limits", {})
        if not isinstance(limits, dict):
            raise ValueError("run_limits must be an object")
        seconds = limits.get(
            "timeout_seconds",
            limits.get(
                "max_duration_seconds", command.metadata.get("timeout_seconds", 300)
            ),
        )
        turns = limits.get("max_turns", 20)
        tokens = limits.get("max_total_tokens", 100000)
        if type(tokens) is not int or not 1 <= tokens <= 1000000:
            raise ValueError("max_total_tokens must be from 1 to 1000000")
        gateway = command.metadata.get("gateway")
        if gateway is not None:
            if not isinstance(gateway, dict):
                raise ValueError("gateway must be an object")
            endpoint = urlsplit(self.config.control_ws_url)
            expected = (
                ("https" if endpoint.scheme == "wss" else "http")
                + "://"
                + endpoint.netloc
                + "/openai/v1"
            )
            if gateway.get("base_url", "").rstrip("/") != expected:
                raise ValueError(
                    "execution gateway must belong to the enrolled instance"
                )
            if (
                not isinstance(gateway.get("api_key"), str)
                or not gateway["api_key"]
                or any(c in gateway["api_key"] for c in "\r\n")
            ):
                raise ValueError("execution gateway credential missing or invalid")
            if not isinstance(gateway.get("model"), str) or not gateway["model"]:
                raise ValueError("execution gateway model missing")
        if (
            any(key in limits for key in ("max_cost", "max_cost_usd", "max_usd"))
            and gateway is None
        ):
            raise ValueError(
                "per-run monetary limits require an execution-scoped gateway"
            )
        history_chars = limits.get("max_history_chars", 64000)
        if type(history_chars) is not int or not 1000 <= history_chars <= 64000:
            raise ValueError("max_history_chars must be from 1000 to 64000")
        if type(seconds) not in {int, float} or not 1 <= seconds <= 3600:
            raise ValueError("timeout_seconds must be from 1 to 3600")
        if type(turns) is not int or not 1 <= turns <= 100:
            raise ValueError("max_turns must be from 1 to 100")
        # Nanobot's registry/context is shared. Serialize turns to avoid session
        # tool contexts and history interleaving; cancellation remains external.
        async with self.lock:
            reference = command.session_reference
            if command.session_mode == "new" or command.start_new_session:
                reference = None
            elif command.session_mode == "current" and reference is None:
                reference = self.current
            if reference is not None and reference not in self.sessions:
                raise ValueError("session is not owned by this runtime")
            if reference is None:
                if command.session_mode == "existing":
                    raise ValueError("existing session requires a reference")
                reference = f"preloop:{uuid4()}"
                self.sessions.add(reference)
                self._save()
            self.current = reference
            session = self.loop.sessions.get_or_create(reference)
            # Keep bounded recent context while Nanobot retains full history on disk.
            if sum(len(str(m)) for m in session.messages[-40:]) > history_chars:
                raise ValueError("session context budget exceeded; start a new session")
            original_loop = self.loop
            if gateway is not None:
                control = asdict(self.config)
                control["bearer_token"] = gateway["api_key"]
                scoped = build_runtime(
                    {
                        "preloop": {"control": control},
                        "model": gateway["model"],
                        "workspace": str(self.loop.workspace),
                    },
                    self.state,
                )
                self.loop = scoped.loop
            if isinstance(self.loop.provider, BoundedProvider):
                self.loop.provider.remaining_tokens = tokens
            self.loop.memory_window = 40
            self.loop.max_iterations = turns
            self.command_sessions[command.command_id] = reference
            token = _session.set(reference)

            async def run_turn() -> str:
                try:
                    return await self.loop.process_direct(
                        command.text, session_key=reference
                    )
                finally:
                    close = getattr(self.loop, "close_mcp", None)
                    if close is not None:
                        await close()
                        self.loop._mcp_connected = False

            task = asyncio.create_task(run_turn())
            self.active[reference] = task
            try:
                reply = await asyncio.wait_for(task, seconds)
                if isinstance(reply, str) and (
                    reply.startswith("Error:")
                    or reply.startswith(
                        "I reached the maximum number of tool call iterations"
                    )
                ):
                    raise RuntimeError("Nanobot provider or runtime failed")
                return AgentControlResult(
                    reply_text=str(reply),
                    session_reference=reference,
                    metadata={"native_session_id": reference},
                )
            finally:
                # Nanobot schedules memory consolidation separately. Do not
                # let it continue under a later task's token budget or session.
                pending = list(getattr(self.loop, "_consolidation_tasks", ()))
                for background in pending:
                    background.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                self.active.pop(reference, None)
                self.command_sessions.pop(command.command_id, None)
                _session.reset(token)
                self.loop = original_loop

    async def interrupt(self, reference: str | None) -> None:
        """Cancel an exact owned active turn without deleting history."""
        if reference is None or reference not in self.sessions:
            raise ValueError("interrupt requires an owned session reference")
        task = self.active.get(reference)
        if task is not None:
            task.cancel()


def build_runtime(document: dict[str, Any], state: Path) -> NanobotRuntime:
    """Construct the pinned SDK runtime with gateway-only model and MCP routing."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.custom_provider import CustomProvider

    config = validate_document(document)
    endpoint = urlsplit(config.control_ws_url)
    scheme = "https" if endpoint.scheme == "wss" else "http"
    base = f"{scheme}://{endpoint.netloc}"
    model = document.get("model", "deepseek-chat")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be nonempty")
    workspace = Path(document.get("workspace", "~/.nanobot/workspace")).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    provider = CustomProvider(
        api_key=config.bearer_token, api_base=f"{base}/openai/v1", default_model=model
    )
    # No arbitrary stdio/remote MCP endpoints: Preloop is the sole MCP server.
    mcp = {
        "preloop": {
            "url": f"{base}/mcp/v1",
            "headers": {"Authorization": f"Bearer {config.bearer_token}"},
        }
    }
    from nanobot.config.schema import MCPServerConfig

    loop = AgentLoop(
        bus=MessageBus(),
        provider=BoundedProvider(provider),
        workspace=workspace,
        model=model,
        max_iterations=20,
        memory_window=40,
        restrict_to_workspace=True,
        mcp_servers={name: MCPServerConfig(**value) for name, value in mcp.items()},
    )
    return NanobotRuntime(config, loop, state)


class ConcurrentControlClient(AgentControlClient):
    """Keep receiving exact-target interrupts while a bounded turn runs."""

    def __init__(self, runtime: NanobotRuntime) -> None:
        super().__init__(runtime.config, runtime)
        self.commands: set[asyncio.Task[Any]] = set()

    async def _connect_once(self) -> None:
        try:
            await super()._connect_once()
        finally:
            pending = list(self.commands)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _handle_text_message(
        self, websocket: Any, envelope: dict[str, Any]
    ) -> None:
        from preloop.integrations.agent_control.core import dispatch_operator_command

        if envelope.get("type") != "command":
            await super()._handle_text_message(websocket, envelope)
            return

        async def dispatch() -> None:
            try:
                outbound = await dispatch_operator_command(self.hooks, envelope)
            except asyncio.CancelledError:
                outbound = [
                    {
                        "type": "status",
                        "name": "command_status",
                        "message_id": envelope.get("message_id"),
                        "payload": {"status": "failed", "reason": "cancelled"},
                    }
                ]
            for message in outbound:
                await self._send_json(websocket, message)

        task = asyncio.create_task(dispatch())
        self.commands.add(task)
        task.add_done_callback(self.commands.discard)
