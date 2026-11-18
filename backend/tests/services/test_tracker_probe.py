"""Target validation, pinning and bounds of tracker connection tests."""

from __future__ import annotations

import asyncio
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Generator, List
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import requests

from preloop.services import tracker_probe
from preloop.services.tracker_probe import (
    ProbePolicy,
    TrackerProbeBusyError,
    TrackerProbeTimeoutError,
    TrackerTargetRefusedError,
    run_tracker_probe,
    validate_target_url,
)

REFUSED_ALWAYS = [
    "169.254.169.254",  # cloud metadata
    "169.254.10.1",  # IPv4 link-local
    "fe80::1",  # IPv6 link-local
    "fd00:ec2::254",  # IPv6 metadata
    "0.0.0.0",
    "::",
    "224.0.0.1",
    "::ffff:169.254.169.254",
]
REFUSED_STRICT_ONLY = [
    "127.0.0.1",
    "::1",
    "10.1.2.3",
    "172.16.0.1",
    "192.168.1.1",
    "fd12:3456::1",  # unique-local
    "100.64.0.1",  # shared address space
    "240.0.0.1",  # reserved
    "::ffff:10.0.0.1",
]


def _fake_resolver(answers: Dict[str, List[str]]) -> Any:
    """getaddrinfo that answers names from ``answers`` and numerics for real."""
    real = tracker_probe._SYSTEM_GETADDRINFO
    calls: Dict[str, int] = {}

    def resolve(host: Any, port: Any, *args: Any, **kwargs: Any) -> List[Any]:
        name = str(host)
        if name in answers:
            calls[name] = calls.get(name, 0) + 1
            return [
                (
                    socket.AF_INET6 if ":" in addr else socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    (addr, port or 0),
                )
                for addr in answers[name]
            ]
        return real(host, port, *args, **kwargs)

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


@pytest.mark.parametrize("address", REFUSED_ALWAYS)
@pytest.mark.parametrize("strict", [False, True])
def test_metadata_link_local_unspecified_multicast_refused_in_both_modes(
    address: str, strict: bool
) -> None:
    """These ranges are never a tracker, so the permissive mode refuses them."""
    with pytest.raises(TrackerTargetRefusedError):
        ProbePolicy(strict=strict).addresses_for(address)


@pytest.mark.parametrize("address", REFUSED_STRICT_ONLY)
def test_private_ranges_refused_only_when_strict(address: str) -> None:
    """Loopback, private, unique-local and reserved space: strict refuses."""
    with pytest.raises(TrackerTargetRefusedError):
        ProbePolicy(strict=True).addresses_for(address)
    assert ProbePolicy(strict=False).addresses_for(address)


def test_public_address_allowed_when_strict() -> None:
    """A global address passes the strict policy."""
    assert ProbePolicy(strict=True).addresses_for("93.184.216.34") == ["93.184.216.34"]


@pytest.mark.parametrize(
    "host", ["postgres", "localhost", "api.preloop.svc", "db.cluster.local"]
)
def test_cluster_service_names_refused_when_strict(host: str) -> None:
    """In-cluster names are refused before any resolution."""
    with pytest.raises(TrackerTargetRefusedError):
        ProbePolicy(strict=True).addresses_for(host)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://gitlab.example.com",
        "file:///etc/passwd",
        "gopher://gitlab.example.com",
        "https://user:pass@gitlab.example.com",
        "https://",
    ],
)
def test_url_shape_refused_in_both_modes(url: str) -> None:
    """Only plain http(s) URLs with a host and no credentials are accepted."""
    with pytest.raises(TrackerTargetRefusedError):
        validate_target_url(url, ProbePolicy(strict=False))


def test_mixed_dns_answer_refused_when_any_address_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One internal record in the answer refuses the whole name."""
    monkeypatch.setattr(
        tracker_probe,
        "_SYSTEM_GETADDRINFO",
        _fake_resolver({"mixed.example.com": ["93.184.216.34", "10.0.0.5"]}),
    )
    with pytest.raises(TrackerTargetRefusedError):
        ProbePolicy(strict=True).addresses_for("mixed.example.com")
    # Permissive keeps self-hosted private hosts working ...
    assert ProbePolicy(strict=False).addresses_for("mixed.example.com")


def test_mixed_dns_answer_with_metadata_refused_when_permissive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """... but not a name that also points at the metadata service."""
    monkeypatch.setattr(
        tracker_probe,
        "_SYSTEM_GETADDRINFO",
        _fake_resolver({"meta.example.com": ["93.184.216.34", "169.254.169.254"]}),
    )
    with pytest.raises(TrackerTargetRefusedError):
        ProbePolicy(strict=False).addresses_for("meta.example.com")


def test_answer_is_pinned_so_a_second_lookup_cannot_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first checked answer is reused for every later connection."""
    answers = {"rebind.example.com": ["93.184.216.34"]}
    resolver = _fake_resolver(answers)
    monkeypatch.setattr(tracker_probe, "_SYSTEM_GETADDRINFO", resolver)
    policy = ProbePolicy(strict=True)
    assert policy.addresses_for("rebind.example.com") == ["93.184.216.34"]
    answers["rebind.example.com"] = ["10.0.0.5"]
    assert policy.addresses_for("rebind.example.com") == ["93.184.216.34"]
    assert resolver.calls["rebind.example.com"] == 1


