"""Protects Teams transport contracts; fails on insecure or changed wire data.

no_claim for every test: a local RSA key and fake HTTP stand in for Microsoft;
passing tests do not prove tenant claims, package acceptance or message delivery.
"""

from __future__ import annotations

import hashlib
import io
import json
import struct
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from zipfile import ZipFile

import httpx
import jwt
import jsonschema
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from preloop.services import teams_auth as auth
from preloop.services.chat_providers import (
    ChatInbound,
    ChatProviderConfig,
    ChatProviderError,
    ChatProviderRetryError,
    TeamsInstallRequiredError,
    TEAMS_INSTALL_ERROR,
    MAX_EVENT_BYTES,
    verify_ingress,
    send_private_reply,
    external_command,
    conversation_scope,
)
from preloop.services.chat_setup import discover
from preloop.services.teams_package import (
    teams_package,
    teams_manifest,
    MANIFEST_SCHEMA,
)

APP = "11111111-1111-4111-8111-111111111111"
TENANT = "22222222-2222-4222-8222-222222222222"
USER = "33333333-3333-4333-8333-333333333333"
PACKAGE = "44444444-4444-4444-8444-444444444444"
SERVICE = "https://smba.trafficmanager.net/emea/"
CONFIG = ChatProviderConfig(
    "teams", TENANT, "", "synthetic-secret", APP, teams_app_tenant_id=TENANT
)
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY = {
    **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key())),
    "kid": "fixture-key",
    "endorsements": ["msteams"],
}


# Activity shape: https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability
# All identities and text are synthetic. Only personal-scope fields are retained.
def activity() -> dict[str, Any]:
    """Build a fresh documented personal message fixture."""
    return {
        "type": "message",
        "id": "activity-1",
        "channelId": "msteams",
        "serviceUrl": SERVICE,
        "from": {"id": "29:synthetic-user", "aadObjectId": USER},
        "recipient": {"id": "28:" + APP},
        "conversation": {
            "id": "a:synthetic-conversation",
            "conversationType": "personal",
            "tenantId": TENANT,
        },
        "channelData": {"tenant": {"id": TENANT}},
        "text": "help",
    }


# Claims: https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-authentication
# Single-tenant claim compatibility remains unverified until a real installation.
def signed_token(**overrides: Any) -> str:
    """Sign synthetic documented Connector claims with the local test key."""
    claims = {
        "iss": auth.ISSUER,
        "aud": APP,
        "exp": time.time() + 600,
        "nbf": time.time() - 60,
        "serviceUrl": SERVICE,
        **overrides,
    }
    return jwt.encode(
        claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": "fixture-key"}
    )


