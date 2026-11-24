"""Authenticate chat ingress and deliver private replies through provider APIs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import ssl
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MAX_EVENT_BYTES = 65_536
MAX_MESSAGE_CHARS = 8_000


class ChatProviderError(ValueError):
    """The request is invalid or the provider refused delivery."""


@dataclass(frozen=True)
class ChatProviderConfig:
    """Server-owned provider credentials, never returned to the model."""

    provider: str
    workspace_id: str
    verification_secret: str
    bot_token: str
    bot_user_id: str = ""
    base_url: str = ""
    slack_refresh_token: str = ""
    slack_token_expires_at: float = 0.0


@dataclass(frozen=True)
class ChatInbound:
    """An authenticated external identity and bounded message."""

    event_id: str
    user_id: str
    channel_id: str
    text: str
    is_private: bool
    thread_id: str = ""


@dataclass(frozen=True)
class ChatIngress:
    """Immediate provider acknowledgement and optional durable work."""

    acknowledgement: dict[str, Any]
    event: ChatInbound | None = None


def _timestamp(value: str, now: float) -> None:
    try:
        timestamp = int(value)
    except (ValueError, TypeError) as exc:
        raise ChatProviderError("Invalid request timestamp") from exc
    if abs(now - timestamp) > 300:
        raise ChatProviderError("Expired request timestamp")


def _object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ChatProviderError("Invalid event body") from exc
    if not isinstance(value, dict):
        raise ChatProviderError("Expected an event object")
    return value


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ChatProviderError(f"Invalid {label}")
    return value


def _string(value: Any, label: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ChatProviderError(f"Invalid {label}")
    return value


def verify_ingress(
    config: ChatProviderConfig,
    raw: bytes,
    headers: Mapping[str, str],
    *,
    now: float | None = None,
) -> ChatIngress:
    """Verify a provider signature before trusting any identity or message."""
    if not config.verification_secret or len(raw) > MAX_EVENT_BYTES:
        raise ChatProviderError("Invalid request")
    headers = {k.lower(): v for k, v in headers.items()}
    now = time.time() if now is None else now
    if config.provider == "slack":
        timestamp = headers.get("x-slack-request-timestamp", "")
        _timestamp(timestamp, now)
        digest = hmac.new(
            config.verification_secret.encode(),
            b"v0:" + timestamp.encode() + b":" + raw,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(
            "v0=" + digest, headers.get("x-slack-signature", "")
        ):
            raise ChatProviderError("Invalid provider signature")
        body = _object(raw)
        if body.get("type") == "url_verification":
            return ChatIngress(
                {"challenge": _string(body.get("challenge"), "challenge")}
            )
        if body.get("team_id") != config.workspace_id:
            raise ChatProviderError("Workspace does not match connection")
        event = body.get("event")
        if not isinstance(event, dict):
            raise ChatProviderError("Missing event")
        if (
            event.get("type") not in {"app_mention", "message"}
            or event.get("subtype")
            or event.get("bot_id")
            or event.get("user") == config.bot_user_id
        ):
            return ChatIngress({"ok": True})
        # Public channel messages require an explicit app mention subscription.
        if event.get("type") == "message" and event.get("channel_type") != "im":
            return ChatIngress({"ok": True})
        inbound = ChatInbound(
            event_id=_string(body.get("event_id"), "event ID"),
            user_id=_string(event.get("user"), "user ID"),
            channel_id=_string(event.get("channel"), "channel ID"),
            text=_string(event.get("text"), "message", MAX_MESSAGE_CHARS),
            is_private=event.get("channel_type") == "im",
            thread_id=str(event.get("thread_ts") or event.get("ts") or ""),
        )
        return ChatIngress({"ok": True}, inbound)
    if config.provider == "discord":
        timestamp = headers.get("x-signature-timestamp", "")
        _timestamp(timestamp, now)
        try:
            key = Ed25519PublicKey.from_public_bytes(
                bytes.fromhex(config.verification_secret)
            )
            key.verify(
                bytes.fromhex(headers.get("x-signature-ed25519", "")),
                timestamp.encode() + raw,
            )
        except (ValueError, InvalidSignature) as exc:
            raise ChatProviderError("Invalid provider signature") from exc
        body = _object(raw)
        if body.get("type") == 1:
            return ChatIngress({"type": 1})
        if body.get("guild_id") != config.workspace_id:
            raise ChatProviderError("Server does not match connection")
        if body.get("type") != 2:
            raise ChatProviderError("Unsupported interaction")
        command = _mapping(body.get("data"), "command")
        if command.get("name") != "preloop":
            raise ChatProviderError("Unsupported command")
        options = command.get("options", [])
        if (
            not isinstance(options, list)
            or len(options) > 25
            or any(not isinstance(item, dict) for item in options)
        ):
            raise ChatProviderError("Invalid command options")
        text = next(
            (item.get("value") for item in options if item.get("name") == "message"),
            None,
        )
        member = _mapping(body.get("member"), "member")
        user = _mapping(member.get("user"), "member user")
        if user.get("bot"):
            return ChatIngress({"type": 4, "data": {"content": "Ignored", "flags": 64}})
        inbound = ChatInbound(
            event_id=_string(body.get("id"), "interaction ID"),
            user_id=_string(user.get("id"), "user ID"),
            channel_id=_string(body.get("channel_id"), "channel ID"),
            text=_string(text, "message", MAX_MESSAGE_CHARS),
            is_private=False,
        )
        return ChatIngress(
            {"type": 4, "data": {"content": "I'll reply privately.", "flags": 64}},
            inbound,
        )
    if config.provider == "mattermost":
        # Mattermost slash commands use a per-command verification token. The
        # immutable trigger/post ID is also persisted for replay deduplication.
        try:
            form = parse_qs(raw.decode(), strict_parsing=True, max_num_fields=40)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ChatProviderError("Invalid command body") from exc
        if any(len(values) != 1 for values in form.values()):
            raise ChatProviderError("Ambiguous command fields")
        body = {key: values[0] for key, values in form.items()}
        if not hmac.compare_digest(config.verification_secret, body.get("token", "")):
            raise ChatProviderError("Invalid provider token")
        if body.get("team_id") != config.workspace_id:
            raise ChatProviderError("Team does not match connection")
        if body.get("user_id") == config.bot_user_id:
            return ChatIngress({"response_type": "ephemeral", "text": "Ignored"})
        inbound = ChatInbound(
            event_id=_string(body.get("trigger_id") or body.get("post_id"), "event ID"),
            user_id=_string(body.get("user_id"), "user ID"),
            channel_id=_string(body.get("channel_id"), "channel ID"),
            text=_string(body.get("text"), "message", MAX_MESSAGE_CHARS),
            is_private=False,
        )
        return ChatIngress(
            {"response_type": "ephemeral", "text": "I'll reply privately."}, inbound
        )
    raise ChatProviderError("Unsupported provider")


def validate_mattermost_url(value: str) -> str:
    """Require a configured HTTPS origin; message payloads never supply URLs."""
    try:
        parsed = urlparse(value)
        _ = parsed.port  # Validate malformed and out-of-range ports before network I/O.
    except ValueError as exc:
        raise ChatProviderError("Mattermost requires an HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ChatProviderError("Mattermost requires an HTTPS origin")
    return value.rstrip("/")


async def discover_mattermost_bot_user_id(base_url: str, bot_token: str) -> str:
    """Read the token owner's user ID without sending any chat messages."""
    origin = validate_mattermost_url(base_url)
    try:
        from preloop.services.chat_network import pinned_mattermost_origin

        numeric, authority, hostname = await pinned_mattermost_origin(origin)
        async with (
            asyncio.timeout(10),
            httpx.AsyncClient(
                trust_env=False, verify=ssl.create_default_context()
            ) as client,
        ):
            async with client.stream(
                "GET",
                f"{numeric}/api/v4/users/me",
                headers={"Authorization": f"Bearer {bot_token}", "Host": authority},
                extensions={"sni_hostname": hostname},
                timeout=10,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise ChatProviderError("Mattermost refused identity lookup")
                raw = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    raw.extend(chunk)
                    if len(raw) > MAX_EVENT_BYTES:
                        raise ChatProviderError(
                            "Mattermost identity response is too large"
                        )
                return _string(_object(bytes(raw)).get("id"), "bot user ID")
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, TimeoutError) as exc:
        raise ChatProviderError(
            "Could not identify the Mattermost bot. Check the server address "
            "and bot access token, or enter the bot user ID manually."
        ) from exc