def test_guarded_getaddrinfo_returns_only_pinned_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Library code resolving inside a probe sees only the checked address."""
    answers = {"pin.example.com": ["93.184.216.34"]}
    monkeypatch.setattr(tracker_probe, "_SYSTEM_GETADDRINFO", _fake_resolver(answers))
    policy = ProbePolicy(strict=True)
    tracker_probe._enter_probe_thread(policy)
    try:
        first = tracker_probe._guarded_getaddrinfo("pin.example.com", 443)
        answers["pin.example.com"] = ["10.0.0.5"]
        second = tracker_probe._guarded_getaddrinfo("pin.example.com", 443)
    finally:
        tracker_probe._state.policy = None
    assert {info[4][0] for info in first + second} == {"93.184.216.34"}


def test_guard_is_inert_outside_probe_threads() -> None:
    """Every other caller in the process gets the system resolver."""
    tracker_probe.install_resolution_guard()
    assert tracker_probe.current_policy() is None
    assert socket.getaddrinfo("127.0.0.1", 80)


class _RedirectHandler(BaseHTTPRequestHandler):
    target = "http://169.254.169.254/latest/meta-data/"

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self.send_response(302)
        self.send_header("Location", self.target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        return None


@pytest.fixture
def redirect_server() -> Generator[str, None, None]:
    """A local server that redirects every request to the metadata address."""
    server = HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def test_sync_redirect_to_refused_target_is_not_followed(redirect_server: str) -> None:
    """A requests-based client (GitLab, Jira) cannot be redirected inward."""

    async def work() -> Any:
        return await asyncio.to_thread(
            requests.get, redirect_server, timeout=tracker_probe.probe_timeout()
        )

    with pytest.raises(TrackerTargetRefusedError):
        asyncio.run(run_tracker_probe(work, url=redirect_server))


def test_async_redirect_to_refused_literal_is_not_followed(
    redirect_server: str,
) -> None:
    """An httpx client following redirects cannot reach a refused literal."""

    async def work() -> Any:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            return await client.get(redirect_server)

    with pytest.raises(TrackerTargetRefusedError):
        asyncio.run(run_tracker_probe(work, url=redirect_server))


def test_permissive_setting_allows_private_self_hosted_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (off): a loopback or private tracker URL is still tested."""
    monkeypatch.setattr(tracker_probe.settings, "tracker_block_private_targets", False)

    async def work() -> str:
        return "reached"

    assert asyncio.run(run_tracker_probe(work, url="http://10.0.0.5")) == "reached"


def test_strict_setting_refuses_private_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On: the URL is refused before the client is even built."""
    monkeypatch.setattr(tracker_probe.settings, "tracker_block_private_targets", True)
    built = threading.Event()

    async def work() -> None:
        built.set()

    with pytest.raises(TrackerTargetRefusedError):
        asyncio.run(run_tracker_probe(work, url="http://10.0.0.5"))
    assert not built.is_set()


def test_probe_deadline_bounds_a_stuck_coroutine() -> None:
    """The overall deadline ends the test with a timeout refusal."""

    async def work() -> None:
        await asyncio.sleep(30)

    with pytest.raises(TrackerProbeTimeoutError):
        asyncio.run(run_tracker_probe(work, url=None, deadline=0.2))


def test_probe_runs_off_the_callers_loop_and_threads() -> None:
    """Work never runs on the API loop or its default executor."""
    caller = threading.get_ident()

    async def work() -> int:
        return threading.get_ident()

    assert asyncio.run(run_tracker_probe(work, url=None)) != caller


def test_busy_when_every_slot_is_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe beyond the limit is refused at once instead of queueing."""
    monkeypatch.setattr(tracker_probe, "_slots", threading.BoundedSemaphore(1))
    release = threading.Event()

    async def blocker() -> None:
        await asyncio.to_thread(release.wait, 5)

    async def scenario() -> None:
        first = asyncio.ensure_future(run_tracker_probe(blocker, url=None))
        await asyncio.sleep(0.1)
        try:
            with pytest.raises(TrackerProbeBusyError):
                await run_tracker_probe(AsyncMock(), url=None)
        finally:
            release.set()
            await first

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Endpoint mapping
# ---------------------------------------------------------------------------

