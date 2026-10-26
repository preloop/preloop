"""Synthetic MCP OAuth providers; no external provider traffic."""

import httpx
import pytest

from preloop.services.upstream_mcp_oauth import OAuthError, OAuthHTTP, discover

RESOURCE = "https://resource.example.com/mcp"
ISSUER = "https://identity.example.com/tenant"


@pytest.fixture
def provider():
    requests = []
    routes = {
        RESOURCE: (
            401,
            {},
            {
                "WWW-Authenticate": 'Bearer resource_metadata="https://resource.example.com/metadata"'
            },
        ),
        "https://resource.example.com/metadata": (
            200,
            {"resource": RESOURCE, "authorization_servers": [ISSUER]},
            {},
        ),
        "https://identity.example.com/.well-known/oauth-authorization-server/tenant": (
            200,
            {
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token",
                "registration_endpoint": ISSUER + "/register",
                "code_challenge_methods_supported": ["S256"],
            },
            {},
        ),
    }

    async def resolve(host, port):
        return ["93.184.216.34"]

    def handle(request):
        requests.append(request)
        url = str(request.url.copy_with(host=request.headers["host"]))
        status, body, headers = routes.get(url, (404, {}, {}))
        return httpx.Response(status, json=body, headers=headers)

    return (
        OAuthHTTP(transport=httpx.MockTransport(handle), resolver=resolve),
        routes,
        requests,
    )


@pytest.mark.asyncio
async def test_challenge_separate_issuer(provider):
    http, _, requests = provider
    metadata = await discover(RESOURCE, {}, http)
    assert metadata["issuer"] == ISSUER
    assert metadata["resource"] == RESOURCE
    assert all("authorization" not in r.headers for r in requests)


@pytest.mark.asyncio
async def test_explicit_resource_mismatch_never_uses_legacy(provider):
    http, routes, requests = provider
    routes["https://resource.example.com/metadata"] = (
        200,
        {"resource": "https://other.example.com", "authorization_servers": [ISSUER]},
        {},
    )
    with pytest.raises(OAuthError, match="resource"):
        await discover(RESOURCE, {}, http)
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_path_aware_well_known_fallback(provider):
    http, routes, _ = provider
    routes[RESOURCE] = (401, {}, {})
    routes["https://resource.example.com/.well-known/oauth-protected-resource/mcp"] = (
        routes.pop("https://resource.example.com/metadata")
    )
    assert (await discover(RESOURCE, {}, http))["issuer"] == ISSUER


@pytest.mark.asyncio
async def test_multiple_issuers_require_explicit_selection(provider):
    http, routes, _ = provider
    routes["https://resource.example.com/metadata"][1]["authorization_servers"].append(
        "https://other.example.com"
    )
    with pytest.raises(OAuthError, match="Configure an issuer"):
        await discover(RESOURCE, {}, http)
    assert (await discover(RESOURCE, {"issuer": ISSUER}, http))["issuer"] == ISSUER
    with pytest.raises(OAuthError, match="contradicts"):
        await discover(RESOURCE, {"issuer": "https://unlisted.example.com"}, http)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"issuer": "https://wrong.example.com"},
        {"authorization_endpoint": None},
        {"code_challenge_methods_supported": ["plain"]},
        {"response_types_supported": ["token"]},
    ],
)
async def test_invalid_authorization_metadata_is_terminal(provider, change):
    http, routes, _ = provider
    routes[
        "https://identity.example.com/.well-known/oauth-authorization-server/tenant"
    ][1].update(change)
    with pytest.raises(OAuthError):
        await discover(RESOURCE, {}, http)


@pytest.mark.asyncio
async def test_malformed_explicit_metadata_never_falls_back(provider):
    http, routes, requests = provider
    routes["https://resource.example.com/metadata"] = (200, ["invalid"], {})
    with pytest.raises(OAuthError, match="malformed"):
        await discover(RESOURCE, {}, http)
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_legacy_metadata_only_when_resource_metadata_is_absent(provider):
    http, routes, _ = provider
    routes[RESOURCE] = (401, {}, {})
    metadata = dict(
        routes[
            "https://identity.example.com/.well-known/oauth-authorization-server/tenant"
        ][1]
    )
    metadata["issuer"] = "https://resource.example.com"
    routes[
        "https://resource.example.com/.well-known/oauth-authorization-server/mcp"
    ] = (200, metadata, {})
    assert (await discover(RESOURCE, {}, http))["issuer"] == metadata["issuer"]


@pytest.mark.asyncio
async def test_public_registration_and_explicit_client(provider):
    from preloop.services.upstream_mcp_oauth import configure_client

    http, routes, requests = provider
    metadata = await discover(RESOURCE, {}, http)
    redirect = "https://console.example.com/callback"
    routes[ISSUER + "/register"] = (
        201,
        {"client_id": "synthetic-public", "redirect_uris": [redirect]},
        {},
    )
    config = await configure_client({}, metadata, redirect, http)
    assert config["client_id"] == "synthetic-public"
    registration = requests[-1]
    assert registration.method == "POST"
    import json

    assert json.loads(registration.content)["token_endpoint_auth_method"] == "none"
    count = len(requests)
    assert (
        await configure_client({"client_id": "explicit"}, metadata, redirect, http)
    )["client_id"] == "explicit"
    assert len(requests) == count
    metadata.pop("registration_endpoint")
    with pytest.raises(OAuthError, match="Configure a client_id"):
        await configure_client({}, metadata, redirect, http)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "::1",
        "::ffff:127.0.0.1",
        "100.64.0.1",
        "0.0.0.0",
    ],
)
async def test_private_destinations_blocked_before_network(address):
    calls = []

    async def resolver(host, port):
        return ["93.184.216.34", address]

    http = OAuthHTTP(
        resolver=resolver,
        transport=httpx.MockTransport(lambda request: calls.append(request)),
    )
    with pytest.raises(OAuthError, match="blocked"):
        await http.get("https://provider.example.com")
    assert calls == []


