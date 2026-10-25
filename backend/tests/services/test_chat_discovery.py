"""Synthetic provider contracts, network bounds and selected-workspace isolation."""

from __future__ import annotations
import json
import time

import httpx
import pytest
from preloop.services.chat_providers import ChatProviderConfig, ChatProviderError
from preloop.services.chat_setup import (
    discover,
    install_discord,
    mattermost_team_name,
    provider_request,
    slack_manifest,
)
from preloop.services.mattermost_listener import canonical_dm


def config(provider: str = "mattermost") -> ChatProviderConfig:
    return ChatProviderConfig(
        provider, "team", "", "secret-token", "bot", "https://chat.example.com"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 2])
async def test_mattermost_named_teams(count: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-token"
        if request.url.path.endswith("/users/me"):
            return httpx.Response(
                200,
                json={
                    "id": "bot",
                    "is_bot": True,
                    "username": "team-assistant",
                    "first_name": "Team assistant",
                },
            )
        return httpx.Response(
            200,
            json=[
                {"id": f"team-{n}", "display_name": f"Team {n}"} for n in range(count)
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await discover(config(), client=client)
    assert result["bot_user_id"] == "bot"
    assert result["bot_username"] == "team-assistant"
    assert result["name"] == "Team assistant"
    assert len(result["workspaces"]) == count
    assert "secret-token" not in json.dumps(result)


@pytest.mark.parametrize(
    "value",
    [
        "https://other.example.com/team",
        "https://chat.example.com/team/channels/town-square",
        "https://chat.example.com/signup_user_complete/?id=invite",
        "invite/abc",
        "https://chat.example.com/team?x=1",
        "https://chat.example.com:wrong/team",
    ],
)
def test_rejects_non_team_links(value: str) -> None:
    with pytest.raises(ChatProviderError):
        mattermost_team_name(value, config().base_url)


def test_normal_team_url() -> None:
    assert (
        mattermost_team_name("https://chat.example.com/engineering/", config().base_url)
        == "engineering"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body",
    [
        (401, b"secret"),
        (302, b"secret"),
        (200, b"bad-json-secret"),
        (200, b"x" * 65537),
    ],
)
async def test_bounded_requests_never_leak_provider_body(
    status: int, body: bytes
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, content=body, headers={"Location": "https://other.example.com"}
            )
        )
    ) as client:
        with pytest.raises(ChatProviderError) as error:
            await provider_request(config(), "GET", "/users/me", client=client)
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_slack_discovers_workspace_and_bot_without_scopes_for_display_lookup() -> (
    None
):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"ok": True, "team_id": "T123", "user_id": "U123", "team": "Team"},
            )
        )
    ) as client:
        result = await discover(config("slack"), client=client)
    assert result["workspaces"] == [{"id": "T123", "name": "Team"}]
    assert result["bot_user_id"] == "U123"
    manifest = slack_manifest("https://preloop.example.com/api/v1/chat/ingress/draft")
    assert "commands" not in manifest["features"]
    assert manifest["settings"]["event_subscriptions"]["bot_events"] == [
        "message.im",
        "app_mention",
    ]
    assert "agent_view" not in json.dumps(manifest)