TEST_PATH = "/api/v1/trackers/test-and-list-orgs"


@pytest.fixture(autouse=True)
def mock_event_bus_connect() -> Generator[None, None, None]:
    """Keep the app from dialling NATS."""
    with patch(
        "preloop.sync.services.event_bus.EventBus.connect", new_callable=AsyncMock
    ):
        yield


def test_endpoint_refuses_metadata_url_with_400(client: Any) -> None:
    """The refusal is a plain 400 and no client is built."""
    with patch(
        "preloop.api.endpoints.trackers.create_tracker_client", new=AsyncMock()
    ) as factory:
        response = client.post(
            TEST_PATH,
            json={
                "tracker_type": "gitlab",
                "api_key": "glpat-example",
                "url": "http://169.254.169.254/",
            },
        )
    assert response.status_code == 400
    assert "not allowed" in response.json()["detail"]
    factory.assert_not_called()


@pytest.mark.parametrize(
    "path,body",
    [
        (TEST_PATH, {"tracker_type": "gitlab", "api_key": "x", "url": "ftp://h"}),
        (
            "/api/v1/trackers/list-projects-for-org",
            {
                "tracker_type": "gitlab",
                "api_key": "x",
                "url": "http://169.254.169.254/",
                "organization_identifier": "1",
            },
        ),
        (
            "/api/v1/trackers",
            {
                "name": "Example",
                "type": "gitlab",
                "api_key": "x",
                "url": "http://169.254.169.254/",
            },
        ),
    ],
)
def test_every_caller_input_endpoint_validates_the_target(
    client: Any, path: str, body: Dict[str, Any]
) -> None:
    """Test, list-projects and register all validate before connecting."""
    with patch(
        "preloop.api.endpoints.trackers.create_tracker_client", new=AsyncMock()
    ) as factory:
        response = client.post(path, json=body)
    assert response.status_code in (400, 422), response.text
    factory.assert_not_called()


def test_endpoint_busy_is_503_with_retry_after(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full slots answer fast with 503 and Retry-After."""
    monkeypatch.setattr(tracker_probe, "_slots", threading.BoundedSemaphore(1))
    assert tracker_probe._slots.acquire(blocking=False)
    response = client.post(
        TEST_PATH,
        json={"tracker_type": "gitlab", "api_key": "x", "url": "https://h.example"},
    )
    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(
        tracker_probe.PROBE_BUSY_RETRY_AFTER_S
    )


def test_endpoint_rate_limited_per_user(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared limiter caps connection tests per user."""
    monkeypatch.setenv("RATE_LIMIT_TRACKERS_CONNECTION_TEST", "2/60")
    body = {"tracker_type": "gitlab", "api_key": "x", "url": "ftp://h.example"}
    codes = [client.post(TEST_PATH, json=body).status_code for _ in range(3)]
    assert codes == [400, 400, 429]


def test_non_isolated_probe_still_validates_with_the_strict_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Managed and GitHub App probes keep target validation and the deadline."""
    monkeypatch.setattr(tracker_probe.settings, "tracker_block_private_targets", True)

    async def work() -> None:
        raise AssertionError("work must not run for a refused target")

    with pytest.raises(TrackerTargetRefusedError):
        asyncio.run(run_tracker_probe(work, url="http://10.0.0.5", isolate=False))

    async def slow() -> None:
        await asyncio.sleep(30)

    with pytest.raises(TrackerProbeTimeoutError):
        asyncio.run(run_tracker_probe(slow, url=None, isolate=False, deadline=0.2))


def test_connection_details_url_override_is_validated(client: Any) -> None:
    """The URL the client actually uses is the one that is validated."""
    with patch(
        "preloop.api.endpoints.trackers.create_tracker_client", new=AsyncMock()
    ) as factory:
        response = client.post(
            TEST_PATH,
            json={
                "tracker_type": "gitlab",
                "api_key": "x",
                "url": "https://gitlab.example.com",
                "connection_details": {"url": "http://169.254.169.254/"},
            },
        )
    assert response.status_code == 400, response.text
    factory.assert_not_called()


@pytest.mark.parametrize(
    "details,source,isolated",
    [
        ({"auth_type": "api_token"}, None, True),
        ({}, None, True),
        ({"auth_type": "github_app"}, None, False),
        ({"auth_type": "oauth_app"}, None, False),
        ({"auth_type": "api_token"}, object(), False),
    ],
)
def test_loop_bound_credentials_are_not_isolated(
    details: Dict[str, Any], source: Any, isolated: bool
) -> None:
    """Saved GitHub App / OAuth and managed trackers stay on the API loop."""
    from preloop.api.endpoints.trackers import _probe_isolated

    assert _probe_isolated(details, source) is isolated
