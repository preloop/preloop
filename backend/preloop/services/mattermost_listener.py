"""Leased, authenticated Mattermost DM ingress; disconnected gaps are not replayed."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Callable

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from preloop.api.endpoints.chat import provider_config
from preloop.models.crud import crud_chat, crud_account
from preloop.models.db.session import get_session_factory
from preloop.services.chat_providers import (
    ChatProviderConfig,
    ChatProviderError,
    MAX_EVENT_BYTES,
    MAX_MESSAGE_CHARS,
    validate_mattermost_url,
)
from preloop.services.chat_setup import identifier, provider_request

logger = logging.getLogger(__name__)


async def verify_team_actor(
    config: ChatProviderConfig, user_id: str, *, client: httpx.AsyncClient | None = None
) -> None:
    """A DM's empty team_id never establishes workspace access."""
    for member in (config.bot_user_id, user_id):
        data = await provider_request(
            config,
            "GET",
            f"/teams/{identifier(config.workspace_id)}/members/{identifier(member)}",
            client=client,
        )
        if data.get("team_id") != config.workspace_id or data.get("user_id") != member:
            raise ChatProviderError(
                "Actor or bot is not an active member of the selected team"
            )
        deleted = data.get("delete_at", 0)
        if deleted == -1:
            # Mattermost redacts another member's role and deletion fields for
            # ordinary bots. A team-filtered search excludes removed members.
            user = await provider_request(
                config, "GET", f"/users/{identifier(member)}", client=client
            )
            username = user.get("username")
            if (
                user.get("id") != member
                or user.get("delete_at")
                or not isinstance(username, str)
                or not username
            ):
                raise ChatProviderError("Actor is unavailable")
            matches = await provider_request(
                config,
                "POST",
                "/users/search",
                client=client,
                payload={
                    "team_id": config.workspace_id,
                    "term": username,
                    "allow_inactive": False,
                    "limit": 100,
                },
            )
            if not isinstance(matches, list) or not any(
                isinstance(match, dict)
                and match.get("id") == member
                and not match.get("delete_at")
                for match in matches
            ):
                raise ChatProviderError(
                    "Actor is not an active member of the selected team"
                )
        elif deleted:
            raise ChatProviderError(
                "Actor or bot is not an active member of the selected team"
            )


