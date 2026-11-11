"""Real-nginx smoke test for console security headers and HSTS (#2278, #2279).

Runs the production nginx image (same digest as ``frontend/Dockerfile``) with
the repo's ``nginx.conf``, ``nginx.conf.template`` and ``security-headers.conf``
mounted where the Dockerfile puts them, a stub static tree, and a stub API
upstream that itself sends ``Strict-Transport-Security``. Requests go over
real HTTP.

Opt-in: needs Docker and image pulls, so it runs only when
``PRELOOP_NGINX_SMOKE=1``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

import pytest

from tests.test_nginx_security_headers import (
    DOCKER_SNIPPET,
    DOCKER_TEMPLATE,
    DOCKERFILE,
    REPO_ROOT,
)

pytestmark = pytest.mark.skipif(
    os.getenv("PRELOOP_NGINX_SMOKE") != "1" or shutil.which("docker") is None,
    reason="set PRELOOP_NGINX_SMOKE=1 with Docker available to run",
)

NGINX_CONF = REPO_ROOT / "frontend" / "docker" / "nginx.conf"
SECURITY = {
    "x-frame-options": "SAMEORIGIN",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": (
        "camera=(), geolocation=(), payment=(), usb=(), microphone=(self)"
    ),
    "content-security-policy": (
        "default-src 'self' http: https: data: blob: 'unsafe-inline'"
    ),
}
STUB_API_CONF = """
server {
    listen 80;
    location / {
        add_header Strict-Transport-Security "max-age=5" always;
        default_type application/json;
        return 200 '{"status":"ok"}';
    }
}
"""


def _nginx_image() -> str:
    match = re.search(r"^FROM (nginx:\S+)", DOCKERFILE.read_text(), re.MULTILINE)
    assert match
    return match.group(1)


def _docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _start_console(tmp: Path, network: str, name: str, env: Dict[str, str]) -> int:
    html = tmp / "html"
    env_args: List[str] = []
    for key, value in {
        "API_URL": "http://stub-api:80",
        "GATEWAY_URL": "http://stub-api:80",
        "NGINX_RESOLVER": "127.0.0.11",
        "HSTS_ENABLED": "true",
        "HSTS_MAX_AGE": "31536000",
        "HSTS_INCLUDE_SUBDOMAINS": "false",
        "HSTS_PRELOAD": "false",
        **env,
    }.items():
        env_args += ["-e", f"{key}={value}"]
    _docker(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        network,
        "-p",
        "127.0.0.1::80",
        *env_args,
        "-v",
        f"{NGINX_CONF}:/etc/nginx/nginx.conf:ro",
        "-v",
        f"{DOCKER_TEMPLATE}:/etc/nginx/templates/default.conf.template:ro",
        "-v",
        f"{DOCKER_SNIPPET}:/etc/nginx/snippets/security-headers.conf:ro",
        "-v",
        f"{html}:/usr/share/nginx/html:ro",
        _nginx_image(),
    )
    port = int(_docker("port", name, "80/tcp").splitlines()[0].rsplit(":", 1)[1])
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
            return port
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.5)
    logs = subprocess.run(
        ["docker", "logs", name], capture_output=True, text=True, check=False
    )
    pytest.fail(f"console nginx did not start: {logs.stdout}{logs.stderr}")


def _request(
    port: int, path: str, method: str = "GET", headers: Dict[str, str] | None = None
) -> Tuple[int, List[Tuple[str, str]]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method, headers=headers or {}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, [(k.lower(), v) for k, v in response.getheaders()]
    except urllib.error.HTTPError as error:
        return error.code, [(k.lower(), v) for k, v in error.headers.items()]


def _values(headers: List[Tuple[str, str]], name: str) -> List[str]:
    return [value for key, value in headers if key == name]


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Dict[str, int]]:
    tmp = tmp_path_factory.mktemp("nginx-smoke")
    html = tmp / "html"
    (html / "assets").mkdir(parents=True)
    (html / "admin").mkdir()
    (html / "index.html").write_text("<html>shell</html>")
    (html / "assets" / "app.js").write_text("console.log(1)")
    (html / "admin" / "index.html").write_text("<html>admin</html>")
    (tmp / "stub.conf").write_text(STUB_API_CONF)
    suffix = uuid.uuid4().hex[:8]
    network = f"preloop-smoke-{suffix}"
    names = [f"preloop-smoke-api-{suffix}"]
    _docker("network", "create", network)
    try:
        _docker(
            "run",
            "-d",
            "--name",
            names[0],
            "--network",
            network,
            "--network-alias",
            "stub-api",
            "-v",
            f"{tmp / 'stub.conf'}:/etc/nginx/conf.d/default.conf:ro",
            _nginx_image(),
        )
        ports = {}
        for label, env in (
            ("default", {}),
            ("disabled", {"HSTS_ENABLED": "false"}),
            (
                "custom",
                {"HSTS_MAX_AGE": "63072000", "HSTS_INCLUDE_SUBDOMAINS": "true"},
            ),
        ):
            name = f"preloop-smoke-{label}-{suffix}"
            names.append(name)
            ports[label] = _start_console(tmp, network, name, env)
        yield ports
    finally:
        for name in names:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


PATHS = ("/", "/console/", "/login", "/admin/", "/assets/app.js", "/api/v1/health")


@pytest.mark.parametrize("path", PATHS)
def test_security_headers_on_every_route_class(
    stack: Dict[str, int], path: str
) -> None:
    """protects=criteria 1 and 4 of #2278 with real nginx; fails_when=a route
    lacks a header, has a wrong value, or sends X-XSS-Protection;
    no_claim=the built frontend bundle (a stub static tree stands in) and the
    real API (a stub nginx upstream stands in)."""
    status, headers = _request(stack["default"], path)
    assert status == 200, path
    for name, value in SECURITY.items():
        assert _values(headers, name) == [value], f"{path}: {name}"
    assert _values(headers, "x-xss-protection") == []


def test_cache_and_cors_headers_unchanged(stack: Dict[str, int]) -> None:
    """protects=criterion 6 of #2278; fails_when=including the set drops the
    location's own cache or CORS headers; no_claim=none beyond protects."""
    _, shell = _request(stack["default"], "/")
    assert _values(shell, "cache-control") == ["no-cache, no-store, must-revalidate"]
    assert _values(shell, "pragma") == ["no-cache"]
    _, asset = _request(stack["default"], "/assets/app.js")
    # `expires 1y` adds its own max-age Cache-Control line next to this one.
    assert "public, immutable" in _values(asset, "cache-control")
    _, api = _request(stack["default"], "/api/v1/health")
    assert _values(api, "access-control-allow-origin") == ["*"]


