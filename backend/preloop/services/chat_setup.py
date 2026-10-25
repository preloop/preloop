"""Read-only discovery and explicit installation using server-owned credentials."""

from __future__ import annotations

import asyncio
import json
import re
import ssl
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx

from preloop.services.chat_providers import (
    ChatProviderConfig,
    ChatProviderError,
    MAX_EVENT_BYTES,
    validate_mattermost_url,
)


class ChatProviderRejectedError(ChatProviderError):
    """A definitive provider rejection; no delivery was accepted."""

    def __init__(self, status: int) -> None:
        super().__init__("Provider rejected the operation")
        self.status = status


async def provider_request(
    config: ChatProviderConfig,
    method: str,
    path: str,
    *,
    payload: Any = None,
    client: httpx.AsyncClient | None = None,
) -> Any:
    """Bound total time and decoded response size; never follow token redirects.

    Mattermost origins are management-only instance/team configuration. Private
    HTTPS origins remain valid for self-hosted intranets. Visitors cannot set one.
    """
    base = {
        "slack": "https://slack.com/api",
        "discord": "https://discord.com/api/v10",
    }.get(config.provider)
    if config.provider == "mattermost":
        base = validate_mattermost_url(config.base_url) + "/api/v4"
    if not base or not path.startswith("/") or ".." in path:
        raise ChatProviderError("Invalid provider request")
    extra_headers: dict[str, str] = {}
    extensions: dict[str, Any] = {}
    if config.provider == "mattermost":
        from preloop.services.chat_network import pinned_mattermost_origin

        numeric, authority, hostname = await pinned_mattermost_origin(config.base_url)
        base = numeric + "/api/v4"
        extra_headers["Host"] = authority
        extensions["sni_hostname"] = hostname
    auth = "Bot" if config.provider == "discord" else "Bearer"

    async def perform(active: httpx.AsyncClient) -> Any:
        async with (
            asyncio.timeout(12),
            active.stream(
                method,
                base + path,
                headers={
                    "Authorization": f"{auth} {config.bot_token}",
                    **extra_headers,
                },
                extensions=extensions,
                json=payload,
                timeout=10,
                follow_redirects=False,
            ) as response,
        ):
            if 400 <= response.status_code < 500 and response.status_code not in {
                408,
                409,
            }:
                raise ChatProviderRejectedError(response.status_code)
            if not 200 <= response.status_code < 300:
                raise ChatProviderError(
                    f"Provider refused request ({response.status_code}). Check the bot token and permissions."
                )
            raw = bytearray()
            async for chunk in response.aiter_bytes(8192):
                raw.extend(chunk)
                if len(raw) > MAX_EVENT_BYTES:
                    raise ChatProviderError("Provider response is too large")
            data = json.loads(bytes(raw))
            if not isinstance(data, (dict, list)):
                raise ChatProviderError("Invalid provider response")
            if isinstance(data, dict) and data.get("ok") is False:
                raise ChatProviderError(
                    "Provider refused request. Check the bot token and permissions."
                )
            return data

    try:
        if client:
            return await perform(client)
        async with httpx.AsyncClient(
            trust_env=False, verify=ssl.create_default_context()
        ) as active:
            return await perform(active)
    except (httpx.HTTPError, ValueError, TimeoutError, RecursionError) as exc:
        if isinstance(exc, ChatProviderError):
            raise
        raise ChatProviderError(
            "Provider unavailable. Check the server address and bot token."
        ) from exc


