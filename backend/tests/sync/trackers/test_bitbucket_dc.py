"""Tests for the Bitbucket Data Center 10.2 tracker and its helpers.

All payloads are synthetic fixtures from ``tests/fixtures/bitbucket_dc``;
no network is used. The approved instance is
``https://bitbucket.example.com/bitbucket`` (context path) and requests are
served by an ``httpx.MockTransport`` so every call is deterministic.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs

import httpx
import pytest

from preloop.sync.exceptions import (
    TrackerAuthenticationError,
    TrackerConnectionError,
    TrackerPermissionError,
    TrackerRateLimitError,
    TrackerResponseError,
)
from preloop.sync.trackers.bitbucket_dc import (
    CAPABILITIES,
    BitbucketDCConflictError,
    BitbucketDCTracker,
    BitbucketDCUnsupportedOperationError,
)
from preloop.utils import bitbucket_dc as dc

pytestmark = pytest.mark.asyncio

os.environ.setdefault("PRELOOP_DISABLE_TELEMETRY", "true")

INSTANCE = "https://bitbucket.example.com/bitbucket"
REST = f"{INSTANCE}/rest/api/1.0"
REST_PATH = "/bitbucket/rest/api/1.0"
REPO_PATH = f"{REST_PATH}/projects/PRJ/repos/my-repo"
PR_PATH = f"{REPO_PATH}/pull-requests/101"
FIXTURES = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "bitbucket_dc"
        / "payloads.json"
    ).read_text()
)
Handler = Callable[[httpx.Request], httpx.Response]


def fx(name: str) -> Dict[str, Any]:
    """Return a deep copy of a named fixture payload."""
    return copy.deepcopy(FIXTURES[name])


def page(
    values: List[Any],
    *,
    last: bool = True,
    start: int = 0,
    next_start: Optional[int] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "size": len(values),
        "limit": 100,
        "isLastPage": last,
        "values": values,
        "start": start,
    }
    if not last and next_start is not None:
        body["nextPageStart"] = next_start
    return body


def ok(
    data: Any = None, status: int = 200, headers: Optional[Dict[str, str]] = None
) -> httpx.Response:
    if data is None and status == 204:
        return httpx.Response(status, headers=headers)
    return httpx.Response(
        status, json=data if data is not None else {}, headers=headers
    )


@pytest.fixture(autouse=True)
def dc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRELOOP_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv(dc.ENV_ENABLED, "true")
    monkeypatch.setenv(
        dc.ENV_INSTANCES, json.dumps([INSTANCE, "https://scm.example.com:8443"])
    )
    # TEST-NET-3 is not globally routable, so the policy treats it like a
    # private network; allowlist it so fixtures can use documentation addresses.
    monkeypatch.setenv(dc.ENV_PRIVATE_NETWORKS, "203.0.113.0/24")
    monkeypatch.delenv(dc.ENV_CA_BUNDLE, raising=False)


def make_tracker(
    handler: Handler,
    requests: List[httpx.Request],
    *,
    resolver: Optional[dc.Resolver] = None,
    **details: Any,
) -> BitbucketDCTracker:
    def record(request: httpx.Request) -> httpx.Response:
        # Most tests isolate one REST resource. Supply its normal repository
        # preflight fixture without counting that boilerplate in resource-call
        # assertions. Explicit repository routes override it. Full preflight
        # ordering and slug-reuse attacks are tested in test_bitbucket_dc_identity.
        key = f"{request.method} {request.url.path}"
        if key == f"GET {REPO_PATH}" and key not in getattr(handler, "routes", set()):
            return ok(fx("repository"))
        requests.append(request)
        return handler(request)

    connection: Dict[str, Any] = {
        "instance_url": INSTANCE,
        "project_key": "PRJ",
        "repository_slug": "my-repo",
        "repository_id": 42,
        "username": "rev",
    }
    connection.update(details)
    for key in [k for k, v in connection.items() if v is None]:
        connection.pop(key)
    return BitbucketDCTracker(
        "tracker-1",
        "secret-pat",
        connection,
        transport=httpx.MockTransport(record),
        resolver=resolver,
    )


def route(table: Dict[str, Any]) -> Handler:
    """Build a handler from ``"METHOD /path" -> response or callable``."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        entry = table.get(key)
        if entry is None:
            return ok({"errors": [{"message": f"unrouted {key}"}]}, 404)
        if callable(entry):
            return entry(request)
        return ok(entry)

    handler.routes = set(table)
    return handler


# ----------------------------------------------------------------------
# Configuration and policy
# ----------------------------------------------------------------------


def test_feature_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(dc.ENV_ENABLED, raising=False)
    assert dc.bitbucket_dc_enabled() is False
    with pytest.raises(dc.BitbucketDCConfigError, match="not enabled"):
        dc.validate_bitbucket_dc_config(
            api_key="pat",
            auth_type="api_token",
            connection_details={"instance_url": INSTANCE},
        )


def test_validate_config_normalises_and_accepts() -> None:
    result = dc.validate_bitbucket_dc_config(
        api_key="pat",
        auth_type="api_token",
        connection_details={
            "instance_url": INSTANCE + "/",
            "project_key": " PRJ ",
            "repository_slug": "my-repo",
            "repository_id": "42",
            "username": "jdoe",
        },
    )
    assert result["instance_url"] == INSTANCE
    assert result["project_key"] == "PRJ"
    assert result["repository_id"] == 42
    assert result["version"] == "10.2"


@pytest.mark.parametrize(
    "details, message",
    [
        ({"instance_url": "http://bitbucket.example.com/bitbucket"}, "https"),
        (
            {"instance_url": "https://user:pw@bitbucket.example.com/bitbucket"},
            "credentials",
        ),
        ({"instance_url": INSTANCE + "?x=1"}, "query"),
        ({"instance_url": INSTANCE + "#frag"}, "query or fragment"),
        (
            {"instance_url": "https://bitbucket.example.com/bitbucket/../admin"},
            "traversal",
        ),
        ({"instance_url": "https://bitbucket.example.com/bitbucket/%2e%2e"}, "percent"),
        (
            {"instance_url": "https://bitbucket.example.com//bitbucket"},
            "empty segments",
        ),
        (
            {"instance_url": "https://bitbucket.example.com:8443/bitbucket"},
            "not in the administrator-approved",
        ),
        (
            {"instance_url": "https://bitbucket.example.com"},
            "not in the administrator-approved",
        ),
        (
            {"instance_url": "https://bitbucket.example.com/bitbucket/other"},
            "not in the administrator-approved",
        ),
        (
            {"instance_url": "https://evil.example.net"},
            "not in the administrator-approved",
        ),
        (
            {"instance_url": "https://169.254.169.254/bitbucket"},
            "link-local or metadata",
        ),
        ({"instance_url": "https://[fe80::1]/bitbucket"}, "link-local or metadata"),
        ({"instance_url": "https://127.0.0.1"}, "loopback"),
        ({"instance_url": INSTANCE, "version": "9.6"}, "not validated"),
        ({"instance_url": INSTANCE, "repository_slug": "x"}, "requires project_key"),
        (
            {"instance_url": INSTANCE, "project_key": "PRJ", "repository_slug": "../x"},
            "repository slug",
        ),
        ({"instance_url": INSTANCE, "project_key": "PRJ/evil"}, "project key"),
        (
            {"instance_url": INSTANCE, "project_key": "PRJ", "repository_id": "abc"},
            "positive integer",
        ),
        (
            {"instance_url": INSTANCE, "project_key": "PRJ", "repository_id": -1},
            "positive integer",
        ),
        (
            {"instance_url": INSTANCE, "repository_id": 42},
            "repository_id requires project_key",
        ),
        ({"instance_url": INSTANCE, "username": "a/b"}, "user slug"),
    ],
)
def test_validate_config_rejects(details: Dict[str, Any], message: str) -> None:
    with pytest.raises(dc.BitbucketDCConfigError, match=message):
        dc.validate_bitbucket_dc_config(
            api_key="pat", auth_type="api_token", connection_details=details
        )


