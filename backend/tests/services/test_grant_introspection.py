"""Synthetic RFC 7662 responses exercise grant gates and cache isolation."""

import hashlib
import json
from urllib.parse import parse_qs

import httpx
import pytest

from preloop.models.schemas.grant_introspection import IntrospectionConfig
from preloop.services.grant_introspection import GrantIntrospector

TOKEN = "synthetic-upstream-bearer"
SECRET = "synthetic-client-secret"


def config(**overrides) -> IntrospectionConfig:
    return IntrospectionConfig.model_validate(
        {
            "endpoint": "https://as.example.com/introspect",
            "client_id": "firewall",
            "client_secret": SECRET,
            **overrides,
        }
    )


def client_for(handler, now=None, max_entries=4096) -> GrantIntrospector:
    return GrantIntrospector(
        client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), **kwargs
        ),
        clock=(lambda: now[0]) if now is not None else lambda: 1000,
        monotonic_clock=(lambda: now[0]) if now is not None else lambda: 1000,
        max_entries=max_entries,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("client_auth", ["basic", "post"])
async def test_exact_upstream_bearer_and_client_auth(client_auth: str) -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "active": True,
                "sub": "subject-example",
                "scope": "read write",
                "client_id": "app",
                "exp": 1100,
                "consent_id": "consent-example",
            },
        )

    client = client_for(handler)
    result = await client.evaluate(
        TOKEN,
        config(client_auth=client_auth, consent_ref_claim="consent_id"),
        server_id="server-example",
    )
    body = parse_qs(requests[0].content.decode())
    assert body["token"] == [TOKEN]
    assert (
        requests[0]
        .headers["content-type"]
        .startswith("application/x-www-form-urlencoded")
    )
    if client_auth == "basic":
        assert requests[0].headers["authorization"].startswith("Basic ")
        assert "client_secret" not in body
    else:
        assert body["client_secret"] == [SECRET]
        assert body["client_id"] == ["firewall"]
    assert result.deny_reason is None
    assert result.binding["scope"] == ["read", "write"]
    assert result.binding["consent_ref"] == "consent-example"
    assert result.binding["cached"] is False
    assert TOKEN not in repr(client._cache)
    assert SECRET not in repr(client._cache)
    assert next(iter(client._cache))[1] == hashlib.sha256(TOKEN.encode()).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,required,reason",
    [
        ({"active": False}, [], "grant_inactive"),
        ({"active": True, "exp": 1000}, [], "grant_inactive"),
        ({"active": True, "scope": "read"}, ["write"], "scope_not_granted"),
        ({"active": True, "scope": "read"}, ["read"], None),
    ],
)
async def test_active_expiry_and_scope_gates(payload, required, reason) -> None:
    client = client_for(lambda request: httpx.Response(200, json=payload))
    result = await client.evaluate(
        TOKEN, config(required_scopes=required), server_id="server-example"
    )
    assert result.deny_reason == reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "timeout",
        "500",
        "redirect",
        "invalid-json",
        "missing-active",
        "bad-active",
        "bad-exp",
        "bad-scope",
    ],
)
@pytest.mark.parametrize("fail_open", [False, True])
async def test_unavailable_is_closed_unless_explicitly_configured(
    failure: str, fail_open: bool
) -> None:
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic failure", request=request)
        if failure == "500":
            return httpx.Response(500)
        if failure == "redirect":
            return httpx.Response(
                302, headers={"Location": "https://other.example.com/"}
            )
        if failure == "invalid-json":
            return httpx.Response(200, text="[")
        return httpx.Response(
            200,
            json={
                "missing-active": {},
                "bad-active": {"active": "true"},
                "bad-exp": {"active": True, "exp": "1100"},
                "bad-scope": {"active": True, "scope": ["read"]},
            }[failure],
        )

    client = client_for(handler)
    cfg = config(fail_open=fail_open)
    for _ in range(2):
        result = await client.evaluate(TOKEN, cfg, server_id="server-example")
        assert result.deny_reason == (
            None if fail_open else "introspection_unavailable"
        )
        assert result.binding["available"] is False
    assert len(calls) == 1
    assert result.binding["cached"] is True