@pytest.mark.asyncio
async def test_discord_uses_installed_guilds_not_application_support_server() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bot secret-token"
        if request.url.path.endswith("/applications/@me"):
            return httpx.Response(
                200,
                json={
                    "id": "app",
                    "name": "Assistant",
                    "guild_id": "support-server",
                    "verify_key": "ab" * 32,
                },
            )
        if request.url.path.endswith("/users/@me"):
            return httpx.Response(200, json={"id": "bot"})
        return httpx.Response(
            200, json=[{"id": "installed", "name": "Installed server"}]
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await discover(config("discord"), client=client)
    assert result["workspaces"] == [{"id": "installed", "name": "Installed server"}]
    assert "support-server" not in result["install_url"]


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_discord_upserts_only_owned_command(existing: bool) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.endswith("/applications/@me"):
            return httpx.Response(200, json={})
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[{"id": "unrelated", "name": "help"}]
                + ([{"id": "ours", "name": "preloop"}] if existing else []),
            )
        return httpx.Response(200, json={"id": "ours"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await install_discord(
            config("discord"),
            "app",
            "https://preloop.example.com/callback",
            client=client,
        )
    assert not any(method in {"PUT", "DELETE"} for method, _ in calls)
    assert calls[-1] == (
        "PATCH" if existing else "POST",
        "/api/v10/applications/app/guilds/team/commands"
        + ("/ours" if existing else ""),
    )


@pytest.mark.asyncio
async def test_discord_callback_conflict_has_no_effect() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(
            200,
            json={"interactions_endpoint_url": "https://other.example.com/callback"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ChatProviderError, match="Confirm replacement"):
            await install_discord(
                config("discord"),
                "app",
                "https://preloop.example.com/callback",
                client=client,
            )
    assert calls == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["none", "bot", "system", "old", "wrong-team", "wrong-channel", "public-channel"],
)
async def test_canonical_dm_requires_bot_and_actor_selected_team_membership(
    failure: str,
) -> None:
    now = int(time.time() * 1000)
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        paths.append(path)
        if "/posts/" in path:
            return httpx.Response(
                200,
                json={
                    "id": "post",
                    "create_at": now - 10000 if failure == "old" else now,
                    "type": "system_join" if failure == "system" else "",
                    "user_id": "bot" if failure == "bot" else "actor",
                    "channel_id": "dm",
                    "message": "link proof",
                },
            )
        if path.endswith("/users/actor"):
            return httpx.Response(200, json={"id": "actor", "is_bot": False})
        if path.endswith("/channels/dm"):
            return httpx.Response(
                200,
                json={
                    "id": "dm",
                    "type": "O" if failure == "public-channel" else "D",
                    "team_id": "",
                },
            )
        if "/channels/" in path:
            return httpx.Response(
                200,
                json={
                    "user_id": path.rsplit("/", 1)[-1],
                    "channel_id": "wrong" if failure == "wrong-channel" else "dm",
                },
            )
        if failure == "wrong-team" and path.endswith("/actor"):
            return httpx.Response(403)
        return httpx.Response(
            200, json={"user_id": path.rsplit("/", 1)[-1], "team_id": "team"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if failure == "wrong-team":
            with pytest.raises(ChatProviderError):
                await canonical_dm(config(), "post", now - 1000, client=client)
            return
        result = await canonical_dm(config(), "post", now - 1000, client=client)
    if failure == "none":
        assert result["payload"]["text"] == "/link"
        assert "proof" not in json.dumps(result)
        assert "/api/v4/teams/team/members/bot" in paths
        assert "/api/v4/teams/team/members/actor" in paths
    else:
        assert result is None


@pytest.fixture(autouse=True)
def synthetic_provider_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic providers never resolve or contact real hosts."""
    import ipaddress

    monkeypatch.setattr(
        "preloop.services.ai_model_provider._resolve_endpoint_host",
        lambda _: [ipaddress.ip_address("93.184.216.34")],
    )


@pytest.mark.asyncio
async def test_discord_command_failure_restores_replaced_callback() -> None:
    target, previous = (
        "https://preloop.example.com/callback",
        "https://other.example.com/callback",
    )
    current, patches = previous, []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal current
        if request.method == "GET" and request.url.path.endswith("/applications/@me"):
            return httpx.Response(200, json={"interactions_endpoint_url": current})
        if request.method == "PATCH":
            current = json.loads(request.content)["interactions_endpoint_url"]
            patches.append(current)
            return httpx.Response(200, json={})
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ChatProviderError):
            await install_discord(
                config("discord"), "app", target, replace_callback=True, client=client
            )
    assert patches == [target, previous]
    assert current == previous