@pytest.mark.parametrize(
    "auth_type", ["oauth_token", "app_password", "basic", "oauth_app"]
)
def test_validate_config_only_pat(auth_type: str) -> None:
    with pytest.raises(dc.BitbucketDCConfigError, match="auth_type"):
        dc.validate_bitbucket_dc_config(
            api_key="pat",
            auth_type=auth_type,
            connection_details={"instance_url": INSTANCE},
        )


@pytest.mark.parametrize("token", ["", "   ", "user:pat", "Bearer abc"])
def test_validate_config_token_shape(token: str) -> None:
    with pytest.raises(dc.BitbucketDCConfigError, match="token"):
        dc.validate_bitbucket_dc_config(
            api_key=token,
            auth_type="api_token",
            connection_details={"instance_url": INSTANCE},
        )


def test_instances_env_must_be_canonical_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(dc.ENV_INSTANCES, "not json")
    with pytest.raises(dc.BitbucketDCConfigError, match="JSON array"):
        dc.approved_instances()
    monkeypatch.setenv(
        dc.ENV_INSTANCES, json.dumps(["https://BitBucket.example.com/bitbucket/"])
    )
    with pytest.raises(dc.BitbucketDCConfigError, match="not canonical"):
        dc.approved_instances()
    monkeypatch.setenv(dc.ENV_INSTANCES, json.dumps([{"url": INSTANCE}]))
    with pytest.raises(dc.BitbucketDCConfigError):
        dc.approved_instances()


def test_canonical_url_keeps_explicit_nondefault_port_and_lowercases_host() -> None:
    assert (
        dc.canonical_instance_url("https://SCM.example.com:8443/")
        == "https://scm.example.com:8443"
    )
    assert (
        dc.canonical_instance_url("https://scm.example.com:443/ctx/")
        == "https://scm.example.com/ctx"
    )
    identity = dc.parse_instance_url(INSTANCE)
    assert identity.owns_url(f"{INSTANCE}/projects/PRJ")
    assert not identity.owns_url("https://bitbucket.example.com/other/projects")
    assert not identity.owns_url("https://bitbucket.example.com:8443/bitbucket/x")
    assert not identity.owns_url("http://bitbucket.example.com/bitbucket/x")


def test_private_networks_and_ca_bundle_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(dc.ENV_PRIVATE_NETWORKS, "10.0.0.0/8, 192.168.1.0/24")
    assert [str(n) for n in dc.private_networks()] == ["10.0.0.0/8", "192.168.1.0/24"]
    monkeypatch.setenv(dc.ENV_PRIVATE_NETWORKS, "nope")
    with pytest.raises(dc.BitbucketDCConfigError, match="CIDR"):
        dc.private_networks()
    monkeypatch.setenv(dc.ENV_CA_BUNDLE, str(tmp_path / "missing.pem"))
    with pytest.raises(dc.BitbucketDCConfigError, match="CA_BUNDLE"):
        dc.ca_bundle_path()
    bundle = tmp_path / "ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv(dc.ENV_CA_BUNDLE, str(bundle))
    assert dc.ca_bundle_path() == str(bundle)


# ----------------------------------------------------------------------
# Destination pinning
# ----------------------------------------------------------------------


def test_resolve_pinned_address_allowlisted_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = dc.parse_instance_url(INSTANCE)
    assert (
        dc.resolve_pinned_address(identity, resolver=lambda h, p: ["203.0.113.10"])
        == "203.0.113.10"
    )
    # Without the allowlist a non-global address is refused.
    monkeypatch.delenv(dc.ENV_PRIVATE_NETWORKS)
    with pytest.raises(dc.BitbucketDCConfigError, match="not allowlisted"):
        dc.resolve_pinned_address(identity, resolver=lambda h, p: ["203.0.113.10"])
    with pytest.raises(dc.BitbucketDCConfigError, match="not allowlisted"):
        dc.resolve_pinned_address(
            identity, resolver=lambda h, p: ["100.64.0.9"]
        )  # CGNAT


@pytest.mark.parametrize(
    "addresses, message",
    [
        (["169.254.169.254"], "link-local or metadata"),
        (["203.0.113.10", "169.254.169.254"], "link-local or metadata"),
        (["fe80::1"], "link-local or metadata"),
        (["fd00:ec2::254"], "link-local or metadata"),
        (["::ffff:169.254.169.254"], "link-local or metadata"),
        (["127.0.0.1"], "loopback"),
        (["::1"], "loopback"),
        (["0.0.0.0"], "unspecified"),
        (["10.1.2.3"], "not allowlisted"),
        (["192.168.0.5"], "not allowlisted"),
        ([], "Could not resolve"),
    ],
)
def test_resolve_pinned_address_refuses(addresses: List[str], message: str) -> None:
    identity = dc.parse_instance_url(INSTANCE)
    with pytest.raises(dc.BitbucketDCConfigError, match=message):
        dc.resolve_pinned_address(identity, resolver=lambda h, p: addresses)


def test_private_address_allowed_only_inside_allowlisted_cidr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = dc.parse_instance_url(INSTANCE)
    monkeypatch.setenv(dc.ENV_PRIVATE_NETWORKS, "10.1.0.0/16")
    assert (
        dc.resolve_pinned_address(identity, resolver=lambda h, p: ["10.1.2.3"])
        == "10.1.2.3"
    )
    with pytest.raises(dc.BitbucketDCConfigError, match="not allowlisted"):
        dc.resolve_pinned_address(identity, resolver=lambda h, p: ["10.2.2.3"])
    # Link-local stays refused even when its range is allowlisted.
    monkeypatch.setenv(dc.ENV_PRIVATE_NETWORKS, "169.254.0.0/16,127.0.0.0/8")
    with pytest.raises(dc.BitbucketDCConfigError, match="link-local"):
        dc.resolve_pinned_address(identity, resolver=lambda h, p: ["169.254.169.254"])
    with pytest.raises(dc.BitbucketDCConfigError, match="loopback"):
        dc.resolve_pinned_address(identity, resolver=lambda h, p: ["127.0.0.1"])


async def test_requests_pin_resolved_address_and_keep_tls_hostname() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route(
            {f"GET {REST_PATH}/application-properties": fx("application_properties")}
        ),
        requests,
        resolver=lambda host, port: ["203.0.113.10"],
    )
    await tracker.get_server_version()
    request = requests[0]
    assert request.url.host == "203.0.113.10"
    assert request.url.path == f"{REST_PATH}/application-properties"
    assert request.headers["Host"] == "bitbucket.example.com"
    assert request.extensions["sni_hostname"] == "bitbucket.example.com"
    assert request.headers["Authorization"] == "Bearer secret-pat"


async def test_requests_pin_nondefault_port_in_host_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route(
            {"GET /rest/api/1.0/application-properties": fx("application_properties")}
        ),
        requests,
        resolver=lambda host, port: ["203.0.113.20"],
        instance_url="https://scm.example.com:8443",
    )
    await tracker.get_server_version()
    assert requests[0].url.host == "203.0.113.20"
    assert requests[0].url.port == 8443
    assert requests[0].headers["Host"] == "scm.example.com:8443"
    assert requests[0].extensions["sni_hostname"] == "scm.example.com"


async def test_metadata_resolution_blocks_request_before_sending() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        lambda r: ok({}), requests, resolver=lambda h, p: ["169.254.169.254"]
    )
    with pytest.raises(TrackerConnectionError, match="link-local"):
        await tracker.get_server_version()
    assert requests == []


async def test_redirects_are_not_followed() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        lambda r: httpx.Response(
            302, headers={"Location": "https://evil.example.net/"}
        ),
        requests,
    )
    with pytest.raises(TrackerResponseError, match="redirect"):
        await tracker.get_server_version()
    assert len(requests) == 1


async def test_absolute_links_are_never_requested() -> None:
    tracker = make_tracker(lambda r: ok({}), [])
    for link in (
        "https://bitbucket.example.com/bitbucket/rest/api/1.0/projects",
        "https://evil.example.net/x",
        "//evil.example.net/x",
        "/bitbucket/rest/api/1.0/projects",
    ):
        with pytest.raises(TrackerResponseError, match="outside the instance"):
            await tracker._request("GET", link)