@pytest.mark.asyncio
async def test_http_loopback_requires_operator_opt_in(monkeypatch):
    async def resolver(host, port):
        return ["127.0.0.1"]

    http = OAuthHTTP(resolver=resolver)
    with pytest.raises(OAuthError):
        await http.validate("http://localhost:8080/mcp")
    monkeypatch.setenv("MCP_OAUTH_ALLOWED_CIDRS", "127.0.0.1/32")
    assert (await http.validate("http://localhost:8080/mcp"))[1] == "127.0.0.1"


@pytest.mark.asyncio
async def test_redirects_revalidated_and_loops_bounded(provider):
    http, routes, requests = provider
    routes[RESOURCE] = (302, {}, {"location": "https://identity.example.com/challenge"})
    routes["https://identity.example.com/challenge"] = (302, {}, {"location": RESOURCE})
    with pytest.raises(OAuthError, match="redirect loop"):
        await http.get(RESOURCE)
    assert len(requests) == 3
    assert all("authorization" not in request.headers for request in requests)


@pytest.mark.asyncio
async def test_retry_budget_and_retry_after(provider, monkeypatch):
    http, routes, requests = provider
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("preloop.services.upstream_mcp_oauth.asyncio.sleep", sleep)
    routes[RESOURCE] = (503, {}, {"retry-after": "1"})
    with pytest.raises(OAuthError, match="temporarily unavailable"):
        await http.get(RESOURCE)
    assert delays == [1, 1]
    assert len(requests) == 3
    routes[RESOURCE] = (429, {}, {"retry-after": "60"})
    http.retries = 0
    with pytest.raises(OAuthError):
        await http.get(RESOURCE)
    assert len(requests) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 429, 500, 503, 307])
async def test_token_failures_never_retry_or_expose_provider_body(
    provider, status, caplog
):
    from preloop.services.upstream_mcp_oauth import request_tokens

    http, routes, requests = provider
    routes[ISSUER + "/token"] = (
        status,
        {"error": "invalid_grant", "error_description": "synthetic-do-not-log"},
        {"location": "https://other.example.com"},
    )
    config = {
        "client_id": "synthetic",
        "refresh_token": "synthetic-refresh",
        "resource": RESOURCE,
        "token_endpoint": ISSUER + "/token",
    }
    with pytest.raises(OAuthError) as caught:
        await request_tokens(config, http)
    assert len(requests) == 1
    assert "synthetic-do-not-log" not in str(caught.value) + caplog.text
    assert "synthetic-refresh" not in str(caught.value) + caplog.text


@pytest.mark.asyncio
async def test_token_timeout_no_retry():
    calls = []

    async def resolver(host, port):
        return ["93.184.216.34"]

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic-sensitive-provider-message")

    http = OAuthHTTP(resolver=resolver, transport=httpx.MockTransport(handle))
    with pytest.raises(OAuthError, match="Reconnect") as caught:
        await http.post(ISSUER + "/token", data={"code": "synthetic-code"})
    assert len(calls) == 1
    assert "sensitive" not in str(caught.value)


@pytest.mark.asyncio
async def test_refresh_retains_old_refresh_only_when_omitted(provider):
    from preloop.services.upstream_mcp_oauth import request_tokens

    http, routes, _ = provider
    routes[ISSUER + "/token"] = (200, {"access_token": "new-access"}, {})
    config = {
        "client_id": "synthetic",
        "refresh_token": "old-refresh",
        "expires_at": 1,
        "resource": RESOURCE,
        "token_endpoint": ISSUER + "/token",
    }
    updated = await request_tokens(config, http)
    assert updated["refresh_token"] == "old-refresh"
    assert "expires_at" not in updated
    routes[ISSUER + "/token"] = (
        200,
        {"access_token": "new-access", "refresh_token": ""},
        {},
    )
    with pytest.raises(OAuthError):
        await request_tokens(config, http)


def test_authorization_binds_scope_resource_redirect_and_s256():
    from urllib.parse import parse_qs, urlsplit
    from preloop.services.upstream_mcp_oauth import authorization_url

    url = authorization_url(
        {
            "authorization_endpoint": ISSUER + "/authorize",
            "client_id": "synthetic",
            "redirect_uri": "https://console.example.com/callback",
            "resource": RESOURCE,
            "scopes": ["read", "write"],
        },
        "synthetic-state",
        "synthetic-verifier",
    )
    params = parse_qs(urlsplit(url).query)
    assert params["scope"] == ["read write"]
    assert params["resource"] == [RESOURCE]
    assert params["code_challenge_method"] == ["S256"]
    assert "synthetic-verifier" not in url
