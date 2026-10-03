"""Provider signatures and private-audience delivery must fail closed."""

import hashlib
import hmac
import json
from urllib.parse import urlencode

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from preloop.services.chat_providers import (
    ChatProviderConfig,
    ChatProviderError,
    send_private_reply,
    verify_ingress,
)

NOW = 1_790_900_000


def slack_request(**event_fields: object) -> tuple[bytes, dict[str, str]]:
    body = {
        "team_id": "team-1",
        "event_id": "event-1",
        "event": {
            "type": "app_mention",
            "user": "user-1",
            "channel": "public-channel",
            "text": "What is our spend?",
            **event_fields,
        },
    }
    raw = json.dumps(body).encode()
    signature = hmac.new(
        b"signing-secret", f"v0:{NOW}:".encode() + raw, hashlib.sha256
    ).hexdigest()
    return raw, {
        "X-Slack-Request-Timestamp": str(NOW),
        "X-Slack-Signature": "v0=" + signature,
    }


def test_slack_signature_is_bound_to_raw_body_and_workspace() -> None:
    config = ChatProviderConfig("slack", "team-1", "signing-secret", "bot-token")
    raw, headers = slack_request()
    event = verify_ingress(config, raw, headers, now=NOW).event
    assert event is not None and event.user_id == "user-1"
    assert not event.is_private
    with pytest.raises(ChatProviderError, match="signature"):
        verify_ingress(config, raw.replace(b"user-1", b"user-2"), headers, now=NOW)
    wrong_team = ChatProviderConfig("slack", "team-2", "signing-secret", "bot-token")
    with pytest.raises(ChatProviderError, match="Workspace"):
        verify_ingress(wrong_team, raw, headers, now=NOW)


def test_slack_rejects_stale_requests_and_ignores_bot_loops() -> None:
    config = ChatProviderConfig("slack", "team-1", "signing-secret", "bot-token")
    raw, headers = slack_request()
    with pytest.raises(ChatProviderError, match="Expired"):
        verify_ingress(config, raw, headers, now=NOW + 301)
    raw, headers = slack_request(bot_id="bot-1")
    assert verify_ingress(config, raw, headers, now=NOW).event is None
    raw, headers = slack_request(type="message", channel_type="channel")
    assert verify_ingress(config, raw, headers, now=NOW).event is None


def test_discord_authenticates_identity_and_command() -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    config = ChatProviderConfig("discord", "guild-1", public, "bot-token")
    raw = json.dumps(
        {
            "type": 2,
            "id": "event-1",
            "guild_id": "guild-1",
            "channel_id": "channel-1",
            "member": {"user": {"id": "user-1"}},
            "data": {
                "name": "preloop",
                "options": [{"name": "message", "value": "list agents"}],
            },
        }
    ).encode()
    headers = {
        "x-signature-timestamp": str(NOW),
        "x-signature-ed25519": private.sign(str(NOW).encode() + raw).hex(),
    }
    result = verify_ingress(config, raw, headers, now=NOW)
    assert result.event is not None and result.event.user_id == "user-1"
    assert result.acknowledgement["data"]["flags"] == 64
    with pytest.raises(ChatProviderError, match="signature"):
        verify_ingress(config, raw.replace(b"user-1", b"user-2"), headers, now=NOW)


def test_mattermost_requires_token_team_and_immutable_event_id() -> None:
    config = ChatProviderConfig("mattermost", "team-1", "command-secret", "bot-token")
    body = {
        "token": "command-secret",
        "team_id": "team-1",
        "user_id": "user-1",
        "channel_id": "channel-1",
        "trigger_id": "trigger-1",
        "text": "list agents",
    }
    result = verify_ingress(config, urlencode(body).encode(), {})
    assert result.event is not None and result.event.event_id == "trigger-1"
    with pytest.raises(ChatProviderError, match="Ambiguous"):
        verify_ingress(config, urlencode(body).encode() + b"&user_id=user-2", {})
    with pytest.raises(ChatProviderError, match="token"):
        verify_ingress(config, urlencode({**body, "token": "forged"}).encode(), {})
    del body["trigger_id"]
    with pytest.raises(ChatProviderError, match="event ID"):
        verify_ingress(config, urlencode(body).encode(), {})


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["slack", "mattermost", "discord"])
async def test_reply_opens_dm_instead_of_using_public_input_channel(
    provider: str,
) -> None:
    requests: list[tuple[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append((str(request.url), payload))
        if len(requests) == 1:
            return httpx.Response(
                200, json={"channel": {"id": "private-dm"}, "id": "private-dm"}
            )
        return httpx.Response(200, json={"id": "message-1", "ts": "123.45"})

    config = ChatProviderConfig(
        provider,
        "workspace-1",
        "secret",
        "bot-token",
        "bot-user",
        "https://chat.example.com",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await send_private_reply(
            config,
            "recipient-1",
            "Private inventory",
            delivery_id="delivery-1",
            client=client,
        )
    assert "recipient-1" in json.dumps(requests[0])
    assert "private-dm" in json.dumps(requests[1])
    if provider == "discord":
        assert requests[1][1]["allowed_mentions"] == {"parse": []}
    if provider == "slack":
        assert requests[1][1]["mrkdwn"] is False


@pytest.mark.asyncio
async def test_delivery_errors_never_echo_provider_secrets() -> None:
    config = ChatProviderConfig("slack", "team-1", "secret", "bot-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "private provider error detail"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ChatProviderError) as error:
            await send_private_reply(
                config, "u1", "reply", delivery_id="d1", client=client
            )
    assert str(error.value) == "Chat delivery failed (403)"


@pytest.mark.parametrize(
    "field,value",
    [("data", []), ("data", None), ("member", []), ("member", {"user": []})],
)
def test_signed_discord_malformed_nested_objects_fail_closed(
    field: str, value: object
) -> None:
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    config = ChatProviderConfig("discord", "server-test", public, "token")
    body = {
        "type": 2,
        "guild_id": "server-test",
        "id": "event-test",
        "channel_id": "channel-test",
        "data": {
            "name": "preloop",
            "options": [{"name": "message", "value": "Synthetic message"}],
        },
        "member": {"user": {"id": "external-test"}},
    }
    body[field] = value
    raw = json.dumps(body).encode()
    headers = {
        "x-signature-timestamp": str(NOW),
        "x-signature-ed25519": key.sign(str(NOW).encode() + raw).hex(),
    }
    with pytest.raises(ChatProviderError):
        verify_ingress(config, raw, headers, now=NOW)


@pytest.mark.parametrize(
    "provider,expected",
    [
        ("slack", "approve example-id"),
        ("mattermost", "/preloop /approve example-id"),
        ("discord", "/preloop message:/approve example-id"),
    ],
)
def test_provider_command_instructions_match_registered_ingress(
    provider: str, expected: str
) -> None:
    from preloop.services.chat_providers import external_command

    assert external_command(provider, "/approve example-id") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,error", [(400, ChatProviderError), (503, httpx.HTTPStatusError)]
)
async def test_provider_rejection_and_ambiguous_server_failure_are_distinct(
    status: int, error: type[Exception]
) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200, json={"ok": True, "channel": {"id": "private-dm"}}
            )
        return httpx.Response(status, json={"ok": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(error):
            await send_private_reply(
                ChatProviderConfig("slack", "team", "secret", "token"),
                "external-user",
                "Synthetic reply",
                delivery_id="synthetic-delivery",
                client=client,
            )
    assert calls == 2