async def test_client_disables_trust_env_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: Dict[str, Any] = {}
    real = httpx.AsyncClient

    class Spy(real):  # type: ignore[misc]
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr("preloop.sync.trackers.bitbucket_dc.httpx.AsyncClient", Spy)
    tracker = make_tracker(
        route(
            {f"GET {REST_PATH}/application-properties": fx("application_properties")}
        ),
        [],
    )
    await tracker.get_server_version()
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False


def test_ssl_context_uses_private_ca_and_keeps_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import ssl

    bundle = tmp_path / "ca.pem"
    bundle.write_text("placeholder")
    monkeypatch.setenv(dc.ENV_CA_BUNDLE, str(bundle))
    seen: Dict[str, Any] = {}
    real = ssl.create_default_context

    def spy(**kwargs: Any) -> ssl.SSLContext:
        seen.update(kwargs)
        return real()

    monkeypatch.setattr(
        "preloop.sync.trackers.bitbucket_dc.ssl.create_default_context", spy
    )
    tracker = make_tracker(lambda r: ok({}), [])
    context = tracker._ssl()
    assert seen == {"cafile": str(bundle)}
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert tracker._ssl() is context


def test_constructor_rejects_unapproved_instance_and_wrong_auth() -> None:
    with pytest.raises(
        dc.BitbucketDCConfigError, match="not in the administrator-approved"
    ):
        BitbucketDCTracker("t", "pat", {"instance_url": "https://other.example.com"})
    with pytest.raises(dc.BitbucketDCConfigError, match="auth_type"):
        BitbucketDCTracker(
            "t", "pat", {"instance_url": INSTANCE, "auth_type": "oauth_token"}
        )


# ----------------------------------------------------------------------
# Errors and refusals
# ----------------------------------------------------------------------


async def test_error_statuses_map_to_tracker_errors() -> None:
    tracker = make_tracker(lambda r: ok(fx("error_401"), 401), [])
    with pytest.raises(TrackerAuthenticationError, match="401"):
        await tracker.get_pull_request(101)
    tracker = make_tracker(lambda r: ok(fx("error_403"), 403), [])
    with pytest.raises(TrackerPermissionError, match="not permitted") as exc:
        await tracker.get_pull_request(101)
    assert exc.value.status_code == 403
    tracker = make_tracker(
        lambda r: httpx.Response(429, headers={"Retry-After": "7"}), []
    )
    with pytest.raises(TrackerRateLimitError, match="Retry after 7s"):
        await tracker.get_pull_request(101)
    tracker = make_tracker(lambda r: ok(fx("error_409_version"), 409), [])
    with pytest.raises(BitbucketDCConflictError, match="out-of-date") as conflict:
        await tracker.get_pull_request(101)
    assert conflict.value.status_code == 409
    tracker = make_tracker(lambda r: httpx.Response(500, text="boom"), [])
    with pytest.raises(TrackerResponseError) as err:
        await tracker.get_pull_request(101)
    assert err.value.status_code == 500


async def test_connection_errors_are_wrapped() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    tracker = make_tracker(boom, [])
    with pytest.raises(TrackerConnectionError, match="ConnectError"):
        await tracker.get_server_version()


@pytest.mark.parametrize(
    "method, path",
    [
        ("POST", "projects/PRJ/repos/my-repo/pull-requests/101/merge"),
        ("POST", "projects/PRJ/repos/my-repo/pull-requests/101/decline/"),
        ("POST", "projects/PRJ/repos/my-repo/pull-requests/101/merge?version=1"),
        ("GET", "projects/PRJ/repos/my-repo/pull-requests/101/merge"),
        ("DELETE", "projects/PRJ/repos/my-repo/pull-requests/101"),
    ],
)
async def test_merge_decline_delete_refused_in_transport(
    method: str, path: str
) -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(lambda r: ok(), requests)
    with pytest.raises(BitbucketDCUnsupportedOperationError, match="never"):
        await tracker._request(method, path)
    assert requests == []


async def test_public_merge_decline_methods_refused() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(lambda r: ok(), requests)
    with pytest.raises(BitbucketDCUnsupportedOperationError):
        await tracker.merge_pull_request(101)
    with pytest.raises(BitbucketDCUnsupportedOperationError):
        await tracker.decline_pull_request(101)
    assert requests == []
    assert CAPABILITIES["pull_request_merge"] is False
    assert CAPABILITIES["pull_request_decline"] is False
    assert "pull_request_merge" in tracker.unsupported_operations
    assert tracker.hosts_repositories is True and tracker.hosts_issues is False


async def test_issue_and_webhook_operations_are_explicitly_unsupported() -> None:
    tracker = make_tracker(lambda r: ok(), [])
    with pytest.raises(BitbucketDCUnsupportedOperationError):
        await tracker.get_issue("1")
    with pytest.raises(BitbucketDCUnsupportedOperationError):
        await tracker.add_comment("1", "x")
    with pytest.raises(BitbucketDCUnsupportedOperationError):
        await tracker.register_webhook(
            db=None, project=None, webhook_url="https://p.example.com", secret="s"
        )
    assert await tracker.get_issues("PRJ", "42") == []
    assert await tracker.search_issues("PRJ/my-repo", None) == ([], 0)  # type: ignore[arg-type]
    assert await tracker.get_webhooks() == []
    assert await tracker.is_webhook_registered_for_project(None, "u") is False  # type: ignore[arg-type]
    assert await tracker.unregister_all_webhooks(None) == {
        "unregistered": 0,
        "failed": 0,
        "not_found": 0,
    }  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Connection, discovery and identity
# ----------------------------------------------------------------------


async def test_test_connection_reports_validated_baseline_and_context_path() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route(
            {
                f"GET {REST_PATH}/application-properties": fx("application_properties"),
                f"GET {REPO_PATH}": fx("repository"),
            }
        ),
        requests,
    )
    result = await tracker.test_connection()
    assert result.connected, result.message
    assert result.server_info["validated"] is True
    assert result.server_info["version"] == "10.2.1"
    assert result.server_info["instance_url"] == INSTANCE
    assert result.server_info["capabilities"]["pull_request_merge"] is False
    assert [r.url.path for r in requests] == [
        f"{REST_PATH}/application-properties",
        REPO_PATH,
    ]


async def test_test_connection_marks_other_release_unvalidated() -> None:
    props = fx("application_properties")
    props["version"] = "9.6.4"
    tracker = make_tracker(
        route(
            {
                f"GET {REST_PATH}/application-properties": props,
                f"GET {REPO_PATH}": fx("repository"),
            }
        ),
        [],
    )
    result = await tracker.test_connection()
    # An unvalidated release does not pass the connection gate, and is
    # reported as such rather than silently treated as Cloud or as 10.2.
    assert result.connected is False
    assert result.server_info["validated"] is False
    assert result.server_info["version"] == "9.6.4"
    assert "unvalidated" in result.message


async def test_test_connection_with_id_only_binding_resolves_by_id() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route(
            {
                f"GET {REST_PATH}/application-properties": fx("application_properties"),
                f"GET {REST_PATH}/projects/PRJ/repos": page([fx("repository")]),
            }
        ),
        requests,
        repository_slug=None,
    )
    result = await tracker.test_connection()
    assert result.connected, result.message
    assert tracker.repository_slug == "my-repo"
    assert [r.url.path for r in requests] == [
        f"{REST_PATH}/application-properties",
        f"{REST_PATH}/projects/PRJ/repos",
    ]


async def test_test_connection_failure_returns_message() -> None:
    tracker = make_tracker(lambda r: ok(fx("error_401"), 401), [])
    result = await tracker.test_connection()
    assert result.connected is False
    assert "401" in result.message