async def send_private_reply(
    config: ChatProviderConfig,
    user_id: str,
    text: str,
    *,
    delivery_id: str,
    client: httpx.AsyncClient,
    channel_id: str = "",
    thread_id: str = "",
) -> str:
    """Open a verified recipient's DM and send a bounded, unmentioned reply.

    The caller owns durable delivery leases. A provider timeout is ambiguous and
    must not be blindly retried where the provider lacks an idempotency facility.
    """
    _string(user_id, "user ID")
    text = _string(text, "reply", MAX_MESSAGE_CHARS)
    headers = {"Authorization": f"Bearer {config.bot_token}"}
    extensions: dict[str, Any] = {}

    async def post(url: str, payload: Any) -> dict[str, Any]:
        response = await client.post(
            url,
            json=payload,
            headers=headers,
            timeout=15,
            follow_redirects=False,
            extensions=extensions,
        )
        if response.status_code >= 500:
            raise httpx.HTTPStatusError(
                "Provider outcome is uncertain",
                request=response.request,
                response=response,
            )
        if response.is_error:
            raise ChatProviderError(f"Chat delivery failed ({response.status_code})")
        try:
            data = response.json()
        except ValueError as exc:
            raise ChatProviderError("Invalid provider response") from exc
        if not isinstance(data, dict) or data.get("ok") is False:
            raise ChatProviderError("Chat provider refused delivery")
        return data

    if config.provider == "slack":
        if channel_id:
            channel = _string(channel_id, "conversation")
        else:
            dm = await post(
                "https://slack.com/api/conversations.open", {"users": user_id}
            )
            channel = _string(
                _mapping(dm.get("channel"), "DM channel").get("id"), "DM channel"
            )
        result = await post(
            "https://slack.com/api/chat.postMessage",
            {
                "channel": channel,
                "text": text,
                "mrkdwn": False,
                "unfurl_links": False,
                "unfurl_media": False,
                "client_msg_id": delivery_id,
                **({"thread_ts": thread_id} if thread_id else {}),
            },
        )
        return _string(result.get("ts"), "message ID")
    if config.provider == "mattermost":
        from preloop.services.chat_network import pinned_mattermost_origin

        numeric, authority, hostname = await pinned_mattermost_origin(config.base_url)
        base = numeric + "/api/v4"
        headers["Host"] = authority
        extensions["sni_hostname"] = hostname
        dm = await post(base + "/channels/direct", [config.bot_user_id, user_id])
        channel = _string(dm.get("id"), "DM channel")
        result = await post(
            base + "/posts",
            {"channel_id": channel, "message": text, "pending_post_id": delivery_id},
        )
        return _string(result.get("id"), "message ID")
    if config.provider == "discord":
        headers["Authorization"] = f"Bot {config.bot_token}"
        base = "https://discord.com/api/v10"
        dm = await post(base + "/users/@me/channels", {"recipient_id": user_id})
        channel = _string(dm.get("id"), "DM channel")
        result = await post(
            base + f"/channels/{channel}/messages",
            {
                "content": text[:2000],
                "allowed_mentions": {"parse": []},
                "nonce": delivery_id.replace("-", "")[:25],
                "enforce_nonce": True,
            },
        )
        return _string(result.get("id"), "message ID")
    raise ChatProviderError("Unsupported provider")


def conversation_scope(
    provider: str, external_user_id: str, payload: Mapping[str, Any]
) -> str:
    """Slack channel conversations belong to threads; private replies to users."""
    if provider == "slack" and not payload.get("private"):
        return f"thread:{payload.get('channel_id', '')}:{payload.get('thread_id', '')}"
    return f"dm:{external_user_id}"


def external_command(provider: str, command: str, *, direct: bool = False) -> str:
    """Render an internal slash command in the provider's supported input UI."""
    if provider == "slack" or (provider == "mattermost" and direct):
        return command.lstrip("/")
    if provider == "mattermost":
        return "/preloop " + command
    return "/preloop message:" + command