async def canonical_dm(
    config: ChatProviderConfig,
    post_id: str,
    since_ms: int,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Trust canonical authenticated API data, never WebSocket post/user fields."""

    async def get(path: str) -> Any:
        return await provider_request(config, "GET", path, client=client)

    post = await get(f"/posts/{identifier(post_id)}")
    if post.get("id") != post_id:
        return None
    created = post.get("create_at", 0)
    if (
        not isinstance(created, int)
        or not since_ms <= created <= int(time.time() * 1000) + 5000
    ):
        return None
    if (
        post.get("delete_at")
        or post.get("type")
        or post.get("user_id") == config.bot_user_id
    ):
        return None
    user_id, channel_id = (
        identifier(post.get("user_id")),
        identifier(post.get("channel_id")),
    )
    user = await get(f"/users/{user_id}")
    if user.get("is_bot") or user.get("delete_at") or user.get("id") != user_id:
        return None
    channel = await get(f"/channels/{channel_id}")
    # Initial release is DM-only; no implicit public-channel or group-DM access.
    if channel.get("type") != "D" or channel.get("id") != channel_id:
        return None
    for member_id in (config.bot_user_id, user_id):
        member = await get(f"/channels/{channel_id}/members/{member_id}")
        if member.get("user_id") != member_id or member.get("channel_id") != channel_id:
            return None
    await verify_team_actor(config, user_id, client=client)
    text = post.get("message")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE_CHARS:
        return None
    plain_text = text if not text.startswith("/") else None
    verb = text.split(maxsplit=1)[0]
    if text.strip() in {"end", "status"}:
        text = "/" + text.strip()
    elif (verb == "link" and len(text.split()) == 2) or verb in {
        "note",
        "message",
        "approve",
        "deny",
        "ask",
        "help",
    }:
        text = "/" + text.strip()
    payload: dict[str, Any] = {
        "text": text,
        "private": True,
        "channel_id": channel_id,
        "thread_id": str(post.get("root_id") or post_id),
        "transport": "websocket",
    }
    if plain_text is not None and not text.startswith("/link"):
        payload["plain_text"] = plain_text
    if text.startswith("/link "):
        payload.update(
            link_digest=hashlib.sha256(
                text.split(maxsplit=1)[1].strip().encode()
            ).hexdigest(),
            text="/link",
        )
    return {
        "event_id": identifier(post.get("id")),
        "external_user_id": user_id,
        "payload": payload,
    }


async def listen_connection(
    connection_id: uuid.UUID,
    stop: asyncio.Event,
    *,
    session_factory: Callable[..., Any] = get_session_factory,
) -> None:
    """Renew replica ownership, reconnect with backoff, and stop on revocation.

    No historical messages are replayed after reconnect: only posts created
    since this socket's authenticated connection are accepted. Stable post IDs
    deduplicate repeat frames. Queue processing/delivery remains durable.
    """
    token, delay = str(uuid.uuid4()), 1
    while not stop.is_set():
        with session_factory()() as db:
            row = crud_chat.connections.get(db, connection_id)
            account = crud_account.get(db, id=row.account_id) if row else None
            if not row or not row.enabled or not account or not account.is_active:
                return
            if not crud_chat.claim_listener(db, connection_id, token):
                await asyncio.sleep(5)
                continue
            config = provider_config(row)
            original_credentials = row.credentials_encrypted
        origin = validate_mattermost_url(config.base_url)
        try:
            from preloop.services.chat_network import (
                mattermost_websocket_options,
                authenticate_mattermost_socket,
            )

            socket_options = await mattermost_websocket_options(origin)
            async with connect(
                "wss://" + origin.removeprefix("https://") + "/api/v4/websocket",
                max_size=MAX_EVENT_BYTES,
                open_timeout=10,
                ping_interval=20,
                ping_timeout=20,
                **socket_options,
            ) as websocket:
                await authenticate_mattermost_socket(websocket, config.bot_token)
                since_ms = int(time.time() * 1000)
                with session_factory()() as db:
                    if not crud_chat.listener_health(
                        db,
                        connection_id,
                        token,
                        "connected",
                        connected_at=datetime.utcnow(),
                        last_error=None,
                    ):
                        return
                delay = 1
                while not stop.is_set():
                    with session_factory()() as db:
                        row = crud_chat.connections.get(db, connection_id)
                        account = (
                            crud_account.get(db, id=row.account_id) if row else None
                        )
                        if (
                            not row
                            or not row.enabled
                            or row.credentials_encrypted != original_credentials
                            or not account
                            or not account.is_active
                            or not crud_chat.claim_listener(db, connection_id, token)
                        ):
                            return
                    try:
                        frame = await asyncio.wait_for(websocket.recv(), 10)
                    except asyncio.TimeoutError:
                        continue
                    event = json.loads(frame)
                    if event.get("event") != "posted":
                        continue
                    data = event.get("data", {})
                    posted = json.loads(data.get("post", "{}"))
                    inbound = await canonical_dm(
                        config, identifier(posted.get("id")), since_ms
                    )
                    if inbound is None:
                        continue
                    with session_factory()() as db:
                        row = crud_chat.connections.get(db, connection_id)
                        if (
                            not row
                            or not row.enabled
                            or row.credentials_encrypted != original_credentials
                            or not crud_chat.claim_listener(db, connection_id, token)
                        ):
                            return
                        actor = crud_chat.principal(
                            db, row, inbound["external_user_id"]
                        )
                        if actor:
                            inbound["payload"]["actor_id"] = str(actor.id)
                        crud_chat.receive(db, connection=row, **inbound)
                        crud_chat.listener_health(
                            db,
                            connection_id,
                            token,
                            "connected",
                            last_event_at=datetime.utcnow(),
                        )
        except asyncio.CancelledError:
            raise
        except (
            ChatProviderError,
            httpx.HTTPError,
            WebSocketException,
            TimeoutError,
            ValueError,
            KeyError,
            TypeError,
        ):
            with session_factory()() as db:
                crud_chat.listener_health(
                    db,
                    connection_id,
                    token,
                    "reconnecting",
                    last_error="Provider unavailable or credentials revoked; disconnected messages are not replayed",
                )
            # Do not log raw provider frames/exception messages or credentials.
            logger.warning("Mattermost listener reconnecting for %s", connection_id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        delay = min(delay * 2, 30)