async def test_discovery_without_project_lists_projects_and_repositories() -> None:
    repo = fx("repository")
    other = fx("repository")
    other.update({"slug": "second", "id": 43, "name": "Second"})
    tracker = make_tracker(
        route(
            {
                f"GET {REST_PATH}/projects": page([fx("project")]),
                f"GET {REST_PATH}/projects/PRJ/repos": page([repo, other]),
            }
        ),
        [],
        project_key=None,
        repository_slug=None,
        repository_id=None,
    )
    orgs = await tracker.get_organizations()
    assert orgs == [
        {"id": "PRJ", "name": "Example Project", "project_id": 7, "type": "NORMAL"}
    ]
    projects = await tracker.get_projects("PRJ")
    assert [p["id"] for p in projects] == ["42", "43"]
    first = projects[0]
    assert first["meta_data"]["full_name"] == "PRJ/my-repo"
    assert first["meta_data"]["repository_id"] == 42
    assert first["meta_data"]["instance_url"] == INSTANCE
    assert first["meta_data"]["default_branch"] == "main"
    assert first["url"] == f"{INSTANCE}/projects/PRJ/repos/my-repo/browse"
    # Clone links are returned as metadata only.
    assert {link["name"] for link in first["meta_data"]["clone_links"]} == {
        "http",
        "ssh",
    }
    transformed = tracker.transform_project(first, "org-db-id")
    assert transformed["identifier"] == "42"
    assert transformed["slug"] == "PRJ/my-repo"


async def test_discovery_refuses_project_outside_bound_project() -> None:
    tracker = make_tracker(lambda r: ok(page([])), [])
    with pytest.raises(dc.BitbucketDCIdentityError, match="outside"):
        await tracker.get_projects("OTHER")


async def test_repository_rename_is_followed_by_immutable_id() -> None:
    renamed = fx("repository")
    renamed["slug"] = "my-repo-renamed"
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route(
            {
                f"GET {REPO_PATH}": lambda r: ok(fx("error_403"), 404),
                f"GET {REST_PATH}/projects/PRJ/repos": page([renamed]),
            }
        ),
        requests,
    )
    projects = await tracker.get_projects("PRJ")
    assert projects[0]["id"] == "42"
    assert projects[0]["meta_data"]["repository_slug"] == "my-repo-renamed"
    assert tracker.repository_slug == "my-repo-renamed"
    assert tracker.connection_details["repository_slug"] == "my-repo-renamed"


async def test_reused_slug_with_different_id_is_rejected() -> None:
    impostor = fx("repository")
    impostor["id"] = 99
    tracker = make_tracker(
        route(
            {
                f"GET {REPO_PATH}": impostor,
                f"GET {REST_PATH}/projects/PRJ/repos": page([]),
            }
        ),
        [],
    )
    with pytest.raises(TrackerResponseError, match="no longer exists"):
        await tracker.get_projects("PRJ")


async def test_renamed_repository_missing_from_project_is_reported() -> None:
    tracker = make_tracker(
        route(
            {
                f"GET {REPO_PATH}": lambda r: ok({}, 404),
                f"GET {REST_PATH}/projects/PRJ/repos": page([]),
            }
        ),
        [],
    )
    with pytest.raises(TrackerResponseError, match="no longer exists"):
        await tracker.resolve_repository()


async def test_cross_tenant_pull_request_is_rejected() -> None:
    foreign = fx("pull_request")
    foreign["toRef"]["repository"] = {
        "slug": "my-repo",
        "id": 42,
        "project": {"key": "OTHER", "id": 9},
    }
    tracker = make_tracker(route({f"GET {PR_PATH}": foreign}), [])
    with pytest.raises(dc.BitbucketDCIdentityError, match="belongs to OTHER/my-repo"):
        await tracker.get_pull_request(101)
    swapped = fx("pull_request")
    swapped["toRef"]["repository"]["id"] = 77
    tracker = make_tracker(route({f"GET {PR_PATH}": swapped}), [])
    with pytest.raises(dc.BitbucketDCIdentityError, match="now has id 77"):
        await tracker.get_pull_request(101)


async def test_explicit_repo_argument_is_validated() -> None:
    tracker = make_tracker(lambda r: ok(fx("pull_request")), [])
    for bad in ("PRJ", "PRJ/a/b", "PRJ/../x", "PRJ/my repo", "../PRJ/x"):
        with pytest.raises(TrackerResponseError):
            await tracker.get_pull_request(101, bad)


async def test_personal_project_key_path_is_encoded() -> None:
    requests: List[httpx.Request] = []
    pr = fx("pull_request")
    pr["toRef"]["repository"] = {
        "slug": "notes",
        "id": 5,
        "project": {"key": "~jdoe", "id": 3},
    }
    tracker = make_tracker(
        lambda r: ok(
            pr["toRef"]["repository"] if r.url.path.endswith("/notes") else pr
        ),
        requests,
        project_key="~jdoe",
        repository_slug="notes",
        repository_id=5,
    )
    await tracker.get_pull_request(101)
    assert (
        requests[1].url.path
        == f"{REST_PATH}/projects/~jdoe/repos/notes/pull-requests/101"
    )


# ----------------------------------------------------------------------
# Cloud payload rejection
# ----------------------------------------------------------------------


def test_cloud_payloads_are_rejected_by_helpers() -> None:
    with pytest.raises(dc.BitbucketDCIdentityError, match="Cloud pull request"):
        dc.build_object_attributes(fx("cloud_pull_request"))
    with pytest.raises(dc.BitbucketDCIdentityError, match="Cloud comment"):
        dc.normalize_comment(fx("cloud_comment"))
    assert dc.looks_like_cloud_payload(fx("cloud_repository"))
    assert not dc.looks_like_cloud_payload(fx("repository"))
    assert not dc.looks_like_cloud_payload(fx("pull_request"))


async def test_cloud_pull_request_from_server_is_rejected() -> None:
    tracker = make_tracker(route({f"GET {PR_PATH}": fx("cloud_pull_request")}), [])
    with pytest.raises(dc.BitbucketDCIdentityError, match="Cloud"):
        await tracker.get_pull_request(101)


# ----------------------------------------------------------------------
# Pagination
# ----------------------------------------------------------------------


async def test_pagination_follows_noncontiguous_next_page_start() -> None:
    requests: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("start", "0"))
        if start == 0:
            return ok(page([{"id": 1}], last=False, start=0, next_start=25))
        if start == 25:
            return ok(
                page([], last=False, start=25, next_start=60)
            )  # empty middle page
        if start == 60:
            return ok(page([{"id": 3}], last=True, start=60))
        raise AssertionError(f"unexpected start {start}")

    tracker = make_tracker(handler, requests)
    values = await tracker._paginate("projects")
    assert [v["id"] for v in values] == [1, 3]
    assert [r.url.params["start"] for r in requests] == ["0", "25", "60"]
    assert requests[0].url.params["limit"] == "100"


@pytest.mark.parametrize(
    "body, message",
    [
        (
            {"values": [{"id": 1}], "isLastPage": False, "nextPageStart": 0},
            "did not progress",
        ),
        ({"values": [{"id": 1}], "isLastPage": False}, "no integer 'nextPageStart'"),
        (
            {"values": [{"id": 1}], "isLastPage": False, "nextPageStart": "25"},
            "no integer 'nextPageStart'",
        ),
        ({"values": "nope", "isLastPage": True}, "no 'values' list"),
        ({"values": [], "isLastPage": "yes"}, "no boolean 'isLastPage'"),
        ([], "not a JSON object"),
    ],
)
async def test_pagination_rejects_malformed_pages(body: Any, message: str) -> None:
    tracker = make_tracker(lambda r: ok(body), [])
    with pytest.raises(TrackerResponseError, match=message):
        await tracker._paginate("projects")


