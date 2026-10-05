"""Enrollment, local validation and governed runtime entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp

from .runtime import ConcurrentControlClient, build_runtime, validate_document

# verify() refuses any other nanobot-ai release. The adapter is written
# against this SDK's AgentLoop and OpenAICompatProvider.
PINNED_NANOBOT_SDK = "0.2.1"


def discover(explicit: Path | None = None) -> Path:
    """Find isolated Preloop configuration without altering native channels."""
    return (
        explicit
        or Path(os.environ.get("NANOBOT_HOME", "~/.nanobot")).expanduser()
        / "preloop.json"
    )


async def enroll(path: Path, base_url: str, access_token: str) -> None:
    """Exchange a logged-in user's token for an isolated runtime credential."""
    endpoint = urlsplit(base_url)
    if (
        endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
        or not endpoint.hostname
    ):
        raise ValueError("invalid enrollment endpoint")
    if endpoint.scheme != "https" and not (
        endpoint.scheme == "http"
        and endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("enrollment requires HTTPS or loopback HTTP")
    if not access_token or any(c in access_token for c in "\r\n"):
        raise ValueError("PRELOOP_ACCESS_TOKEN must contain a valid user credential")
    if path.exists():
        raise ValueError("configuration exists; back it up before re-enrolling")
    principal = f"nanobot-{uuid4()}"
    async with aiohttp.ClientSession(
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as client:
        async with client.post(
            base_url.rstrip("/") + "/api/v1/auth/runtime-sessions/token",
            json={
                "session_source_type": "nanobot",
                "session_source_id": principal,
                "session_reference": principal,
                "runtime_principal_name": "Nanobot",
            },
        ) as response:
            response.raise_for_status()
            result = await response.json()
    document: dict[str, Any] = {
        "model": "deepseek-chat",
        "preloop": {
            "control": {
                "runtime": "nanobot",
                "control_ws_url": base_url.rstrip("/")
                .replace("https://", "wss://")
                .replace("http://", "ws://")
                + "/api/v1/agents/control/ws",
                "bearer_token": result.get("token"),
                "runtime_principal_id": principal,
                "managed_agent_id": result.get("managed_agent_id"),
                "runtime_session_id": result.get("runtime_session_id"),
                "session_source_type": "nanobot",
                "session_source_id": principal,
            }
        },
    }
    validate_document(document)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation avoids replacing another enrollment during concurrent setup.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(document, handle, indent=2)


def main() -> None:
    """Run the installed standalone integration command."""
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["enroll", "verify", "run"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--base-url", default="https://app.preloop.ai")
    args = parser.parse_args()
    path = discover(args.config)
    if args.command == "enroll":
        asyncio.run(
            enroll(path, args.base_url, os.environ.get("PRELOOP_ACCESS_TOKEN", ""))
        )
        print("Nanobot enrolled; run preloop-nanobot-plugin run")
        return
    document = json.loads(path.read_text())
    validate_document(document)
    if args.command == "verify":
        from importlib.metadata import version

        if version("nanobot-ai") != PINNED_NANOBOT_SDK:
            raise ValueError("unsupported Nanobot SDK version")
        print("Nanobot configuration and pinned SDK verified (connectivity not tested)")
        return
    runtime = build_runtime(document, path.with_name("preloop-sessions.json"))
    asyncio.run(ConcurrentControlClient(runtime).run_forever())
