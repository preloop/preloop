"""Opt-in Discord Gateway bridge for selected employee subjects.

Run with ``python -m preloop.integrations.discord_employee_bridge``. Bot token
and HMAC secret are read from the environment; credentials are never logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from typing import Any
from pathlib import Path

import aiohttp


def normalize_discord_event(
    name: str,
    data: dict[str, Any],
    *,
    guild_id: str,
    channel_ids: frozenset[str],
) -> dict[str, Any] | None:
    """Select configured guild/channels and exclude bot or webhook replies."""
    if not isinstance(data, dict) or data.get("guild_id") != guild_id:
        return None
    if name == "GUILD_MEMBER_ADD":
        user = data.get("user", {})
        if (
            not isinstance(user, dict)
            or user.get("bot")
            or not user.get("id")
            or not data.get("joined_at")
        ):
            return None
        return {
            "event_id": f"join:{guild_id}:{user['id']}:{data['joined_at']}",
            "kind": "member_joined",
            "subject": f"guild:{guild_id}:members",
            "payload": {
                "guild_id": guild_id,
                "user_id": user["id"],
                "joined_at": data["joined_at"],
            },
        }
    if name == "MESSAGE_CREATE":
        author = data.get("author", {})
        if (
            not isinstance(author, dict)
            or data.get("channel_id") not in channel_ids
            or author.get("bot")
            or data.get("webhook_id")
            or not data.get("id")
        ):
            return None
        return {
            "event_id": data["id"],
            "kind": "channel_message",
            "subject": f"guild:{guild_id}:channel:{data['channel_id']}",
            "payload": {
                "guild_id": guild_id,
                "channel_id": data["channel_id"],
                "message_id": data["id"],
                "user_id": author.get("id"),
                "content": str(data.get("content", ""))[:16000],
            },
        }
    return None


async def run_bridge(
    *,
    token: str,
    endpoint: str,
    secret: str,
    guild_id: str,
    channel_ids: frozenset[str],
    state_path: Path | None = None,
) -> None:
    """Read Gateway events and sign bounded HTTP deliveries to owned ingress.

    Discord must enable Server Members and (for selected channel text) Message
    Content privileged intents for this application. No outbound Discord messages
    are sent. Stable provider IDs permit safe retry and replay at the receiver.
    """
    if len(secret) < 32:
        raise ValueError("Employee ingress secret must contain at least 32 characters")
    if not endpoint.startswith("https://"):
        raise ValueError("Employee ingress must use HTTPS")
    sequence: int | None = None
    session_id: str | None = None
    gateway = "wss://gateway.discord.gg/?v=10&encoding=json"
    state_path = state_path or Path.home() / ".preloop" / (
        "discord-employee-"
        + hashlib.sha256((endpoint + guild_id).encode()).hexdigest()[:16]
        + ".json"
    )
    if state_path.exists():
        checkpoint = json.loads(state_path.read_text())
        if (
            checkpoint.get("endpoint") != endpoint
            or checkpoint.get("guild_id") != guild_id
        ):
            raise ValueError("Discord checkpoint belongs to another connection")
        session_id, sequence = checkpoint.get("session_id"), checkpoint.get("sequence")
        gateway = checkpoint.get("gateway", gateway)

    acknowledged_sequence = sequence

    def save_checkpoint() -> None:
        nonlocal acknowledged_sequence
        state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = state_path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            json.dump(
                {
                    "endpoint": endpoint,
                    "guild_id": guild_id,
                    "session_id": session_id,
                    "sequence": sequence,
                    "gateway": gateway,
                },
                output,
            )
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(state_path)
        acknowledged_sequence = sequence

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as client:
        while True:
            try:
                async with client.ws_connect(gateway, heartbeat=None) as websocket:
                    hello = await websocket.receive_json()
                    interval = hello["d"]["heartbeat_interval"] / 1000
                    if session_id:
                        await websocket.send_json(
                            {
                                "op": 6,
                                "d": {
                                    "token": token,
                                    "session_id": session_id,
                                    "seq": sequence,
                                },
                            }
                        )
                    else:
                        await websocket.send_json(
                            {
                                "op": 2,
                                "d": {
                                    "token": token,
                                    "intents": 2 | 512 | (32768 if channel_ids else 0),
                                    "properties": {
                                        "os": "linux",
                                        "browser": "preloop",
                                        "device": "preloop",
                                    },
                                },
                            }
                        )

                    async def heartbeat() -> None:
                        while True:
                            await asyncio.sleep(interval)  # noqa: B023
                            await websocket.send_json({"op": 1, "d": sequence})  # noqa: B023

                    beat = asyncio.create_task(heartbeat())
                    try:
                        async for frame in websocket:
                            if frame.type != aiohttp.WSMsgType.TEXT:
                                break
                            packet = json.loads(frame.data)
                            if packet.get("s") is not None:
                                sequence = packet["s"]
                            if packet.get("op") == 7:
                                break
                            if packet.get("op") == 9:
                                session_id, sequence = None, None
                                save_checkpoint()
                                break
                            if packet.get("op") == 1:
                                await websocket.send_json({"op": 1, "d": sequence})  # noqa: B023
                            if packet.get("t") == "READY":
                                session_id = packet["d"]["session_id"]
                                resume_url = packet["d"].get("resume_gateway_url")
                                if isinstance(
                                    resume_url, str
                                ) and resume_url.startswith("wss://"):
                                    gateway = resume_url + "/?v=10&encoding=json"
                            event = normalize_discord_event(
                                packet.get("t", ""),
                                packet.get("d") or {},
                                guild_id=guild_id,
                                channel_ids=channel_ids,
                            )
                            if event:
                                event["occurred_at"] = datetime.now(UTC).isoformat()
                                body = json.dumps(event, separators=(",", ":")).encode()
                                signature = hmac.new(
                                    secret.encode(), body, hashlib.sha256
                                ).hexdigest()
                                for attempt in range(5):
                                    async with client.post(
                                        endpoint,
                                        data=body,
                                        headers={
                                            "Content-Type": "application/json",
                                            "X-Preloop-Signature": signature,
                                        },
                                    ) as response:
                                        if response.status < 300:
                                            break
                                        if (
                                            response.status < 500
                                            and response.status != 429
                                        ):
                                            raise ValueError(
                                                "Employee ingress rejected configured event"
                                            )
                                    await asyncio.sleep(min(2**attempt, 16))
                                else:
                                    # Resume before this event so transport failure cannot lose it.
                                    sequence = acknowledged_sequence
                                    raise aiohttp.ClientConnectionError(
                                        "Employee ingress unavailable"
                                    )
                            save_checkpoint()
                    finally:
                        beat.cancel()
                        await asyncio.gather(beat, return_exceptions=True)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                # A failed POST may have accepted the event or may never have
                # reached ingress. Resume from the durable acknowledgement;
                # intake deduplicates accepted events. Never skip the failed one.
                sequence = acknowledged_sequence
                await asyncio.sleep(5)


def main() -> None:
    """Launch the explicitly configured bridge."""
    asyncio.run(
        run_bridge(
            token=os.environ["DISCORD_BOT_TOKEN"],
            endpoint=os.environ["PRELOOP_EMPLOYEE_EVENT_URL"],
            secret=os.environ["PRELOOP_EMPLOYEE_EVENT_SECRET"],
            guild_id=os.environ["DISCORD_GUILD_ID"],
            channel_ids=frozenset(
                filter(None, os.environ.get("DISCORD_CHANNEL_IDS", "").split(","))
            ),
        )
    )


if __name__ == "__main__":
    main()