async def test_pagination_rejects_backwards_cursor_and_bounds_pages() -> None:
    def backwards(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("start", "0"))
        if start == 0:
            return ok(page([{"id": 1}], last=False, start=0, next_start=50))
        return ok(page([{"id": 2}], last=False, start=50, next_start=10))

    tracker = make_tracker(backwards, [])
    with pytest.raises(TrackerResponseError, match="did not progress"):
        await tracker._paginate("projects")

    def endless(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("start", "0"))
        return ok(page([{"id": start}], last=False, start=start, next_start=start + 1))

    requests: List[httpx.Request] = []
    tracker = make_tracker(endless, requests)
    with pytest.raises(TrackerResponseError, match="exceeded 3 pages"):
        await tracker._paginate("projects", max_pages=3)
    assert len(requests) == 3


def test_next_page_start_helper_last_page_and_empty() -> None:
    seen: set[int] = set()
    assert (
        dc.next_page_start(
            {"values": [], "isLastPage": True}, current_start=0, seen_starts=seen
        )
        is None
    )
    assert seen == {0}
    assert (
        dc.next_page_start(
            {"values": [{"id": 1}], "isLastPage": False, "nextPageStart": 7},
            current_start=0,
            seen_starts=seen,
        )
        == 7
    )
    with pytest.raises(dc.BitbucketDCPaginationError):
        dc.next_page_start(
            {"values": [{"id": 1}], "isLastPage": False, "nextPageStart": 7},
            current_start=7,
            seen_starts=seen,
        )
    with pytest.raises(dc.BitbucketDCPaginationError, match="malformed values"):
        dc.next_page_start(
            {"values": [1], "isLastPage": True}, current_start=0, seen_starts=set()
        )


# ----------------------------------------------------------------------
# Branches
# ----------------------------------------------------------------------


async def test_branch_exists_requires_exact_match() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route({f"GET {REPO_PATH}/branches": page([fx("branch_feature")])}), requests
    )
    assert await tracker.branch_exists("feature/retry") is True
    assert await tracker.branch_exists("feature/ret") is False
    assert requests[0].url.params["filterText"] == "feature/retry"
    with pytest.raises(TrackerResponseError, match="Invalid branch"):
        await tracker.branch_exists("-bad")


async def test_get_default_branch() -> None:
    tracker = make_tracker(
        route(
            {
                f"GET {REPO_PATH}/default-branch": {
                    "id": "refs/heads/main",
                    "displayId": "main",
                }
            }
        ),
        [],
    )
    assert await tracker.get_default_branch() == "main"
    tracker = make_tracker(lambda r: ok({}, 404), [])
    assert await tracker.get_default_branch() is None


# ----------------------------------------------------------------------
# Pull requests
# ----------------------------------------------------------------------


async def test_list_pull_requests_filters_by_full_ref_and_reports_has_more() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route(
            {
                f"GET {REPO_PATH}/pull-requests": page(
                    [fx("pull_request")], last=False, next_start=1
                )
            }
        ),
        requests,
    )
    listing = await tracker.list_open_pull_requests_by_source_branch("feature/retry")
    params = requests[0].url.params
    assert params["at"] == "refs/heads/feature/retry"
    assert params["direction"] == "OUTGOING"
    assert params["state"] == "OPEN"
    assert params["start"] == "0"
    assert listing["has_more"] is True
    item = listing["items"][0]
    assert item["number"] == 101 and item["iid"] == 101
    assert item["source_branch"] == "feature/retry"
    assert item["target_branch"] == "main"
    assert item["url"] == f"{INSTANCE}/projects/PRJ/repos/my-repo/pull-requests/101"
    assert item["author"] == "jdoe"
    assert item["version"] == 3
    assert tracker._first_listed_for_branch(listing, "feature/retry") is item
    assert tracker._first_listed_for_branch(listing, "other") is None


async def test_list_pull_requests_page_walks_next_page_start_cursors() -> None:
    requests: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        if start == 0:
            return ok(page([fx("pull_request")], last=False, start=0, next_start=10))
        if start == 10:
            return ok(page([fx("pull_request")], last=False, start=10, next_start=37))
        if start == 37:
            merged = fx("pull_request")
            merged.update({"id": 7, "state": "MERGED"})
            return ok(page([merged], last=True, start=37))
        raise AssertionError(start)

    tracker = make_tracker(handler, requests)
    listing = await tracker.list_pull_requests(state="merged", limit=10, page=3)
    assert [r.url.params["start"] for r in requests] == ["0", "10", "37"]
    assert requests[0].url.params["limit"] == "10"
    assert requests[0].url.params["state"] == "MERGED"
    assert listing["has_more"] is False
    assert [i["number"] for i in listing["items"]] == [7]
    assert listing["items"][0]["state"] == "merged"


async def test_list_pull_requests_page_beyond_last_is_empty_and_bounded() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route({f"GET {REPO_PATH}/pull-requests": page([fx("pull_request")])}), requests
    )
    assert await tracker.list_pull_requests(page=4) == {"items": [], "has_more": False}
    assert len(requests) == 1
    with pytest.raises(TrackerResponseError, match="pagination limit"):
        await tracker.list_pull_requests(page=10_000)


async def test_get_pull_request_and_object_attributes() -> None:
    tracker = make_tracker(route({f"GET {PR_PATH}": fx("pull_request")}), [])
    pr = await tracker.get_pull_request(101)
    attributes = tracker.object_attributes(pr)
    assert attributes["source_branch"] == "feature/retry"
    assert attributes["target_branch"] == "main"
    assert attributes["state"] == "open"
    assert attributes["last_commit"] == {
        "id": "04c7c5c931b9418ca7b66f51fe934d0bd9b2ba4b"
    }
    assert attributes["repository"]["repository_id"] == 42
    assert (
        attributes["url"] == f"{INSTANCE}/projects/PRJ/repos/my-repo/pull-requests/101"
    )
    assert tracker.pull_request_url(101) == attributes["url"]


def test_object_attributes_drops_links_to_other_hosts() -> None:
    pr = fx("pull_request")
    pr["links"]["self"][0]["href"] = (
        "https://evil.example.net/projects/PRJ/repos/my-repo/pull-requests/101"
    )
    attributes = dc.build_object_attributes(pr, dc.parse_instance_url(INSTANCE))
    assert (
        attributes["url"] == f"{INSTANCE}/projects/PRJ/repos/my-repo/pull-requests/101"
    )


async def test_create_pull_request_sends_full_refs_and_repository_objects() -> None:
    requests: List[httpx.Request] = []
    created = fx("pull_request")
    created["draft"] = True
    tracker = make_tracker(
        route({f"POST {REPO_PATH}/pull-requests": lambda r: ok(created, 201)}), requests
    )
    result = await tracker.create_pull_request(
        "Add retry to sync",
        "feature/retry",
        "main",
        description="Line one\n\nLine two",
        draft=True,
        reviewers=["rev"],
    )
    body = json.loads(requests[0].content)
    assert body["fromRef"] == {
        "id": "refs/heads/feature/retry",
        "repository": {"id": 42, "slug": "my-repo", "project": {"key": "PRJ"}},
    }
    assert body["toRef"] == {
        "id": "refs/heads/main",
        "repository": {"id": 42, "slug": "my-repo", "project": {"key": "PRJ"}},
    }
    assert body["draft"] is True
    assert body["reviewers"] == [{"user": {"name": "rev"}}]
    assert (
        "labels" not in body
        and "close_source_branch" not in body
        and "closeSourceBranch" not in body
    )
    assert result["number"] == 101 and result["is_draft"] is True
    assert (
        result["source_branch"] == "feature/retry" and result["target_branch"] == "main"
    )
    assert result["version"] == 3


@pytest.mark.parametrize(
    "extra",
    [
        {"labels": ["x"]},
        {"assignees": ["jdoe"]},
        {"milestone": "m1"},
        {"close_source_branch": True},
    ],
)
async def test_create_pull_request_refuses_unsupported_options_before_request(
    extra: Dict[str, Any],
) -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(lambda r: ok(fx("pull_request"), 201), requests)
    with pytest.raises(BitbucketDCUnsupportedOperationError, match="does not support"):
        await tracker.create_pull_request("t", "a", "b", **extra)
    assert requests == []