def test_preflight_carries_security_and_cors_headers(stack: Dict[str, int]) -> None:
    """protects=criterion 2 of #2278; fails_when=the OPTIONS branch drops either
    set; no_claim=none beyond protects."""
    status, headers = _request(
        stack["default"],
        "/api/v1/health",
        method="OPTIONS",
        headers={"Origin": "https://example.test"},
    )
    assert status == 204
    for name, value in SECURITY.items():
        assert _values(headers, name) == [value], name
    assert _values(headers, "access-control-allow-origin") == ["*"]
    assert _values(headers, "access-control-max-age") == ["1728000"]


@pytest.mark.parametrize("path", ("/", "/api/v1/health", "/assets/app.js"))
def test_hsts_only_over_https_and_exactly_once(
    stack: Dict[str, int], path: str
) -> None:
    """protects=criteria 1-4 of #2279 with real nginx, including one header on
    /api/ although the upstream sends its own; fails_when=HSTS appears on plain
    HTTP, twice, with the upstream's value, or ignores the settings;
    no_claim=TLS termination (X-Forwarded-Proto stands in for it)."""
    https = {"X-Forwarded-Proto": "https"}
    _, headers = _request(stack["default"], path, headers=https)
    assert _values(headers, "strict-transport-security") == ["max-age=31536000"]
    _, headers = _request(stack["default"], path)
    assert _values(headers, "strict-transport-security") == []
    _, headers = _request(stack["disabled"], path, headers=https)
    assert _values(headers, "strict-transport-security") == []
    _, headers = _request(stack["custom"], path, headers=https)
    assert _values(headers, "strict-transport-security") == [
        "max-age=63072000; includeSubDomains"
    ]
