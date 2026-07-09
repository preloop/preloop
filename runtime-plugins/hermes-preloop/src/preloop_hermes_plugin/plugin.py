"""Standalone Hermes plugin entrypoint for Preloop."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib import parse, request
from uuid import uuid4

import aiohttp
import yaml

from preloop.integrations.agent_control import (
    AgentControlCapabilities,
    AgentControlClient,
    AgentControlConfig,
    AgentControlResult,
    OperatorCommand,
    create_hermes_agent_control_client,
    load_hermes_control_config,
)

logger = logging.getLogger(__name__)

# Hermes fires this lifecycle hook after the model selects a tool but before the
# tool runs. Returning a block envelope vetoes the call.
#
# Pinned against the Hermes hook API (docs + PR #9377 / issue #9388):
#   - Event payload (shell-hook form): {"hook_event_name": "pre_tool_call",
#     "tool_name": ..., "tool_input": {...}, "session_id": ..., "cwd": ...,
#     "extra": {...}} -- https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks
#   - Python plugin form registers via ctx.register_hook("pre_tool_call", cb)
#     with callback (tool_name, args, task_id, **kwargs)
#     -- https://hermes-agent.nousresearch.com/docs/guides/build-a-hermes-plugin
#   - Canonical BLOCK return (upstream): {"action": "block", "message": ...}
#     Extra keys are ignored by Hermes' strict validator. We also include the
#     plan-era aliases ``decision``/``reason`` for older docs/tests.
#   - Any other / empty return allows the call.
# The handler below accepts both calling conventions defensively.
HOOK_EVENT_PRE_TOOL_CALL = "pre_tool_call"
PERMISSION_CHECK_PATH = "/api/v1/agents/permission-check"
# Backend blocks up to ~300s waiting for a mobile/watch decision; give it slack.
PERMISSION_CHECK_TIMEOUT_SECONDS = 310.0


class HermesPreloopPlugin:
    """Hermes plugin wrapper around Preloop's Agent Control client."""

    runtime_name = "hermes"

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path
        self.client: AgentControlClient | None = None
        self._control_settings: tuple[AgentControlConfig, dict[str, Any]] | None = None

    def load_config(self) -> AgentControlConfig:
        """Load the `preloop.control` block from Hermes configuration."""
        return load_hermes_control_config(self.config_path)

    def capabilities(self) -> AgentControlCapabilities:
        """Advertise the Hermes control surface exposed by this plugin."""
        return AgentControlCapabilities(
            supports_new_session=True,
            supports_existing_session=True,
            supports_text=True,
            supports_voice=True,
            supports_interrupt=True,
            supports_tool_approval=True,
        )

    async def start(self, hermes_runtime: Any | None = None) -> AgentControlClient:
        """Create and connect the Preloop WebSocket client."""
        config_path = self.config_path or Path.home() / ".hermes" / "config.yaml"

        async def send_message(command: OperatorCommand) -> AgentControlResult:
            metadata = dict(command.metadata)
            if command.input_mode in {"voice", "voice_transcript"}:
                if hermes_runtime and hasattr(hermes_runtime, "send_voice_transcript"):
                    reply = await hermes_runtime.send_voice_transcript(
                        command.text,
                        metadata,
                    )
                elif hermes_runtime and hasattr(hermes_runtime, "send_prompt"):
                    reply = await hermes_runtime.send_prompt(command.text, metadata)
                else:
                    raise RuntimeError("Hermes runtime voice hook is not available")
            elif hermes_runtime and hasattr(hermes_runtime, "send_prompt"):
                reply = await hermes_runtime.send_prompt(command.text, metadata)
            else:
                raise RuntimeError("Hermes runtime hook send_prompt is not available")
            reply_text = str(reply) if reply is not None else ""
            return AgentControlResult(
                reply_text=reply_text,
                session_reference=command.session_reference,
            )

        async def interrupt_session(session_reference: str | None) -> None:
            if hermes_runtime and hasattr(hermes_runtime, "interrupt"):
                await hermes_runtime.interrupt({"session_reference": session_reference})
                return
            raise RuntimeError("Hermes runtime interrupt hook is not available")

        # Gate Hermes's native tool calls through Preloop approval when the
        # runtime exposes the plugin hook registry.
        if hermes_runtime is not None and hasattr(hermes_runtime, "register_hook"):
            self.register(hermes_runtime)

        caps = self.capabilities()
        self.client = create_hermes_agent_control_client(
            config_path,
            send_message=send_message,
            interrupt_session=interrupt_session,
            supports_interrupt=caps.supports_interrupt,
            supports_voice=caps.supports_voice,
            supports_tool_approval=caps.supports_tool_approval,
        )
        await self.client.run_forever()
        return self.client

    def register(self, ctx: Any) -> None:
        """Register Hermes lifecycle hooks (the native tool-approval gate).

        Hermes calls this with its plugin context at load time. ``ctx`` is
        expected to expose ``register_hook(event_name, callback)``.

        Args:
            ctx: The Hermes plugin context / runtime hook registry.

        Raises:
            RuntimeError: If the context cannot register hooks.
        """
        register_hook = getattr(ctx, "register_hook", None)
        if not callable(register_hook):
            raise RuntimeError("Hermes plugin context does not expose register_hook")
        register_hook(HOOK_EVENT_PRE_TOOL_CALL, self.pre_tool_call)

    async def pre_tool_call(
        self, payload: Any = None, /, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Gate one native Hermes tool call through Preloop approval.

        Accepts both Hermes calling conventions: a single event payload dict
        (shell-hook form) and/or keyword arguments (Python plugin form).

        Args:
            payload: Event payload mapping, when Hermes passes one positionally.
            **kwargs: Event fields when Hermes passes them as keywords.

        Returns:
            Upstream block envelope ``{"action": "block", "message": ...}``
            (plus plan-era ``decision``/``reason`` aliases) to veto the tool
            call, or ``None`` to allow it.
        """
        event: dict[str, Any] = {}
        if isinstance(payload, dict):
            event.update(payload)
        # Python plugin form: register_hook("pre_tool_call", cb) calls
        # cb(tool_name=..., args=..., task_id=...). A bare string first arg is
        # treated as tool_name when kwargs omit it.
        if isinstance(payload, str) and "tool_name" not in kwargs:
            event["tool_name"] = payload
        if kwargs:
            event.update(kwargs)

        tool_name = _coerce_optional_str(event.get("tool_name"))
        if not tool_name:
            # Non-tool lifecycle events carry no tool to gate.
            return None

        tool_input = event.get("tool_input")
        if not isinstance(tool_input, dict):
            args = event.get("args")
            tool_input = args if isinstance(args, dict) else {}

        extra = event.get("extra") if isinstance(event.get("extra"), dict) else {}
        session_id = _coerce_optional_str(
            event.get("session_id") or event.get("task_id") or extra.get("task_id")
        )
        cwd = _coerce_optional_str(event.get("cwd"))
        agent_reasoning = _coerce_optional_str(
            event.get("agent_reasoning")
            or extra.get("agent_reasoning")
            or extra.get("reasoning")
        )
        # Hermes has no Claude-style allow/deny/ask lists for native tools; the
        # pre_tool_call hook fires on every call, so escalate as "ask" unless
        # the operator disabled the gate.
        return await self._check_permission(
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=session_id,
            cwd=cwd,
            agent_reasoning=agent_reasoning,
            client_decision="ask",
        )

    async def _check_permission(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        session_id: str | None,
        cwd: str | None,
        agent_reasoning: str | None,
        client_decision: str | None = "ask",
    ) -> dict[str, Any] | None:
        """Call the Preloop permission-check endpoint and map the decision."""
        config, approval = self._load_control_settings()
        if not approval.get("enabled", True):
            return None
        fail_open = _resolve_fail_open(approval)
        url = _permission_check_url(config.control_ws_url)
        body: dict[str, Any] = {
            "source": "hermes",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "session_id": session_id,
            "cwd": cwd,
            "agent_reasoning": agent_reasoning,
        }
        if client_decision:
            body["client_decision"] = client_decision
        headers = {
            "Authorization": f"Bearer {config.bearer_token}",
            "Content-Type": "application/json",
        }
        try:
            data = await self._request_decision(url, body, headers)
        except Exception as exc:  # network/timeout/HTTP error
            logger.warning(
                "Preloop permission check failed for tool %s: %s", tool_name, exc
            )
            if fail_open:
                return None
            return _block(f"Preloop approval unavailable: {exc}")

        decision = str(data.get("decision") or "").strip().lower()
        if decision == "deny":
            reason = str(data.get("reason") or "Denied by Preloop approval")
            return _block(reason)
        return None

    async def _request_decision(
        self, url: str, body: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        """POST the permission-check request and return the decoded response."""
        timeout = aiohttp.ClientTimeout(total=PERMISSION_CHECK_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=body, headers=headers, timeout=timeout
            ) as response:
                response.raise_for_status()
                return await response.json()

    def _load_control_settings(self) -> tuple[AgentControlConfig, dict[str, Any]]:
        """Resolve the control config plus the optional ``tool_approval`` block."""
        if self._control_settings is None:
            block = self._read_control_block()
            config = AgentControlConfig.from_control_block(block)
            raw_approval = block.get("tool_approval")
            approval = raw_approval if isinstance(raw_approval, dict) else {}
            self._control_settings = (config, approval)
        return self._control_settings

    def _read_control_block(self) -> dict[str, Any]:
        """Read the raw ``preloop.control`` mapping from Hermes config."""
        path = self.config_path or Path.home() / ".hermes" / "config.yaml"
        document = yaml.safe_load(path.read_text())
        if not isinstance(document, dict):
            raise ValueError("Hermes config root must be an object")
        preloop = document.get("preloop")
        control = preloop.get("control") if isinstance(preloop, dict) else None
        if not isinstance(control, dict):
            raise ValueError("missing preloop.control config block")
        return control

    def verify(self) -> None:
        """Validate local plugin load and config shape."""
        config = self.load_config()
        if config.runtime != self.runtime_name:
            raise ValueError(f"Expected Hermes runtime config, got {config.runtime!r}")
        if not config.control_ws_url:
            raise ValueError("preloop.control.control_ws_url is required")
        if not config.bearer_token:
            raise ValueError("preloop.control.bearer_token is required")

    def login(self, base_url: str) -> None:
        """Bootstrap Preloop auth and write Hermes Agent Control config."""
        config_path = self.config_path or Path.home() / ".hermes" / "config.yaml"
        runtime_principal_id = f"hermes-{uuid4()}"
        authorize_url = _build_url(
            base_url,
            "/oauth/authorize",
            {
                "response_type": "code",
                "client_id": "cli",
                "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                "scope": "offline_access",
            },
        )
        print("Open this Preloop login/signup URL:")
        print(authorize_url)
        code = input("Paste the authorization code: ").strip()
        if not code:
            raise ValueError("Authorization code is required")

        token_response = _post_form(
            _build_url(base_url, "/oauth/token"),
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "cli",
                "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            },
        )
        access_token = str(token_response.get("access_token") or "")
        if not access_token:
            raise ValueError("Preloop did not return an access token")

        runtime_session = _post_json(
            _build_url(base_url, "/api/v1/auth/runtime-sessions/token"),
            {
                "session_source_type": "hermes",
                "session_source_id": runtime_principal_id,
                "session_reference": str(config_path),
                "runtime_principal_name": "Hermes",
            },
            access_token,
        )
        runtime_token = str(runtime_session.get("token") or "")
        if not runtime_token:
            raise ValueError("Preloop did not return a runtime token")

        document: dict[str, Any] = {}
        if config_path.exists():
            loaded = yaml.safe_load(config_path.read_text())
            if isinstance(loaded, dict):
                document = loaded
        preloop = document.setdefault("preloop", {})
        preloop["control"] = {
            "enabled": True,
            "protocol": "preloop.agent_control.v1",
            "runtime": "hermes",
            "control_ws_url": _websocket_url(base_url),
            "bearer_token": runtime_token,
            "managed_agent_id": runtime_session.get("managed_agent_id"),
            "runtime_session_id": runtime_session.get("runtime_session_id"),
            "runtime_principal_id": runtime_principal_id,
            "runtime_principal_name": "Hermes",
            "session_reference": str(config_path),
        }
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(yaml.safe_dump(document, sort_keys=False))
        print(f"Wrote Preloop Agent Control config to {config_path}")


plugin = HermesPreloopPlugin()


def register(ctx: Any) -> None:
    """Module-level entry point Hermes can call to wire plugin hooks."""
    plugin.register(ctx)


def main() -> None:
    """CLI helper used by marketplace verification commands."""
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["login", "run", "verify"])
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--base-url", default="https://app.preloop.ai")
    args = parser.parse_args()

    instance = HermesPreloopPlugin(args.config)
    if args.command == "login":
        instance.login(args.base_url)
        return
    if args.command == "verify":
        instance.verify()
        print("preloop-hermes-plugin verified")
        return

    asyncio.run(instance.start())


def _build_url(
    base_url: str,
    path: str,
    query: dict[str, str] | None = None,
) -> str:
    url = base_url.rstrip("/") + path
    if query:
        url += "?" + parse.urlencode(query)
    return url


def _websocket_url(base_url: str) -> str:
    parsed = parse.urlparse(base_url.rstrip() + "/api/v1/agents/control/ws")
    scheme = "ws" if parsed.scheme == "http" else "wss"
    return parse.urlunparse(parsed._replace(scheme=scheme))


def _permission_check_url(control_ws_url: str) -> str:
    """Derive the permission-check HTTP URL from the control WebSocket URL."""
    parsed = parse.urlparse(control_ws_url)
    scheme = "https" if parsed.scheme in {"wss", "https"} else "http"
    return f"{scheme}://{parsed.netloc}{PERMISSION_CHECK_PATH}"


def _block(reason: str) -> dict[str, Any]:
    """Build the Hermes block envelope vetoing a tool call.

    Upstream Hermes (``get_pre_tool_call_block_message``) requires
    ``{"action": "block", "message": <non-empty str>}``. The plan-era
    ``decision``/``reason`` aliases are included for compatibility with older
    docs and tests; Hermes ignores unknown keys.
    """
    return {
        "action": "block",
        "message": reason,
        "decision": "block",
        "reason": reason,
    }


def _resolve_fail_open(approval: dict[str, Any]) -> bool:
    """Decide fail-open behavior from env override or the config flag."""
    env = os.getenv("PRELOOP_TOOL_APPROVAL_FAIL_OPEN")
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes", "on"}
    return bool(approval.get("fail_open", False))


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    resolved = str(value).strip()
    return resolved or None


def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    data = parse.urlencode(fields).encode()
    req = request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with request.urlopen(req, timeout=30) as response:
        return json.loads(response.read())


def _post_json(url: str, body: dict[str, Any], access_token: str) -> dict[str, Any]:
    req = request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
    )
    with request.urlopen(req, timeout=30) as response:
        return json.loads(response.read())


if __name__ == "__main__":
    main()