def identifier(value: Any) -> str:
    """Allow provider IDs only as single URL path segments."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
        raise ChatProviderError("Provider returned an invalid identifier")
    return value


def workspaces(data: Any) -> list[dict[str, str]]:
    if not isinstance(data, list) or len(data) > 200:
        raise ChatProviderError(
            "Invalid workspace list; use an account with fewer than 200 workspaces"
        )
    return [
        {
            "id": identifier(row.get("id")),
            "name": str(row.get("display_name") or row.get("name") or "Workspace")[
                :120
            ],
        }
        for row in data
        if isinstance(row, dict)
    ]


def mattermost_team_name(value: str, origin: str) -> str:
    """Resolve a normal team URL/name; never treat invite/channel IDs as a team."""
    if "://" in value:
        try:
            parsed, server = urlparse(value), urlparse(validate_mattermost_url(origin))
        except ValueError as exc:
            raise ChatProviderError("Enter a valid Mattermost team URL") from exc
        if (
            parsed.scheme != server.scheme
            or parsed.netloc != server.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ChatProviderError("Use a team URL on this Mattermost server")
        value = parsed.path.strip("/")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", value) or value in {
        "signup_user_complete",
        "login",
        "channels",
        "invite",
    }:
        raise ChatProviderError(
            "Enter the team name or its normal team URL, not an invite or channel URL"
        )
    return value


async def discover(
    config: ChatProviderConfig,
    *,
    team_hint: str = "",
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Discover identity and installed workspaces without exposing credentials."""

    async def get(path: str) -> Any:
        return await provider_request(config, "GET", path, client=client)

    if config.provider == "slack":
        identity = await get("/auth.test")
        if not isinstance(identity, dict) or identity.get("ok") is not True:
            raise ChatProviderError("Slack refused bot identity discovery")
        return {
            "bot_user_id": identifier(identity.get("user_id")),
            "name": str(identity.get("team") or "Slack")[:120],
            "workspaces": [
                {
                    "id": identifier(identity.get("team_id")),
                    "name": str(identity.get("team") or "Slack")[:120],
                }
            ],
        }
    if config.provider == "mattermost":
        identity = await get("/users/me")
        if not isinstance(identity, dict):
            raise ChatProviderError("Invalid Mattermost identity response")
        bot_id = identifier(identity.get("id"))
        if identity.get("is_bot") is not True:
            raise ChatProviderError(
                "Use a Mattermost bot access token, not a human or system-admin token"
            )
        teams = workspaces(await get(f"/users/{bot_id}/teams"))
        if team_hint:
            team = await get(
                "/teams/name/"
                + quote(mattermost_team_name(team_hint, config.base_url), safe="")
            )
            team_id = identifier(team.get("id"))
            if team_id not in {row["id"] for row in teams}:
                raise ChatProviderError("Add the bot to this team before continuing")
            teams = [row for row in teams if row["id"] == team_id]
        return {
            "bot_user_id": bot_id,
            "bot_username": str(identity.get("username") or "")[:120],
            "name": str(
                identity.get("first_name") or identity.get("username") or "Mattermost"
            )[:120],
            "workspaces": teams,
        }
    if config.provider == "discord":
        application = await get("/applications/@me")
        bot = await get("/users/@me")
        if not isinstance(application, dict) or not isinstance(bot, dict):
            raise ChatProviderError("Invalid Discord application response")
        key = str(application.get("verify_key") or "")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", key):
            raise ChatProviderError(
                "Discord did not return a verification key for this application"
            )
        app_id = identifier(application.get("id"))
        return {
            "bot_user_id": identifier(bot.get("id")),
            "application_id": app_id,
            "verification_secret": key,
            "name": str(application.get("name") or "Discord")[:120],
            "interactions_endpoint_url": str(
                application.get("interactions_endpoint_url") or ""
            ),
            "workspaces": workspaces(await get("/users/@me/guilds?limit=200")),
            "install_url": "https://discord.com/oauth2/authorize?"
            + urlencode(
                {
                    "client_id": app_id,
                    "scope": "bot applications.commands",
                    "permissions": "2048",
                }
            ),
        }
    raise ChatProviderError("Unsupported provider")


def slack_manifest(callback_url: str) -> dict[str, Any]:
    """Portable DM + app mention integration; no slash command or AI-plan gate."""
    return {
        "display_information": {"name": "Preloop"},
        "features": {
            "bot_user": {"display_name": "Preloop", "always_online": False},
            "app_home": {
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
        },
        "oauth_config": {
            "scopes": {
                "bot": ["chat:write", "im:write", "im:history", "app_mentions:read"]
            }
        },
        "settings": {
            "event_subscriptions": {
                "request_url": callback_url,
                "bot_events": ["message.im", "app_mention"],
            },
            "socket_mode_enabled": False,
        },
    }


async def install_discord(
    config: ChatProviderConfig,
    application_id: str,
    callback_url: str,
    *,
    replace_callback: bool = False,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Upsert only our command, preserving unrelated application commands."""
    app = await provider_request(config, "GET", "/applications/@me", client=client)
    existing = app.get("interactions_endpoint_url")
    if existing and existing != callback_url and not replace_callback:
        raise ChatProviderError(
            "This Discord application already has an interactions callback. Confirm replacement before finishing."
        )
    # The application modify API supports interactions_endpoint_url with bot auth.
    await provider_request(
        config,
        "PATCH",
        "/applications/@me",
        payload={"interactions_endpoint_url": callback_url},
        client=client,
    )
    try:
        path = f"/applications/{identifier(application_id)}/guilds/{identifier(config.workspace_id)}/commands"
        commands = await provider_request(config, "GET", path, client=client)
        owned = next(
            (
                row
                for row in commands
                if row.get("name") == "preloop" and row.get("type", 1) == 1
            ),
            None,
        )
        payload = {
            "name": "preloop",
            "description": "Ask Preloop or send an explicit command",
            "type": 1,
            "options": [
                {
                    "name": "message",
                    "description": "Question or command",
                    "type": 3,
                    "required": True,
                    "max_length": 2000,
                }
            ],
        }
        if owned:
            payload.pop("type", None)
        await provider_request(
            config,
            "PATCH" if owned else "POST",
            path + ("/" + identifier(owned["id"]) if owned else ""),
            payload=payload,
            client=client,
        )
    except ChatProviderError:
        # The durable connection remains available if rollback itself is
        # unavailable. Never overwrite a callback concurrently changed elsewhere.
        try:
            current = await provider_request(
                config, "GET", "/applications/@me", client=client
            )
            if current.get("interactions_endpoint_url") == callback_url:
                await provider_request(
                    config,
                    "PATCH",
                    "/applications/@me",
                    payload={"interactions_endpoint_url": existing or ""},
                    client=client,
                )
        except ChatProviderError:
            pass
        raise