async def test_create_pull_request_rejects_response_for_other_repository() -> None:
    created = fx("pull_request")
    created["toRef"]["repository"]["slug"] = "other"
    tracker = make_tracker(
        route({f"POST {REPO_PATH}/pull-requests": lambda r: ok(created, 201)}), []
    )
    with pytest.raises(dc.BitbucketDCIdentityError):
        await tracker.create_pull_request("t", "a", "b")


async def test_update_pull_request_sends_version_and_retries_once_on_409() -> None:
    requests: List[httpx.Request] = []
    state = {"version": 3, "puts": 0}

    def get_pr(request: httpx.Request) -> httpx.Response:
        pr = fx("pull_request")
        pr["version"] = state["version"]
        return ok(pr)

    def put_pr(request: httpx.Request) -> httpx.Response:
        state["puts"] += 1
        body = json.loads(request.content)
        if body["version"] != 4:
            state["version"] = 4  # someone else bumped the version meanwhile
            return ok(fx("error_409_version"), 409)
        pr = fx("pull_request")
        pr.update({"version": 5, "title": body["title"]})
        return ok(pr)

    tracker = make_tracker(
        route({f"GET {PR_PATH}": get_pr, f"PUT {PR_PATH}": put_pr}), requests
    )
    updated = await tracker.update_pull_request(101, title="New title")
    assert updated["title"] == "New title" and updated["version"] == 5
    puts = [json.loads(r.content) for r in requests if r.method == "PUT"]
    assert [p["version"] for p in puts] == [3, 4]
    assert all("description" not in p for p in puts)


async def test_update_pull_request_conflict_when_same_field_changed_concurrently() -> (
    None
):
    requests: List[httpx.Request] = []
    reads = {"n": 0}

    def get_pr(request: httpx.Request) -> httpx.Response:
        reads["n"] += 1
        pr = fx("pull_request")
        if reads["n"] > 1:
            pr.update({"version": 4, "title": "Someone else's title"})
        return ok(pr)

    tracker = make_tracker(
        route(
            {
                f"GET {PR_PATH}": get_pr,
                f"PUT {PR_PATH}": lambda r: ok(fx("error_409_version"), 409),
            }
        ),
        requests,
    )
    with pytest.raises(
        BitbucketDCConflictError, match="changed concurrently \\(title\\)"
    ):
        await tracker.update_pull_request(101, title="Mine")
    assert sum(1 for r in requests if r.method == "PUT") == 1


async def test_update_pull_request_second_409_is_returned_not_retried_again() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route(
            {
                f"GET {PR_PATH}": fx("pull_request"),
                f"PUT {PR_PATH}": lambda r: ok(fx("error_409_version"), 409),
            }
        ),
        requests,
    )
    with pytest.raises(BitbucketDCConflictError):
        await tracker.update_pull_request(101, description="d")
    assert sum(1 for r in requests if r.method == "PUT") == 2


async def test_update_pull_request_refuses_closed() -> None:
    merged = fx("pull_request")
    merged["state"] = "MERGED"
    tracker = make_tracker(route({f"GET {PR_PATH}": merged}), [])
    with pytest.raises(BitbucketDCConflictError, match="MERGED"):
        await tracker.update_pull_request(101, title="x")


async def test_update_pull_request_without_edits_reads_only() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(route({f"GET {PR_PATH}": fx("pull_request")}), requests)
    await tracker.update_pull_request(101)
    assert [r.method for r in requests] == ["GET"]


async def test_diff_changes_and_commits() -> None:
    requests: List[httpx.Request] = []
    move = fx("change_modify")
    move.update(
        {
            "type": "MOVE",
            "srcPath": {"toString": "src/old.py"},
            "path": {"toString": "src/new.py"},
        }
    )
    tracker = make_tracker(
        route(
            {
                f"GET {PR_PATH}.diff": lambda r: httpx.Response(
                    200, text="diff --git a/x b/x\n"
                ),
                f"GET {PR_PATH}/changes": page([fx("change_modify"), move]),
                f"GET {PR_PATH}/commits": page(
                    [
                        {
                            "id": "04c7c5c931b9418ca7b66f51fe934d0bd9b2ba4b",
                            "displayId": "04c7c5c",
                        }
                    ]
                ),
            }
        ),
        requests,
    )
    assert (await tracker.get_pull_request_diff(101)).startswith("diff --git")
    assert requests[0].headers["Accept"] == "text/plain"
    stat = await tracker.get_pull_request_diffstat(101)
    assert stat[0] == {
        "path": "src/sync.py",
        "old_path": None,
        "status": "modified",
        "type": "MODIFY",
        "lines_added": None,
        "lines_removed": None,
    }
    assert stat[1]["status"] == "renamed" and stat[1]["old_path"] == "src/old.py"
    commits = await tracker.get_pull_request_commits(101)
    assert commits[0]["displayId"] == "04c7c5c"


# ----------------------------------------------------------------------
# Comments, replies, anchors
# ----------------------------------------------------------------------


async def test_comments_from_activities_flatten_replies_and_mark_outdated() -> None:
    orphaned = fx("comment_inline")
    orphaned.update({"id": 510, "comments": []})
    orphaned["anchor"]["orphaned"] = True
    orphaned["anchor"]["line"] = 3
    orphaned["anchor"]["lineType"] = "REMOVED"
    orphaned["anchor"]["fileType"] = "FROM"
    activities = [
        {
            "id": 1,
            "action": "COMMENTED",
            "commentAction": "ADDED",
            "comment": fx("comment_general"),
        },
        {
            "id": 2,
            "action": "COMMENTED",
            "commentAction": "ADDED",
            "comment": fx("comment_inline"),
            "commentAnchor": fx("comment_inline")["anchor"],
        },
        {
            "id": 3,
            "action": "COMMENTED",
            "commentAction": "ADDED",
            "comment": orphaned,
            "commentAnchor": orphaned["anchor"],
        },
        {"id": 4, "action": "APPROVED", "user": {"slug": "rev"}},
        {"id": 5, "action": "RESCOPED", "added": {}, "removed": {}},
    ]
    tracker = make_tracker(route({f"GET {PR_PATH}/activities": page(activities)}), [])
    comments = await tracker.get_pull_request_comments(101)
    by_id = {c["id"]: c for c in comments}
    assert set(by_id) == {501, 502, 503, 510}
    assert by_id[501]["type"] == "issue_comment" and by_id[501]["thread_id"] == 501
    inline = by_id[502]
    assert inline["type"] == "review_comment"
    assert (
        inline["path"] == "src/sync.py"
        and inline["line"] == 12
        and inline["side"] == "RIGHT"
    )
    assert inline["outdated"] is False and inline["version"] == 1
    reply = by_id[503]
    assert reply["in_reply_to_id"] == 502 and reply["thread_id"] == 502
    assert reply["path"] == "src/sync.py"
    assert reply["body"] == "Done in the latest push."
    old = by_id[510]
    assert (
        old["outdated"] is True
        and old["side"] == "LEFT"
        and old["old_line"] == 3
        and old["line"] is None
    )


async def test_add_general_multiline_comment() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route({f"POST {PR_PATH}/comments": lambda r: ok(fx("comment_general"), 201)}),
        requests,
    )
    created = await tracker.add_pull_request_comment(101, "First line\n\nSecond line")
    assert json.loads(requests[0].content) == {"text": "First line\n\nSecond line"}
    assert created["id"] == 501


async def test_add_inline_comment_effective_anchor() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route({f"POST {PR_PATH}/comments": lambda r: ok(fx("comment_inline"), 201)}),
        requests,
    )
    await tracker.add_pull_request_comment(
        101, "Handle the timeout here.", path="src/sync.py", line=12
    )
    body = json.loads(requests[0].content)
    assert body["anchor"] == {
        "diffType": "EFFECTIVE",
        "path": "src/sync.py",
        "srcPath": "src/sync.py",
        "line": 12,
        "fileType": "TO",
        "lineType": "ADDED",
    }