@pytest.mark.asyncio
async def test_positive_cache_bounded_by_exp_and_observes_revocation() -> None:
    now = [1000]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={"active": True, "exp": 1003}
            if len(calls) == 1
            else {"active": False},
        )

    client = client_for(handler, now)
    cfg = config(max_cache_ttl_seconds=60, negative_cache_ttl_seconds=2)
    first = await client.evaluate(TOKEN, cfg, server_id="server-example")
    assert first.deny_reason is None
    assert next(iter(client._cache.values())).expires_at == 1003
    now[0] = 1002
    assert (await client.evaluate(TOKEN, cfg, server_id="server-example")).binding[
        "cached"
    ]
    now[0] = 1003
    assert (
        await client.evaluate(TOKEN, cfg, server_id="server-example")
    ).deny_reason == "grant_inactive"
    assert len(calls) == 2
    now[0] = 1004
    assert (await client.evaluate(TOKEN, cfg, server_id="server-example")).binding[
        "cached"
    ]
    now[0] = 1005
    await client.evaluate(TOKEN, cfg, server_id="server-example")
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_shared_token_isolated_by_server_issuer_and_credentials() -> None:
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"active": True})

    client = client_for(handler)
    for server, cfg in [
        ("one", config()),
        ("two", config()),
        ("one", config(endpoint="https://another.example.com/introspect")),
        ("one", config(client_secret="replacement-secret")),
    ]:
        await client.evaluate(TOKEN, cfg, server_id=server)
    assert len(calls) == 4
    assert len(client._cache) == 4
    assert TOKEN not in json.dumps(list(client._cache))


@pytest.mark.asyncio
async def test_cache_is_bounded_and_returned_scopes_cannot_mutate_it() -> None:
    client = client_for(
        lambda request: httpx.Response(200, json={"active": True, "scope": "read"}),
        max_entries=2,
    )
    cfg = config()
    first = await client.evaluate(TOKEN, cfg, server_id="one")
    first.binding["scope"].append("write")
    cached = await client.evaluate(TOKEN, cfg, server_id="one")
    assert cached.binding["scope"] == ["read"]
    for server in ["two", "three"]:
        await client.evaluate(TOKEN, cfg, server_id=server)
    assert len(client._cache) == 2


@pytest.mark.asyncio
async def test_missing_token_never_calls_authorization_server() -> None:
    def handler(request):
        pytest.fail("missing bearer must not make an HTTP request")

    client = client_for(handler)
    result = await client.evaluate(None, config(), server_id="server-example")
    assert result.deny_reason == "introspection_unavailable"
    assert not client._cache


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", ["sub", "scope", "consent_ref", "client_id"])
async def test_echoed_bearer_is_never_retained_in_grant_metadata(claim: str) -> None:
    client = client_for(
        lambda request: httpx.Response(200, json={"active": True, claim: TOKEN})
    )
    result = await client.evaluate(TOKEN, config(), server_id="server-example")
    assert result.deny_reason == "introspection_unavailable"
    assert TOKEN not in repr(result.binding)
    assert TOKEN not in repr(client._cache)


@pytest.mark.asyncio
async def test_wall_clock_changes_do_not_extend_cache_ttl() -> None:
    wall, monotonic = [1000], [1000]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"active": len(calls) == 1})

    client = GrantIntrospector(
        client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), **kwargs
        ),
        clock=lambda: wall[0],
        monotonic_clock=lambda: monotonic[0],
    )
    await client.evaluate(
        TOKEN, config(max_cache_ttl_seconds=3), server_id="server-example"
    )
    wall[0] = 900
    monotonic[0] = 1003
    result = await client.evaluate(
        TOKEN, config(max_cache_ttl_seconds=3), server_id="server-example"
    )
    assert result.deny_reason == "grant_inactive"
    assert len(calls) == 2
