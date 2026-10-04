"""Runnable offline end-to-end fixture using the pinned Nanobot SDK.

Run with PRELOOP_DISABLE_TELEMETRY=true and the plugin/SDK environment active.
No HTTP or channel message is sent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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
        original_loop = runtime.loop

        def offline_scoped_runtime(scoped_document, state, *, model_base_url):
            scoped = build_runtime(
                scoped_document, state, model_base_url=model_base_url
            )
            assert scoped.config.bearer_token == "synthetic-execution-token"
            assert scoped.loop.tools.config.bearer_token == "synthetic-execution-token"
            assert (
                scoped.loop.provider.provider._client.api_key
                == "synthetic-execution-token"
            )
            assert (
                str(scoped.loop.provider.provider._client.base_url)
                == "https://gateway.example.com/openai/v1/"
            )
            scoped.loop.provider.provider = OfflineProvider()
            scoped.loop._mcp_servers = {}
            return scoped

        with patch(
            "preloop_nanobot_plugin.runtime.build_runtime",
            side_effect=offline_scoped_runtime,
        ):
            employee = await runtime.handle_send_message(
                OperatorCommand(
                    "employee-fixture",
                    "Process the example issue.",
                    session_mode="new",
                    metadata={
                        "employee_task_key": "example-task",
                        "gateway": {
                            "api_key": "synthetic-execution-token",
                            "base_url": "https://gateway.example.com/openai/v1",
                            "api_url": "https://example.com",
                            "model": "deepseek-chat",
                        },
                        "run_limits": {
                            "max_turns": 10,
                            "max_total_tokens": 32000,
                            "max_usd": 2,
                            "max_history_chars": 64000,
                            "timeout_seconds": 300,
                        },
                    },
                )
            )
        assert employee.reply_text == "Completed local fixture."
        assert employee.session_reference != first.session_reference
        assert runtime.loop is original_loop
        assert runtime.config.bearer_token == "synthetic-runtime-token"
        print(
            "Pinned SDK lifecycle and scoped employee fixtures passed; no external request sent."
        )


if __name__ == "__main__":
    asyncio.run(main())