async def test_add_inline_comment_on_removed_line_with_commit_hashes_and_range() -> (
    None
):
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route({f"POST {PR_PATH}/comments": lambda r: ok(fx("comment_inline"), 201)}),
        requests,
    )
    await tracker.add_pull_request_comment(
        101,
        "Removed",
        path="src/sync.py",
        old_line=9,
        start_line=9,
        diff_type="COMMIT",
        from_hash="6df3858eeb9a53a911cd17e66a9174d44ffb02cd",
        to_hash="04c7c5c931b9418ca7b66f51fe934d0bd9b2ba4b",
    )
    anchor = json.loads(requests[0].content)["anchor"]
    assert (
        anchor["fileType"] == "FROM"
        and anchor["lineType"] == "REMOVED"
        and anchor["line"] == 9
    )
    assert anchor["diffType"] == "COMMIT"
    assert anchor["fromHash"] == "6df3858eeb9a53a911cd17e66a9174d44ffb02cd"
    assert "multilineMarker" not in anchor


async def test_ranged_comment_is_explicitly_unsupported() -> None:
    # ``multilineMarker`` is read-only in the 10.2 schema; a real range is
    # refused instead of being silently collapsed to a single line.
    requests: List[httpx.Request] = []
    tracker = make_tracker(lambda r: ok(), requests)
    with pytest.raises(NotImplementedError, match="multilineMarker"):
        await tracker.add_pull_request_comment(101, "x", path="f", line=9, start_line=7)
    assert requests == []


async def test_add_reply_uses_parent_and_ignores_anchor() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route(
            {
                f"POST {PR_PATH}/comments": lambda r: ok(
                    fx("comment_inline")["comments"][0], 201
                )
            }
        ),
        requests,
    )
    await tracker.add_pull_request_comment(
        101, "A measured reply.", parent_id=502, path="src/sync.py", line=12
    )
    body = json.loads(requests[0].content)
    assert body == {"text": "A measured reply.", "parent": {"id": 502}}


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"body": ""}, "empty"),
        ({"body": "x", "line": 3}, "needs a file path"),
        ({"body": "x", "path": "f", "line": 3, "old_line": 2}, "either line"),
        (
            {"body": "x", "path": "f", "line": 3, "diff_type": "COMMIT"},
            "fromHash and toHash",
        ),
        (
            {"body": "x", "path": "f", "line": 3, "from_hash": "a"},
            "fromHash and toHash",
        ),
        (
            {"body": "x", "path": "f", "line": 3, "diff_type": "WEIRD"},
            "Unknown diff type",
        ),
    ],
)
async def test_add_comment_validates_anchor(
    kwargs: Dict[str, Any], message: str
) -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(lambda r: ok(), requests)
    with pytest.raises(ValueError, match=message):
        await tracker.add_pull_request_comment(101, **kwargs)
    assert requests == []


async def test_update_comment_sends_version_and_retries_once() -> None:
    requests: List[httpx.Request] = []
    state = {"version": 1}

    def get_comment(request: httpx.Request) -> httpx.Response:
        comment = fx("comment_inline")
        comment["version"] = state["version"]
        return ok(comment)

    def put_comment(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["version"] != 2:
            state["version"] = 2
            return ok(fx("error_409_version"), 409)
        comment = fx("comment_inline")
        comment.update({"version": 3, "text": body["text"]})
        return ok(comment)

    tracker = make_tracker(
        route(
            {
                f"GET {PR_PATH}/comments/502": get_comment,
                f"PUT {PR_PATH}/comments/502": put_comment,
            }
        ),
        requests,
    )
    updated = await tracker.update_pull_request_comment(101, 502, "Edited")
    assert updated["text"] == "Edited" and updated["version"] == 3
    puts = [json.loads(r.content) for r in requests if r.method == "PUT"]
    assert puts == [{"version": 1, "text": "Edited"}, {"version": 2, "text": "Edited"}]


async def test_update_comment_conflict_when_text_changed_by_someone_else() -> None:
    reads = {"n": 0}

    def get_comment(request: httpx.Request) -> httpx.Response:
        reads["n"] += 1
        comment = fx("comment_inline")
        if reads["n"] > 1:
            comment.update({"version": 2, "text": "Changed elsewhere"})
        return ok(comment)

    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route(
            {
                f"GET {PR_PATH}/comments/502": get_comment,
                f"PUT {PR_PATH}/comments/502": lambda r: ok(
                    fx("error_409_version"), 409
                ),
            }
        ),
        requests,
    )
    with pytest.raises(BitbucketDCConflictError, match="changed concurrently"):
        await tracker.update_pull_request_comment(101, 502, "Mine")
    assert sum(1 for r in requests if r.method == "PUT") == 1


async def test_update_comment_already_applied_returns_current_without_second_put() -> (
    None
):
    reads = {"n": 0}

    def get_comment(request: httpx.Request) -> httpx.Response:
        reads["n"] += 1
        comment = fx("comment_inline")
        if reads["n"] > 1:
            # Concurrent edit changed only the resolution flag we are setting.
            comment.update({"version": 2, "threadResolved": True})
        return ok(comment)

    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route(
            {
                f"GET {PR_PATH}/comments/502": get_comment,
                f"PUT {PR_PATH}/comments/502": lambda r: ok(
                    fx("error_409_version"), 409
                ),
            }
        ),
        requests,
    )
    assert await tracker.set_comment_resolved(101, 502, True) is True
    assert sum(1 for r in requests if r.method == "PUT") == 1


async def test_resolve_thread_and_task_use_different_fields() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route(
            {
                f"GET {PR_PATH}/comments/502": fx("comment_inline"),
                f"PUT {PR_PATH}/comments/502": fx("comment_inline"),
                f"GET {PR_PATH}/comments/601": fx("task"),
                f"PUT {PR_PATH}/comments/601": fx("task"),
            }
        ),
        requests,
    )
    await tracker.set_comment_resolved(101, 502, True)
    await tracker.resolve_pull_request_task(101, 601, True)
    await tracker.resolve_pull_request_task(101, 601, False)
    puts = [json.loads(r.content) for r in requests if r.method == "PUT"]
    assert puts == [
        {"version": 1, "threadResolved": True},
        {"version": 0, "state": "RESOLVED"},
        {"version": 0, "state": "OPEN"},
    ]


async def test_delete_comment_sends_version_and_handles_replies_conflict() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route(
            {
                f"GET {PR_PATH}/comments/501": fx("comment_general"),
                f"DELETE {PR_PATH}/comments/501": lambda r: ok(status=204),
            }
        ),
        requests,
    )
    assert await tracker.delete_pull_request_comment(101, 501) is True
    assert requests[1].url.params["version"] == "0"

    with_replies = fx("comment_inline")
    tracker = make_tracker(
        route(
            {
                f"GET {PR_PATH}/comments/502": with_replies,
                f"DELETE {PR_PATH}/comments/502": lambda r: ok(
                    fx("error_409_version"), 409
                ),
            }
        ),
        [],
    )
    with pytest.raises(BitbucketDCConflictError, match="has replies"):
        await tracker.delete_pull_request_comment(101, 502)


# ----------------------------------------------------------------------
# Reviewer verdicts and tasks
# ----------------------------------------------------------------------


def participant_echo(request: httpx.Request) -> httpx.Response:
    """Answer ``participants/{userSlug}`` with the requested status echoed back."""
    participant = fx("participant_approved")
    status = json.loads(request.content)["status"]
    participant.update({"status": status, "approved": status == "APPROVED"})
    return ok(participant)


@pytest.mark.parametrize(
    "call, status",
    [
        (lambda t: t.set_approval(101, True), "APPROVED"),
        (lambda t: t.set_approval(101, False), "UNAPPROVED"),
        (lambda t: t.set_changes_requested(101, True), "NEEDS_WORK"),
        (lambda t: t.set_changes_requested(101, False), "UNAPPROVED"),
    ],
)
async def test_verdicts_use_participants_resource(
    call: Callable[[BitbucketDCTracker], Any], status: str
) -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route({f"PUT {PR_PATH}/participants/rev": participant_echo}), requests
    )
    assert await call(tracker) is True
    assert requests[0].method == "PUT"
    assert requests[0].url.path == f"{PR_PATH}/participants/rev"
    assert json.loads(requests[0].content) == {"status": status}