@pytest.fixture(autouse=True)
def reset_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate cache tests and operator host configuration."""
    monkeypatch.setattr(auth, "_keys", [])
    monkeypatch.setattr(auth, "_keys_until", 0.0)
    monkeypatch.setattr(auth, "_refetch_after", 0.0)
    monkeypatch.setattr(auth, "_tokens", {})
    monkeypatch.delenv("CHAT_TEAMS_SERVICE_HOSTS", raising=False)


def test_valid_and_exact_inbound() -> None:
    """Protects exact ingress; fails on routing drift; no_claim: real claims."""
    token = signed_token()
    auth.verify_activity_token(token, [KEY], APP, SERVICE)
    ingress = verify_ingress(
        CONFIG,
        json.dumps(activity()).encode(),
        {"Authorization": "Bearer " + token},
        signing_keys=[KEY],
    )
    assert ingress.acknowledgement == {}
    assert ingress.event == ChatInbound(
        event_id=hashlib.sha256(b"a:synthetic-conversation:activity-1").hexdigest(),
        user_id=USER,
        channel_id=hashlib.sha256(b"a:synthetic-conversation").hexdigest(),
        text="help",
        is_private=True,
        service_url=SERVICE,
        provider_conversation_id="a:synthetic-conversation",
    )
    assert external_command("teams", "/ask example question") == "ask example question"


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://example.com"},
        {"aud": "wrong-app"},
        {"exp": time.time() - 601},
        {"nbf": "future"},
        {"serviceUrl": "https://example.com/"},
        {"serviceUrl": None},
        {"exp": "999999999999"},
        {"nbf": float("nan")},
    ],
)
def test_reject_claims(claims: dict[str, Any]) -> None:
    """Protects claims; fails on acceptance; no_claim: real tenant claims."""
    if claims.get("nbf") == "future":
        claims = {**claims, "nbf": time.time() + 601}
    with pytest.raises(ChatProviderError, match="^Invalid Teams token$"):
        auth.verify_activity_token(signed_token(**claims), [KEY], APP, SERVICE)


@pytest.mark.parametrize("field", ["exp", "nbf", "iss", "aud", "serviceUrl"])
def test_required_claims(field: str) -> None:
    """Protects required claims; fails if omitted; no_claim: real issuer."""
    claims = jwt.decode(signed_token(), options={"verify_signature": False})
    claims.pop(field)
    token = jwt.encode(
        claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": "fixture-key"}
    )
    with pytest.raises(ChatProviderError, match="^Invalid Teams token$"):
        auth.verify_activity_token(token, [KEY], APP, SERVICE)


@pytest.mark.parametrize("algorithm", ["none", "HS256"])
def test_algorithm_pinned(algorithm: str) -> None:
    """Protects RS256; fails on algorithm confusion; no_claim: Microsoft keys."""
    token = jwt.encode(
        {"aud": APP},
        "synthetic" * 8 if algorithm == "HS256" else None,
        algorithm=algorithm,
        headers={"kid": "fixture-key"},
    )
    with pytest.raises(ChatProviderError, match="^Invalid Teams token$"):
        auth.verify_activity_token(token, [KEY], APP, SERVICE)


@pytest.mark.parametrize(
    "changes",
    [
        {"kid": "unknown"},
        {"endorsements": []},
        {"endorsements": "msteams"},
        {"kty": "oct"},
    ],
)
def test_key_rejections(changes: dict[str, Any]) -> None:
    """Protects key and channel binding; fails open; no_claim: upstream keys."""
    with pytest.raises(ChatProviderError, match="^Invalid Teams token$"):
        auth.verify_activity_token(signed_token(), [{**KEY, **changes}], APP, SERVICE)


def test_wrong_signature() -> None:
    """Protects signature checking; fails on forged token; no_claim: live keys."""
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = jwt.decode(signed_token(), options={"verify_signature": False})
    token = jwt.encode(claims, other, algorithm="RS256", headers={"kid": "fixture-key"})
    with pytest.raises(ChatProviderError, match="^Invalid Teams token$"):
        auth.verify_activity_token(token, [KEY], APP, SERVICE)


@pytest.mark.parametrize(
    "url",
    [
        "http://smba.trafficmanager.net/",
        "https://example.com/",
        "https://smba.trafficmanager.net.example.com/",
        "https://x@smba.trafficmanager.net/",
        "https://smba.trafficmanager.net:444/",
        "https://smba.trafficmanager.net/?next=x",
        "https://smba.trafficmanager.net/#x",
        "https://smba.trafficmanager.net\\@example.com/",
        "https://smba.trafficmanager.net/\n",
    ],
)
def test_service_hosts(url: str) -> None:
    """Protects bearer destination; fails on spoof; no_claim: observed hosts."""
    assert not auth.service_url_allowed(url)


@pytest.mark.parametrize(
    "path,value",
    [
        (("conversation", "tenantId"), "other"),
        (("channelData", "tenant", "id"), "other"),
        (("conversation", "tenantId"), None),
        (("channelData", "tenant", "id"), None),
        (("recipient", "id"), "28:other"),
        (("channelId",), "other"),
        (("serviceUrl",), "https://example.com/"),
    ],
)
def test_activity_rejections(path: tuple[str, ...], value: Any) -> None:
    """Protects tenant and destination; fails open; no_claim: live activity."""
    body = activity()
    target = body
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(ChatProviderError):
        verify_ingress(
            CONFIG,
            json.dumps(body).encode(),
            {"Authorization": "Bearer " + signed_token(serviceUrl=body["serviceUrl"])},
            signing_keys=[KEY],
        )


@pytest.mark.parametrize(
    "path,value",
    [
        (("type",), "conversationUpdate"),
        (("conversation", "conversationType"), "channel"),
        (("conversation", "conversationType"), "groupChat"),
        (("from", "id"), "28:" + APP),
        (("from", "role"), "bot"),
    ],
)
def test_no_work_for_nonpersonal_or_bot(path: tuple[str, ...], value: str) -> None:
    """Protects link privacy and loops; fails on queueing; no_claim: delivery."""
    body = activity()
    body["text"] = "link synthetic-code"
    target = body
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    assert (
        verify_ingress(
            CONFIG,
            json.dumps(body).encode(),
            {"Authorization": "Bearer " + signed_token()},
            signing_keys=[KEY],
        ).event
        is None
    )


def test_mentions_and_size() -> None:
    """Protects message bounds and bot mentions; fails on drift; no_claim: UI."""
    body = activity()
    body.update(
        text="<at>Preloop</at> help",
        entities=[
            {
                "type": "mention",
                "mentioned": {"id": "28:" + APP},
                "text": "<at>Preloop</at>",
            }
        ],
    )
    headers = {"Authorization": "Bearer " + signed_token()}
    assert (
        verify_ingress(
            CONFIG, json.dumps(body).encode(), headers, signing_keys=[KEY]
        ).event.text
        == "help"
    )
    with pytest.raises(ChatProviderError):
        verify_ingress(
            CONFIG, b"x" * (MAX_EVENT_BYTES + 1), headers, signing_keys=[KEY]
        )


@pytest.mark.asyncio
async def test_fixed_key_source_and_bounded_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Protects fixed trust and refetch bound; fails on fetch storm; no_claim: rotation."""
    calls = []

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            json={"jwks_uri": auth.KEYS_URL}
            if str(request.url) == auth.OPENID_URL
            else {"keys": [KEY]},
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        auth.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    claims = jwt.decode(signed_token(), options={"verify_signature": False})
    token = jwt.encode(
        claims,
        PRIVATE_KEY,
        algorithm="RS256",
        headers={
            "kid": "fixture-key",
            "jku": "https://example.com/keys",
            "x5u": "https://example.com/cert",
        },
    )
    assert await auth.signing_keys(token) == [KEY]
    assert await auth.signing_keys(token) == [KEY]
    assert calls == [auth.OPENID_URL, auth.KEYS_URL]
    unknown = jwt.encode(
        claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": "unknown"}
    )
    monkeypatch.setattr(auth, "_refetch_after", 0)
    for _ in range(4):
        keys = await auth.signing_keys(unknown)
        with pytest.raises(ChatProviderError):
            auth.verify_activity_token(unknown, keys, APP, SERVICE)
    assert calls == [auth.OPENID_URL, auth.KEYS_URL] * 2
    monkeypatch.setattr(auth, "_keys_until", 0)
    with pytest.raises(ChatProviderError, match="unavailable"):
        await auth.signing_keys(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"jwks_uri": "https://example.com/keys"},
        {"jwks_uri": auth.KEYS_URL + "?redirect=example.com"},
    ],
)
async def test_metadata_cannot_redirect_trust(
    monkeypatch: pytest.MonkeyPatch, metadata: dict[str, str]
) -> None:
    """Protects key-origin pinning; fails on second fetch; no_claim: live metadata."""
    calls = []

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=metadata)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        auth.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    with pytest.raises(ChatProviderError):
        await auth.signing_keys(signed_token())
    assert calls == [auth.OPENID_URL]


