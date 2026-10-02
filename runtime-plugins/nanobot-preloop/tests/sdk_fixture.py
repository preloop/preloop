"""Runnable offline end-to-end fixture using the pinned Nanobot SDK.

Run with PRELOOP_DISABLE_TELEMETRY=true and the plugin/SDK environment active.
No HTTP or channel message is sent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from nanobot.providers.base import LLMResponse

from preloop.integrations.agent_control import OperatorCommand
from preloop_nanobot_plugin.runtime import build_runtime


class OfflineProvider:
    """Return a real Nanobot model response through its provider interface."""

    async def chat(self, *args: object, **kwargs: object) -> LLMResponse:
        return LLMResponse(
            content="Completed local fixture.",
            finish_reason="stop",
            usage={"total_tokens": 10},
        )


async def main() -> None:
    """Verify SDK processing, owned resume and persisted session identity."""
    with TemporaryDirectory() as directory:
        document = {
            "workspace": directory,
            "preloop": {
                "control": {
                    "control_ws_url": "wss://example.com/api/v1/agents/control/ws",
                    "bearer_token": "synthetic-runtime-token",
                    "runtime_principal_id": "synthetic-principal",
                }
            },
        }
        runtime = build_runtime(document, Path(directory) / "state.json")
        runtime.loop.provider.provider = OfflineProvider()
        runtime.loop._mcp_servers = {}
        first = await runtime.handle_send_message(
            OperatorCommand("first", "Describe this fixture.", session_mode="new")
        )
        second = await runtime.handle_send_message(
            OperatorCommand(
                "second",
                "Continue.",
                session_mode="existing",
                session_reference=first.session_reference,
            )
        )
        assert first.reply_text == second.reply_text == "Completed local fixture."
        assert first.session_reference == second.session_reference
        print("Pinned SDK lifecycle fixture passed; no external request sent.")


if __name__ == "__main__":
    asyncio.run(main())