async def test_verdict_not_echoed_by_server_is_an_error() -> None:
    # A 200 whose body does not carry the requested status is not success.
    tracker = make_tracker(
        route({f"PUT {PR_PATH}/participants/rev": fx("participant_approved")}), []
    )
    with pytest.raises(TrackerResponseError, match="Malformed reviewer verdict"):
        await tracker.set_changes_requested(101, True)


async def test_verdict_learns_user_slug_from_response_header() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route(
            {
                f"GET {PR_PATH}": lambda r: ok(
                    fx("pull_request"), headers={"X-AUSERNAME": "learned"}
                ),
                f"PUT {PR_PATH}/participants/learned": participant_echo,
            }
        ),
        requests,
        username=None,
    )
    assert tracker.current_user is None
    await tracker.set_changes_requested(101, True)
    assert tracker.current_user == "learned"
    assert [r.url.path for r in requests] == [
        PR_PATH,
        f"{PR_PATH}/participants/learned",
    ]


async def test_verdict_without_known_user_is_an_explicit_error() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route({f"GET {PR_PATH}": fx("pull_request")}), requests, username=None
    )
    with pytest.raises(dc.BitbucketDCConfigError, match="user slug is unknown"):
        await tracker.set_approval(101, True)
    assert all(r.method == "GET" for r in requests)


async def test_verdict_on_closed_pull_request_is_a_conflict() -> None:
    tracker = make_tracker(
        route(
            {
                f"PUT {PR_PATH}/participants/rev": lambda r: ok(
                    {"errors": [{"message": "The pull request is not open"}]}, 409
                )
            }
        ),
        [],
    )
    with pytest.raises(BitbucketDCConflictError, match="not open"):
        await tracker.set_approval(101, True)


async def test_participant_status_rejects_unknown_value() -> None:
    tracker = make_tracker(lambda r: ok(), [])
    with pytest.raises(ValueError, match="Unknown participant status"):
        await tracker.set_participant_status(101, "MERGE")


async def test_tasks_create_list_and_permission_skip() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route(
            {
                f"POST {PR_PATH}/blocker-comments": lambda r: ok(fx("task"), 201),
                f"GET {PR_PATH}/blocker-comments": page([fx("task")]),
            }
        ),
        requests,
    )
    task = await tracker.create_pull_request_task(
        101, "Add a unit test for the retry.", comment_id=502
    )
    assert task is not None and task["severity"] == "BLOCKER"
    assert json.loads(requests[0].content) == {
        "text": "Add a unit test for the retry.",
        "parent": {"id": 502},
        "severity": "BLOCKER",
    }
    tasks = await tracker.list_pull_request_tasks(101, state="open")
    assert tasks[0]["task"] is True and tasks[0]["resolved"] is False
    assert requests[1].url.params["state"] == "OPEN"

    denied = make_tracker(lambda r: ok(fx("error_403"), 403), [])
    assert await denied.create_pull_request_task(101, "x") is None


def test_normalize_resolved_task() -> None:
    task = fx("task")
    task["state"] = "RESOLVED"
    normalized = BitbucketDCTracker.normalize_comment(task)
    assert normalized["task"] is True and normalized["resolved"] is True
    assert normalized["created_at"].startswith("2025-")


# ----------------------------------------------------------------------
# Build status
# ----------------------------------------------------------------------


async def test_create_commit_status_posts_builds_resource() -> None:
    requests: List[httpx.Request] = []
    sha = "04c7c5c931b9418ca7b66f51fe934d0bd9b2ba4b"
    tracker = make_tracker(
        route({f"POST {REPO_PATH}/commits/{sha}/builds": lambda r: ok(status=204)}),
        requests,
    )
    result = await tracker.create_commit_status(
        sha,
        "pending",
        context="preloop/review",
        description="Reviewing",
        refname="feature/retry",
    )
    body = json.loads(requests[0].content)
    assert body == {
        "key": "preloop/review",
        "name": "preloop/review",
        "state": "INPROGRESS",
        "url": f"{INSTANCE}/projects/PRJ/repos/my-repo/browse",
        "description": "Reviewing",
        "ref": "refs/heads/feature/retry",
    }
    assert result["state"] == "INPROGRESS" and result["key"] == "preloop/review"

    await tracker.create_commit_status(
        sha, "success", target_url="https://preloop.example.com/run/1"
    )
    assert json.loads(requests[1].content)["url"] == "https://preloop.example.com/run/1"
    assert json.loads(requests[1].content)["state"] == "SUCCESSFUL"

    with pytest.raises(ValueError, match="Unsupported commit status"):
        await tracker.create_commit_status(sha, "weird")
    with pytest.raises(ValueError, match="Invalid commit hash"):
        await tracker.create_commit_status("../x", "success")


async def test_get_commit_status() -> None:
    sha = "04c7c5c931b9418ca7b66f51fe934d0bd9b2ba4b"
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        route(
            {
                f"GET {REPO_PATH}/commits/{sha}/builds": {
                    "key": "preloop",
                    "state": "SUCCESSFUL",
                }
            }
        ),
        requests,
    )
    assert (await tracker.get_commit_status(sha, "preloop"))["state"] == "SUCCESSFUL"
    assert requests[0].url.params["key"] == "preloop"
    tracker = make_tracker(lambda r: ok({}, 404), [])
    assert await tracker.get_commit_status(sha, "preloop") is None


# ----------------------------------------------------------------------
# Regression: Cloud tracker untouched and factory wiring
# ----------------------------------------------------------------------


def test_cloud_tracker_keeps_its_own_host_checks() -> None:
    from preloop.sync.trackers.bitbucket import BitbucketTracker
    from preloop.utils.bitbucket import BITBUCKET_API_BASE_URL

    cloud = BitbucketTracker("c", "tok", {"workspace": "ws", "repository": "repo"})
    assert cloud.tracker_type == "bitbucket"
    assert cloud.api_base_url == BITBUCKET_API_BASE_URL
    with pytest.raises(TrackerResponseError, match="another host"):
        cloud._url("https://bitbucket.example.com/bitbucket/rest/api/1.0/projects")
    assert not isinstance(cloud, BitbucketDCTracker)


async def test_factory_builds_dc_tracker() -> None:
    from preloop.sync.trackers.factory import TRACKER_CLASSES, create_tracker_client

    assert TRACKER_CLASSES["bitbucket_dc"] is BitbucketDCTracker
    assert TRACKER_CLASSES["bitbucket"] is not BitbucketDCTracker
    client = await create_tracker_client(
        "bitbucket_dc",
        "tracker-9",
        "pat",
        {"instance_url": INSTANCE, "project_key": "PRJ"},
    )
    assert isinstance(client, BitbucketDCTracker)
    assert client.instance_url == INSTANCE
    assert client.repo_full_name is None


def test_payload_helpers() -> None:
    assert dc.full_ref("main") == "refs/heads/main"
    assert dc.full_ref("refs/tags/v1") == "refs/tags/v1"
    assert dc.ref_branch({"id": "refs/heads/x/y"}) == "x/y"
    assert dc.path_text({"components": ["a", "b.py"]}) == "a/b.py"
    assert dc.path_text({"parent": "a", "name": "b.py"}) == "a/b.py"
    assert dc.path_text("plain") == "plain"
    assert dc.user_name({"slug": "s", "name": "n"}) == "s"
    assert dc.repository_identity(fx("repository")) == {
        "repository_id": 42,
        "repository_slug": "my-repo",
        "project_key": "PRJ",
        "project_id": 7,
    }
    assert dc.repository_identity({"slug": "x"}) is None
    allowed = [ipaddress.ip_network("203.0.113.0/24")]
    assert dc.check_destination_address(
        "203.0.113.1", allowed_private=allowed
    ) == ipaddress.ip_address("203.0.113.1")
    assert parse_qs("a=1") == {"a": ["1"]}
