"""Fixture site routes, the bounded result writer, and the browser QA flow.

The end-to-end case runs the real egress proxy, the real fixture site and the
pinned Playwright MCP from ``environments/preloop/tools`` on the host, with
``enable.sh`` writing the harness config the flow reads. No model is called.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[3]
# scripts/tests is not a package on every runner's path; import by directory,
# the same way flow_environment_integration.py imports its helpers.
if str(REPO / "scripts" / "tests") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts" / "tests"))

import browser_fixture_flow as flow  # noqa: E402

FIXTURE_DIR = REPO / "environments" / "fixture-site"
BROWSER = REPO / "environments" / "preloop" / "browser"
TOOLS = REPO / "environments" / "preloop" / "tools" / "node_modules"


def _load_fixture_server() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "fixture_site_server", FIXTURE_DIR / "server.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixture_server = _load_fixture_server()


@pytest.fixture
def fixture_site() -> Iterator[int]:
    server = fixture_server.make_server("127.0.0.1", 0, slow_seconds=30)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _request(
    port: int,
    method: str,
    path: str,
    body: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5,
) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    response.read()
    conn.close()
    return response


def test_fixture_home_links_to_sign_in(fixture_site: int) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", fixture_site, timeout=5)
    conn.request("GET", "/")
    response = conn.getresponse()
    body = response.read().decode()
    conn.close()
    assert response.status == 200
    assert "<a href='/login'>Sign in</a>" in body


def test_fixture_login_form_and_credentials(fixture_site: int) -> None:
    assert _request(fixture_site, "GET", "/login").status == 200
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    good = _request(
        fixture_site,
        "POST",
        "/login",
        "email=jane%40example.com&password=fixture-pass",
        form,
    )
    assert good.status == 303
    assert good.getheader("Location") == "/welcome"
    cookie = good.getheader("Set-Cookie") or ""
    assert cookie.startswith("fixture_session=signed-in;")
    bad = _request(
        fixture_site, "POST", "/login", "email=jane%40example.com&password=no", form
    )
    assert bad.status == 401
    assert bad.getheader("Set-Cookie") is None
    huge = _request(fixture_site, "POST", "/login", "x" * 5000, form)
    assert huge.status == 413


def test_fixture_welcome_requires_session(fixture_site: int) -> None:
    anonymous = _request(fixture_site, "GET", "/welcome")
    assert anonymous.status == 303
    assert anonymous.getheader("Location") == "/login"
    conn = http.client.HTTPConnection("127.0.0.1", fixture_site, timeout=5)
    conn.request("GET", "/welcome", headers={"Cookie": "fixture_session=signed-in"})
    response = conn.getresponse()
    body = response.read().decode()
    conn.close()
    assert response.status == 200
    assert "Welcome, Jane Doe" in body


def test_fixture_redirect_targets(fixture_site: int) -> None:
    escape = _request(fixture_site, "GET", "/escape")
    assert escape.status == 302
    assert escape.getheader("Location") == "http://169.254.169.254/latest/meta-data/"
    offsite = _request(fixture_site, "GET", "/offsite")
    assert offsite.status == 302
    assert offsite.getheader("Location") == "https://example.org/"
    assert _request(fixture_site, "GET", "/missing").status == 404
    assert _request(fixture_site, "GET", "/healthz").status == 200


def test_fixture_slow_route_sleeps() -> None:
    server = fixture_server.make_server("127.0.0.1", 0, slow_seconds=1.0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        response = _request(server.server_address[1], "GET", "/slow")
        assert response.status == 200
        assert time.monotonic() - started >= 1.0
    finally:
        server.shutdown()
        server.server_close()


def test_fixture_dockerfile_is_digest_pinned() -> None:
    text = (FIXTURE_DIR / "Dockerfile").read_text()
    assert "FROM python:3.12-slim@sha256:" in text
    assert "USER nobody" in text
    assert "EXPOSE 8080" in text


def test_example_profile_registers_fixture_site() -> None:
    example = REPO / "environments" / "preloop" / "profile.json.example"
    entry = json.loads(example.read_text())["preloop-browser"]
    services = {service["name"]: service for service in entry["services"]}
    assert services["fixture-site"]["port"] == 8080
    assert services["fixture-site"]["image"].endswith("REPLACE_WITH_PUBLISHED_DIGEST")
    allowed = services["egress-proxy"]["env"]["EGRESS_ALLOWED_ORIGINS"].split(",")
    assert "http://fixture-site:8080" in allowed


def test_result_is_bounded_and_redacted() -> None:
    notes = [f"note {i} fixture-pass " + "x" * 500 for i in range(20)]
    doc = flow.bounded_result(
        "http://fixture-site:8080/welcome?p=fixture-pass",
        True,
        notes,
        "2026-11-10T00:00:00+00:00",
    )
    raw = flow.serialize_result(doc)
    assert len(raw) <= flow.RESULT_MAX_BYTES
    assert b"fixture-pass" not in raw
    assert len(doc["notes"]) == flow.MAX_NOTES
    assert all(len(note) <= flow.MAX_NOTE_CHARS for note in doc["notes"])
    assert set(doc) == {"url", "pass", "notes", "checked_at"}
    assert doc["pass"] is True


def test_result_drops_notes_then_shortens_url_to_fit() -> None:
    doc = flow.bounded_result(
        # 4-byte characters: the URL alone (2048 kept) is about 8 KiB.
        "http://fixture-site:8080/" + "\U0001f600" * 2100,
        False,
        ["\U0001f600" * 160] * 5,
        "2026-11-10T00:00:00+00:00",
    )
    assert len(flow.serialize_result(doc)) <= flow.RESULT_MAX_BYTES
    assert doc["notes"] == []
    assert len(doc["url"]) < 2048
    assert doc["pass"] is False


def test_write_result_reports_failures(tmp_path: Path) -> None:
    outcome = flow.FlowOutcome(url="http://fixture-site:8080/login")
    outcome.notes.append("login: FAIL typed fixture-pass")
    outcome.failures.append("login")
    path = tmp_path / "out" / "result.json"
    flow.write_result(path, outcome, checked_at="2026-11-10T00:00:00+00:00")
    written = json.loads(path.read_text())
    assert written["pass"] is False
    assert written["notes"] == ["login: FAIL typed [redacted]"]


def test_load_server_command_reads_codex_and_claude(tmp_path: Path) -> None:
    toml = tmp_path / "config.toml"
    toml.write_text(
        '[mcp_servers.browser]\ncommand = "/bin/mcp"\nargs = ["--isolated"]\n'
    )
    assert flow.load_server_command(toml) == flow.McpServerCommand(
        "/bin/mcp", ["--isolated"]
    )
    mcp_json = tmp_path / ".mcp.json"
    mcp_json.write_text(
        json.dumps({"mcpServers": {"browser": {"command": "/bin/mcp", "args": []}}})
    )
    assert flow.load_server_command(mcp_json).command == "/bin/mcp"
    mcp_json.write_text("{}")
    with pytest.raises(ValueError):
        flow.load_server_command(mcp_json)


def test_unexpected_denials_parses_proxy_log() -> None:
    log = "\n".join(
        [
            "egress-proxy listening",
            json.dumps(
                {
                    "method": "CONNECT",
                    "target": "example.org:443",
                    "reason": "not_allowlisted",
                }
            ),
            json.dumps(
                {
                    "method": "GET",
                    "target": "http://169.254.169.254/latest/meta-data/",
                    "reason": "private_address",
                }
            ),
            json.dumps(
                {"method": "GET", "target": "http://127.0.0.1:9/", "reason": "x"}
            ),
            json.dumps(
                {
                    "method": "GET",
                    "target": "http://fixture-site:8080/slow",
                    "reason": "dial_error",
                }
            ),
        ]
    )
    assert flow.proxy_denials(log)[0] == ("example.org", "not_allowlisted")
    fixture = ("fixture-site",)
    missing, extra = flow.unexpected_denials(
        log, ignored_hosts=("127.0.0.1",), allowed_dial_hosts=fixture
    )
    assert missing == [] and extra == []
    missing, extra = flow.unexpected_denials(log, allowed_dial_hosts=fixture)
    assert extra == [("127.0.0.1", "x")]
    # A dial_error for any other host means it got past the allowlist.
    _, extra = flow.unexpected_denials(log, ignored_hosts=("127.0.0.1",))
    assert extra == [("fixture-site", "dial_error")]
    missing, _ = flow.unexpected_denials("")
    assert missing == ["example.org", "169.254.169.254"]


def test_unexpected_denials_skips_entries_before_since() -> None:
    from datetime import UTC, datetime

    log = "\n".join(
        json.dumps({"ts": ts, "target": target, "reason": "not_allowlisted"})
        for ts, target in (
            ("2026-11-10T14:00:00.123456789Z", "example.org:443"),
            ("2026-11-10T14:00:05.5Z", "http://169.254.169.254/"),
        )
    )
    since = datetime(2026, 11, 10, 14, 0, 1, tzinfo=UTC)
    missing, extra = flow.unexpected_denials(log, since=since)
    # The setup-time example.org denial must not satisfy the flow's check.
    assert missing == ["example.org"]
    assert extra == []


def _browsers_root(executable: Path) -> Path:
    """The Playwright browsers directory that holds ``executable``."""
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured and configured != "0":
        return Path(configured)
    browsers = executable
    while browsers.name != "ms-playwright":
        assert browsers != browsers.parent, f"no ms-playwright above {executable}"
        browsers = browsers.parent
    return browsers


def test_browsers_root_prefers_env_and_never_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/preloop-browsers")
    assert _browsers_root(Path("/opt/preloop-browsers/c/chrome")) == Path(
        "/opt/preloop-browsers"
    )
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH")
    assert _browsers_root(Path("/h/.cache/ms-playwright/c/chrome")) == Path(
        "/h/.cache/ms-playwright"
    )
    with pytest.raises(AssertionError):
        _browsers_root(Path("/opt/elsewhere/chrome"))


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _chromium_ready() -> bool:
    if not (TOOLS / "playwright").is_dir() or not (TOOLS / ".bin").is_dir():
        return False
    probe = subprocess.run(
        [
            "node",
            "-e",
            "const {chromium}=require('playwright');"
            "process.exit(require('fs').existsSync(chromium.executablePath())?0:1)",
        ],
        capture_output=True,
        env={**os.environ, "NODE_PATH": str(TOOLS)},
        timeout=30,
    )
    return probe.returncode == 0


@pytest.mark.skipif(
    os.environ.get("PRELOOP_BROWSER_CI") != "1" and not _chromium_ready(),
    reason="needs Playwright MCP from environments/preloop/tools and Chromium",
)
def test_browser_fixture_flow_end_to_end(tmp_path: Path, fixture_site: int) -> None:
    binary = tmp_path / "egress-proxy"
    build = subprocess.run(
        ["go", "build", "-o", str(binary), "."],
        cwd=REPO / "environments" / "egress-proxy",
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert build.returncode == 0, build.stderr
    proxy_port = _free_port()
    base_url = f"http://127.0.0.1:{fixture_site}"
    proxy_log = tmp_path / "proxy.log"
    proxy_env = {
        **os.environ,
        "EGRESS_LISTEN": f"127.0.0.1:{proxy_port}",
        "EGRESS_ALLOWED_ORIGINS": base_url,
        "EGRESS_DENY_PRIVATE": "true",
        # The host-mode fixture is on loopback; only its exact origin is
        # allowlisted, so this carve-out admits nothing else.
        "EGRESS_ALLOW_PRIVATE_CIDRS": "127.0.0.1/32",
    }
    with proxy_log.open("wb") as log_handle:
        proxy = subprocess.Popen(
            [str(binary)], env=proxy_env, stdout=log_handle, stderr=log_handle
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", proxy_port), 0.2):
                        break
                except OSError:
                    time.sleep(0.05)
            located = subprocess.run(
                [
                    "node",
                    "-e",
                    "console.log(require('playwright').chromium.executablePath())",
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "NODE_PATH": str(TOOLS)},
                timeout=30,
                check=True,
            )
            browsers = _browsers_root(Path(located.stdout.strip()))
            home = tmp_path / "home"
            home.mkdir()
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("PRELOOP_BROWSER_")
            }
            env.update(
                {
                    "HOME": str(home),
                    "PRELOOP_HARNESS": "codex",
                    "PRELOOP_BROWSER_PROXY": f"http://127.0.0.1:{proxy_port}",
                    "PRELOOP_BROWSER_STATE_DIR": str(tmp_path / "state"),
                    "PRELOOP_PLAYWRIGHT_NODE_PATH": str(TOOLS),
                    "PRELOOP_PLAYWRIGHT_MCP_BIN": str(
                        TOOLS / ".bin" / "playwright-mcp"
                    ),
                    "PLAYWRIGHT_BROWSERS_PATH": str(browsers),
                }
            )
            enable = subprocess.run(
                ["bash", str(BROWSER / "enable.sh")],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert enable.returncode == 0, enable.stdout + enable.stderr
            log_handle.flush()
            setup_offset = proxy_log.stat().st_size
            result_path = tmp_path / "result.json"
            run = subprocess.run(
                [
                    sys.executable,
                    str(REPO / "scripts" / "tests" / "browser_fixture_flow.py"),
                    "--mcp-config",
                    str(home / ".codex" / "config.toml"),
                    "--base-url",
                    base_url,
                    "--result",
                    str(result_path),
                    "--tool-timeout",
                    "5",
                ],
                env=env,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=240,
            )
        finally:
            proxy.terminate()
            try:
                proxy.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proxy.kill()
    assert result_path.is_file(), run.stdout + run.stderr
    raw = result_path.read_bytes()
    result = json.loads(raw)
    assert run.returncode == 0, run.stdout + run.stderr + raw.decode()
    assert result["pass"] is True, result
    assert result["url"] == base_url + "/welcome"
    assert len(raw) <= flow.RESULT_MAX_BYTES
    assert b"fixture-pass" not in raw
    prefixes = [note.split(":", 1)[0] for note in result["notes"]]
    assert prefixes == [
        "login",
        "rejected_origin",
        "redirect_escape",
        "timeout",
        "cleanup",
    ]
    flow_log = proxy_log.read_bytes()[setup_offset:].decode()
    # The abandoned /slow request to the fixture may log a dial_error.
    missing, extra = flow.unexpected_denials(
        flow_log, allowed_dial_hosts=("127.0.0.1",)
    )
    assert missing == [], flow_log
    assert extra == [], flow_log
    reasons = dict(flow.proxy_denials(flow_log))
    assert reasons["example.org"] == "not_allowlisted"
    assert reasons["169.254.169.254"] in {"private_address", "not_allowlisted"}


def _integration_module() -> ModuleType:
    scripts = str(REPO / "scripts" / "tests")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import flow_environment_integration

    return flow_environment_integration


def test_integration_browser_profile_is_valid_and_ships_the_scripts(
    tmp_path: Path,
) -> None:
    from argparse import Namespace

    from preloop.services.flow_environment import EnvironmentProfile

    integration = _integration_module()
    digest = "@sha256:" + "b" * 64
    raw = integration.browser_profile(
        Namespace(
            image="example.com/env" + digest,
            proxy_image="example.com/egress-proxy" + digest,
            fixture_image="example.com/fixture-site" + digest,
        )
    )
    profile = EnvironmentProfile.model_validate(raw)
    assert [service.name for service in profile.services] == [
        "egress-proxy",
        "fixture-site",
    ]
    assert raw["services"][0]["env"]["EGRESS_ALLOWED_ORIGINS"] == (
        "http://fixture-site:8080"
    )
    unpack = raw["setup_commands"][0]
    encoded = unpack.split()[1]
    import base64
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(encoded))) as archive:
        names = sorted(archive.getnames())
    assert names == [
        "environments/preloop/browser/enable.sh",
        "environments/preloop/browser/playwright-mcp.config.json",
        "environments/preloop/browser/selfcheck.sh",
        "scripts/tests/browser_fixture_flow.py",
    ]


def _good_run() -> tuple[str, str]:
    doc = flow.bounded_result(
        "http://fixture-site:8080/welcome",
        True,
        [
            "login: ok",
            "rejected_origin: ok",
            "redirect_escape: ok",
            "timeout: ok",
            "cleanup: ok",
        ],
        "2026-11-10T00:00:00+00:00",
    )
    logs = (
        "setup\nBROWSER_FLOW_START 2026-11-10T14:00:10+00:00\n"
        "BROWSER_FIXTURE_RESULT "
        + flow.serialize_result(doc).decode().strip()
        + "\nBROWSER_FIXTURE_EXIT=0\n"
    )
    # The first two lines are the setup self-check, before the start marker.
    proxy_log = "\n".join(
        json.dumps({"ts": ts, "target": target, "reason": reason})
        for ts, target, reason in (
            ("2026-11-10T14:00:01Z", "127.0.0.1:41000", "not_allowlisted"),
            ("2026-11-10T14:00:02Z", "example.org:443", "not_allowlisted"),
            ("2026-11-10T14:00:11Z", "example.org:443", "not_allowlisted"),
            (
                "2026-11-10T14:00:12Z",
                "http://169.254.169.254/latest/meta-data/",
                "not_allowlisted",
            ),
            ("2026-11-10T14:00:20Z", "http://fixture-site:8080/slow", "dial_error"),
        )
    )
    return logs, proxy_log


def test_check_browser_run_accepts_expected_denials() -> None:
    integration = _integration_module()
    logs, proxy_log = _good_run()
    assert integration.check_browser_run(logs, proxy_log)["pass"] is True


def test_check_browser_run_rejects_leaks_and_unexpected_denials() -> None:
    integration = _integration_module()
    logs, proxy_log = _good_run()
    with pytest.raises(AssertionError, match="credential leaked"):
        integration.check_browser_run(
            logs.replace("cleanup: ok", "cleanup: ok fixture-pass"), proxy_log
        )
    late = json.dumps(
        {"ts": "2026-11-10T14:00:30Z", "target": "x.test:443", "reason": "r"}
    )
    with pytest.raises(AssertionError):
        integration.check_browser_run(logs, proxy_log + "\n" + late)
    # Only the setup-time denials: the flow's own denials are missing.
    setup_only = "\n".join(proxy_log.splitlines()[:2])
    with pytest.raises(AssertionError):
        integration.check_browser_run(logs, setup_only)