# REST examples: https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages
@pytest.mark.asyncio
async def test_send_and_token_payloads() -> None:
    """Protects exact wire data and cache; fails on drift; no_claim: install or rendering."""
    requests = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(
                200, json={"access_token": "synthetic-access", "expires_in": 3600}
            )
        return httpx.Response(
            200,
            json={
                "id": "a:conversation/one"
                if request.url.path.endswith("/conversations")
                else "sent-1"
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), trust_env=False
    ) as client:
        assert (
            await send_private_reply(
                CONFIG,
                USER,
                "A <b>plain</b> @message",
                delivery_id="job",
                client=client,
                service_url=SERVICE,
            )
            == "sent-1"
        )
        assert await auth.bot_token(CONFIG, client) == "synthetic-access"
    assert len(requests) == 3
    assert (
        str(requests[0].url)
        == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    )
    assert parse_qs(requests[0].content.decode()) == {
        "grant_type": ["client_credentials"],
        "client_id": [APP],
        "client_secret": ["synthetic-secret"],
        "scope": ["https://api.botframework.com/.default"],
    }
    assert json.loads(requests[1].content) == {
        "bot": {"id": "28:" + APP},
        "members": [{"id": USER}],
        "isGroup": False,
        "channelData": {"tenant": {"id": TENANT}},
    }
    assert requests[2].url.raw_path.endswith(
        b"/v3/conversations/a%3Aconversation%2Fone/activities"
    )
    assert json.loads(requests[2].content) == {
        "type": "message",
        "textFormat": "plain",
        "text": "A <b>plain</b> @message",
    }
    assert requests[2].headers["authorization"] == "Bearer synthetic-access"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,error",
    [
        (429, ChatProviderRetryError),
        (412, ChatProviderRetryError),
        (403, TeamsInstallRequiredError),
        (502, httpx.HTTPError),
        (503, httpx.HTTPError),
        (504, httpx.HTTPError),
    ],
)
async def test_delivery_outcomes(status: int, error: type[Exception]) -> None:
    """Protects definite vs uncertain outcomes; fails on replay; no_claim: live status."""

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(
                200, json={"access_token": "synthetic-access", "expires_in": 3600}
            )
        return httpx.Response(
            status, text="sensitive provider error", headers={"Retry-After": "42"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(error) as caught:
            await send_private_reply(
                CONFIG,
                USER,
                "Hello",
                delivery_id="job",
                client=client,
                service_url=SERVICE,
                conversation_id="existing",
            )
    assert "sensitive" not in str(caught.value)
    if status in {412, 429}:
        assert caught.value.retry_after == 42
    if status == 403:
        assert str(caught.value) == TEAMS_INSTALL_ERROR


@pytest.mark.asyncio
async def test_send_rechecks_stored_url_and_secret_rotation() -> None:
    """Protects stored URL and rotated secrets; fails on leak/stale cache; no_claim: rotation."""
    calls = []

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ChatProviderError, match="service URL"):
            await send_private_reply(
                CONFIG,
                USER,
                "Hello",
                delivery_id="job",
                client=client,
                service_url="https://example.com/",
            )
        assert calls == []
        await auth.bot_token(CONFIG, client)
        await auth.bot_token(replace(CONFIG, bot_token="rotated-secret"), client)
        assert len(calls) == 2
        with pytest.raises(ChatProviderError):
            await auth.bot_token(
                replace(CONFIG, teams_app_tenant_id="../common"), client
            )
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_discovery_failure_is_fixed() -> None:
    """Protects secret-safe errors; fails on echo; no_claim: real credentials."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, text="synthetic-secret")
        )
    ) as client:
        with pytest.raises(
            ChatProviderError,
            match="^Teams authentication failed; check application ID, tenant ID and client secret$",
        ):
            await discover(CONFIG, client=client)


def test_package_schema_and_icons() -> None:
    """Protects published schema and package; fails on invalid manifest; no_claim: admin acceptance."""
    schema = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/teams/MicrosoftTeams.schema.json"
        ).read_text()
    )
    with ZipFile(
        io.BytesIO(teams_package(APP, PACKAGE, "https://preloop.example.com/path"))
    ) as package:
        assert set(package.namelist()) == {"manifest.json", "color.png", "outline.png"}
        manifest = json.loads(package.read("manifest.json"))
        # Microsoft's schema contains ECMA Unicode regexes in unused extensions.
        # Instantiate its declared validator directly: Python's schema meta-check
        # rejects those regexes, even though this bot manifest never uses them.
        jsonschema.validators.validator_for(schema)(schema).validate(manifest)
        assert manifest["$schema"] == MANIFEST_SCHEMA
        assert manifest["id"] == PACKAGE
        assert manifest["bots"] == [
            {
                "botId": APP,
                "scopes": ["personal"],
                "isNotificationOnly": False,
                "supportsFiles": False,
            }
        ]
        assert manifest["validDomains"] == ["preloop.example.com"]
        assert set(manifest["developer"].values()) == {
            "Preloop",
            "https://preloop.example.com",
        }
        assert "synthetic-secret" not in package.read("manifest.json").decode()
        for name, size in (("color.png", 192), ("outline.png", 32)):
            assert struct.unpack(">II", package.read(name)[16:24]) == (size, size)
    custom = teams_manifest(
        APP,
        PACKAGE,
        "https://preloop.example.com",
        developer_url="https://example.com/developer",
        privacy_url="https://example.com/privacy",
        terms_url="https://example.com/terms",
    )
    jsonschema.validators.validator_for(schema)(schema).validate(custom)
    assert custom["developer"]["privacyUrl"] == "https://example.com/privacy"
    with pytest.raises(ChatProviderError):
        teams_manifest(APP, PACKAGE, "http://localhost")


def test_personal_scope_is_provider_neutral() -> None:
    """Protects broker interface parity; fails on Teams-only scope; no_claim: follow-ups."""
    assert (
        conversation_scope("teams", USER, {"private": True, "channel_id": "digest"})
        == f"dm:{USER}"
    )
    assert (
        conversation_scope(
            "slack", USER, {"private": False, "channel_id": "c", "thread_id": "t"}
        )
        == "thread:c:t"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [b"x" * (auth.MAX_RESPONSE_BYTES + 1), b"[]", b"invalid"]
)
async def test_response_bounds(body: bytes) -> None:
    """Protects bounded parsing; fails on unbounded/invalid data; no_claim: live response."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    ) as client:
        with pytest.raises(ChatProviderError, match="^Invalid Teams response$"):
            await auth.request_json(client, "GET", auth.OPENID_URL)


@pytest.mark.asyncio
async def test_discovery_exact_tenant() -> None:
    """Protects discovery shape; fails on accidental Graph lookup; no_claim: installation."""
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200, json={"access_token": "synthetic-access", "expires_in": 3600}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        assert await discover(CONFIG, client=client) == {
            "bot_user_id": APP,
            "name": "Microsoft Teams",
            "workspaces": [{"id": TENANT, "name": TENANT}],
        }
    assert calls == [f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"]
