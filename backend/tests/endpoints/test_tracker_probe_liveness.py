"""A tracker connection test against an unresponsive host keeps the API live.

The API is served by a real uvicorn server on a background thread so a
blocked event loop shows up as a probe that does not answer, exactly as the
kubelet would see it. The tracker host is a local TCP listener that accepts
connections and never sends a byte.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Generator, List
from unittest.mock import AsyncMock, patch

import fastapi
import httpx
import pytest
import uvicorn

from preloop.services import tracker_probe

TEST_PATH = "/api/v1/trackers/test-and-list-orgs"
# The bound a caller may wait for one test, plus scheduling slack.
RESPONSE_BOUND_S = tracker_probe.PROBE_DEADLINE_S + 3.0


@pytest.fixture(autouse=True)
def mock_event_bus_connect() -> Generator[None, None, None]:
    """Keep the app from dialling NATS."""
    with patch(
        "preloop.sync.services.event_bus.EventBus.connect", new_callable=AsyncMock
    ):
        yield


class SilentServer:
    """Accepts TCP connections and never answers them."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.sock.settimeout(0.1)
        self.port = int(self.sock.getsockname()[1])
        self.accepted: List[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        """Base URL of the listener."""
        return f"http://127.0.0.1:{self.port}"

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.accepted.append(conn)

    def wait_for_connection(self, count: int = 1, timeout: float = 10.0) -> None:
        """Block until ``count`` connections were accepted."""
        deadline = time.monotonic() + timeout
        while len(self.accepted) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(self.accepted) >= count, "tracker client never connected"

    def close(self) -> None:
        """Stop accepting and drop every held connection."""
        self._stop.set()
        for conn in self.accepted:
            try:
                conn.close()
            except OSError:
                pass
        self.sock.close()
        self._thread.join(timeout=2)


@pytest.fixture
def silent_server() -> Generator[SilentServer, None, None]:
    """A tracker host that accepts and never answers."""
    server = SilentServer()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def live_api(
    app: fastapi.FastAPI, silent_server: SilentServer
) -> Generator[str, None, None]:
    """Serve the test app over real HTTP on a background thread."""
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    # Serve one authenticated round trip before a test starts its clock. The
    # first request on a cold app pays for auth, database and route warm-up,
    # which on a loaded runner took seconds and is not part of the probe bound.
    for path in ("/api/v1/ping", "/api/v1/trackers"):
        assert httpx.get(f"{base}{path}", timeout=30.0).status_code == 200
    try:
        yield base
    finally:
        # Drop the held connections first so a client stuck on them returns.
        silent_server.close()
        server.should_exit = True
        thread.join(timeout=15)


def _gitlab_test(base: str, target: str) -> httpx.Response:
    return httpx.post(
        f"{base}{TEST_PATH}",
        json={"tracker_type": "gitlab", "api_key": "glpat-example", "url": target},
        timeout=60.0,
    )


def _assert_live(base: str) -> None:
    ping = httpx.get(f"{base}/api/v1/ping", timeout=1.0)
    assert ping.status_code == 200
    listing = httpx.get(f"{base}/api/v1/trackers", timeout=1.0)
    assert listing.status_code == 200


def test_one_hanging_connection_test_keeps_ping_and_api_answering(
    live_api: str, silent_server: SilentServer
) -> None:
    """One test against a silent host must not block the event loop."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        started = time.monotonic()
        pending = pool.submit(_gitlab_test, live_api, silent_server.url)
        silent_server.wait_for_connection()
        _assert_live(live_api)
        response = pending.result(timeout=60)
        elapsed = time.monotonic() - started

    assert elapsed < RESPONSE_BOUND_S
    assert 400 <= response.status_code < 500
    assert "did not respond" in response.json()["detail"]


def test_many_hanging_connection_tests_are_bounded_and_keep_api_live(
    live_api: str, silent_server: SilentServer
) -> None:
    """More tests than the default executor has threads stay bounded."""
    count = min(32, (os.cpu_count() or 1) + 4) + 2
    with ThreadPoolExecutor(max_workers=count) as pool:
        started = time.monotonic()
        pending = [
            pool.submit(_gitlab_test, live_api, silent_server.url) for _ in range(count)
        ]
        silent_server.wait_for_connection()
        _assert_live(live_api)
        responses = [future.result(timeout=60) for future in pending]
        elapsed = time.monotonic() - started

    assert elapsed < RESPONSE_BOUND_S
    codes = [response.status_code for response in responses]
    assert all(400 <= code < 500 or code == 503 for code in codes), codes
    # Only the dedicated slots ever reached the host.
    assert len(silent_server.accepted) <= tracker_probe.PROBE_MAX_CONCURRENCY
    assert any(code in (429, 503) for code in codes), codes
    _assert_live(live_api)
