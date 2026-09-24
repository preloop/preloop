"""Environment approval, protocol preflight and setup deadline tests."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from preloop.config import settings
from preloop.services.flow_environment import (
    EnvironmentProfile,
    profile_setup_shell,
    resolve_profile,
)

IMAGE = "example.com/agent@sha256:" + "a" * 64


def test_profile_requires_pinned_image() -> None:
    with pytest.raises(ValidationError):
        EnvironmentProfile(image="example.com/agent:latest", harness="codex")


def test_payload_cannot_supply_unapproved_image_profile(tmp_path, monkeypatch) -> None:
    registry = tmp_path / "profiles.json"
    registry.write_text(json.dumps({"approved": {"image": IMAGE, "harness": "codex"}}))
    monkeypatch.setattr(settings, "flow_environment_profiles_file", str(registry))
    with pytest.raises(ValueError, match="not_approved"):
        resolve_profile(
            {"environment_profile": "from-issue"}, agent_type="codex", runner="docker"
        )
    profile = resolve_profile(
        {"environment_profile": "approved", "image": "attacker/image"},
        agent_type="codex",
        runner="docker",
    )
    assert profile.image == IMAGE
    with pytest.raises(ValueError, match="unsupported_private"):
        resolve_profile(
            {"environment_profile": "approved"}, agent_type="codex", runner="private"
        )


def test_profile_rejects_harness_mismatch(tmp_path, monkeypatch) -> None:
    registry = tmp_path / "profiles.json"
    registry.write_text(json.dumps({"approved": {"image": IMAGE, "harness": "codex"}}))
    monkeypatch.setattr(settings, "flow_environment_profiles_file", str(registry))
    with pytest.raises(ValueError, match="harness_mismatch"):
        resolve_profile(
            {"environment_profile": "approved"}, agent_type="opencode", runner="docker"
        )


def test_missing_or_corrupt_registry_fails_closed(tmp_path, monkeypatch) -> None:
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(settings, "flow_environment_profiles_file", str(missing))
    with pytest.raises(ValueError, match="not_approved"):
        resolve_profile(
            {"environment_profile": "approved"}, agent_type="codex", runner="docker"
        )
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{")
    monkeypatch.setattr(settings, "flow_environment_profiles_file", str(corrupt))
    with pytest.raises(ValueError, match="not_approved"):
        resolve_profile(
            {"environment_profile": "approved"}, agent_type="codex", runner="docker"
        )


def test_environment_digest_changes_with_lockfile_contract() -> None:
    a = EnvironmentProfile(
        image=IMAGE, harness="codex", lockfiles=["package-lock.json"]
    )
    b = a.model_copy(update={"setup_commands": ["npm ci"]})
    assert a.digest != b.digest


def test_setup_separate_timeout_kills_process_group(tmp_path) -> None:
    profile = EnvironmentProfile(
        image=IMAGE,
        harness="codex",
        setup_commands=["sleep 30"],
        setup_timeout_seconds=1,
    )
    protocol = tmp_path / "protocol.json"
    protocol.write_text(json.dumps({"version": 1, "harness": "codex"}))
    shell = (
        profile_setup_shell(profile, kubernetes=False, working_dir=str(tmp_path))
        .replace("/workspace/evidence", str(tmp_path / "evidence"))
        .replace("/opt/preloop-environment.json", str(protocol))
    )
    result = subprocess.run(
        ["bash", "-c", shell], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 124
    assert "PRELOOP_SETUP_FAILED setup_timeout" in result.stdout


def test_cached_setup_requires_unchanged_lockfile_and_existing_dependencies(
    tmp_path,
) -> None:
    protocol = tmp_path / "protocol.json"
    protocol.write_text(json.dumps({"version": 1, "harness": "codex"}))
    lockfile = tmp_path / "lock.json"
    lockfile.write_text("version-one")
    profile = EnvironmentProfile(
        image=IMAGE,
        harness="codex",
        setup_commands=["mkdir -p deps; echo installed >> count"],
        lockfiles=["lock.json"],
        cache_paths=["deps"],
    )
    shell = (
        profile_setup_shell(profile, kubernetes=False, working_dir=str(tmp_path))
        .replace("/workspace/evidence", str(tmp_path / "evidence"))
        .replace("/opt/preloop-environment.json", str(protocol))
    )
    first = subprocess.run(
        ["bash", "-c", shell], capture_output=True, text=True, timeout=5
    )
    second = subprocess.run(
        ["bash", "-c", shell], capture_output=True, text=True, timeout=5
    )
    assert first.returncode == second.returncode == 0
    assert "cache_hit" in second.stdout
    assert (tmp_path / "count").read_text().splitlines() == ["installed"]
    lockfile.write_text("version-two")
    third = subprocess.run(
        ["bash", "-c", shell], capture_output=True, text=True, timeout=5
    )
    assert third.returncode == 0
    assert "cache_hit" not in third.stdout
    assert len((tmp_path / "count").read_text().splitlines()) == 2


def test_incompatible_image_protocol_fails_before_setup(tmp_path) -> None:
    profile = EnvironmentProfile(
        image=IMAGE, harness="codex", setup_commands=["touch agent-started"]
    )
    shell = (
        profile_setup_shell(profile, kubernetes=False, working_dir=str(tmp_path))
        .replace("/workspace/evidence", str(tmp_path / "evidence"))
        .replace("/opt/preloop-environment.json", str(tmp_path / "missing"))
    )
    result = subprocess.run(
        ["bash", "-c", shell], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 78
    assert "environment_protocol_unsupported" in result.stdout
    assert not (tmp_path / "agent-started").exists()


def test_readiness_requires_each_named_verification_command(
    tmp_path, monkeypatch
) -> None:
    from preloop.services.flow_environment import profile_readiness

    registry = tmp_path / "profiles.json"
    registry.write_text(
        json.dumps(
            {
                "approved": {
                    "image": IMAGE,
                    "harness": "codex",
                    "test_commands": {"component": ["npm test"]},
                }
            }
        )
    )
    monkeypatch.setattr(settings, "flow_environment_profiles_file", str(registry))
    result = profile_readiness(
        {"environment_profile": "approved"},
        agent_type="codex",
        runner="docker",
        required_command_ids=["component", "backend"],
    )
    assert result["ready"] is False
    assert result["blockers"] == ["environment_command_missing:backend"]


def test_readiness_checks_command_text_not_only_identifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.services.flow_environment import profile_readiness

    registry = tmp_path / "profiles.json"
    registry.write_text(
        json.dumps(
            {
                "approved": {
                    "image": IMAGE,
                    "harness": "codex",
                    "test_commands": {"component": ["cd frontend", "npm test"]},
                }
            }
        )
    )
    monkeypatch.setattr(settings, "flow_environment_profiles_file", str(registry))
    kwargs = {"agent_type": "codex", "runner": "server", "required_command_ids": []}
    approved = profile_readiness(
        {"environment_profile": "approved"},
        **kwargs,
        required_commands={"component": "cd frontend\nnpm test"},
    )
    assert approved["ready"] is True
    mismatch = profile_readiness(
        {"environment_profile": "approved"},
        **kwargs,
        required_commands={"component": "echo passed"},
    )
    assert mismatch["ready"] is False
    assert mismatch["blockers"] == ["environment_command_mismatch:component"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pool, commands, expected",
    [
        ("server", ["component"], []),
        ("server", ["component", "backend"], ["environment_command_missing:backend"]),
        (
            "private-pool",
            ["component"],
            ["environment_protocol_unsupported_private_runner"],
        ),
    ],
)
async def test_lifecycle_uses_actual_environment_capability_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pool: str,
    commands: list[str],
    expected: list[str],
) -> None:
    from types import SimpleNamespace
    from uuid import uuid4
    from unittest.mock import Mock

    from preloop.services.issue_lifecycle_runtime import FlowEnvironmentCapabilities
    from preloop.models.crud import crud_flow

    registry = tmp_path / "profiles.json"
    registry.write_text(
        json.dumps(
            {
                "approved": {
                    "image": IMAGE,
                    "harness": "codex",
                    "test_commands": {"component": ["npm test"]},
                }
            }
        )
    )
    monkeypatch.setattr(settings, "flow_environment_profiles_file", str(registry))
    flow = SimpleNamespace(
        agent_config={"environment_profile": "approved"},
        agent_type="codex",
        runner_pool=pool,
        is_enabled=True,
        git_clone_config={
            "verification": {
                "mode": "gate",
                "profile": {
                    "profile_id": "tests",
                    "always": [
                        {"id": key, "command": "npm test", "reason": "acceptance"}
                        for key in commands
                    ],
                },
            }
        },
    )
    monkeypatch.setattr(crud_flow, "get", lambda *args, **kwargs: flow)
    capabilities = FlowEnvironmentCapabilities(
        Mock(),
        uuid4(),
        {"implementation_flow_id": str(uuid4())},
    )
    assert await capabilities.blockers("approved", commands) == expected


@pytest.mark.parametrize(
    "policy, expected",
    [
        ({}, "verification_gate_required"),
        ({"mode": "off"}, "verification_gate_required"),
        ({"mode": "gate"}, "verification_policy_invalid"),
        (
            {"mode": "gate", "profile": {"profile_id": "empty"}},
            "verification_profile_empty",
        ),
    ],
)
def test_verification_readiness_blocks_missing_or_invalid_gate(
    policy: dict,
    expected: str,
) -> None:
    from preloop.services.flow_environment import verification_profile_readiness

    result = verification_profile_readiness(
        {},
        {"verification": policy} if policy else {},
        agent_type="codex",
        runner="server",
        required_command_ids=["component"],
    )
    assert result["ready"] is False
    assert result["blockers"] == [expected]


def test_verification_readiness_requires_every_rule_and_issue_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.services.flow_environment import verification_profile_readiness

    registry = tmp_path / "profiles.json"
    registry.write_text(
        json.dumps(
            {
                "approved": {
                    "image": IMAGE,
                    "harness": "codex",
                    "test_commands": {
                        "lint": ["ruff check ."],
                        "component": ["npm test"],
                    },
                }
            }
        )
    )
    monkeypatch.setattr(settings, "flow_environment_profiles_file", str(registry))
    policy = {
        "verification": {
            "mode": "gate",
            "profile": {
                "profile_id": "tests",
                "always": [
                    {"id": "lint", "command": "ruff check .", "reason": "style"}
                ],
                "rules": [
                    {
                        "id": "backend",
                        "description": "API changes",
                        "path_globs": ["backend/*"],
                        "commands": [
                            {"id": "backend", "command": "pytest", "reason": "API"}
                        ],
                    }
                ],
                "unknown_default": [
                    {"id": "component", "command": "npm test", "reason": "unknown"}
                ],
            },
        }
    }
    result = verification_profile_readiness(
        {"environment_profile": "approved"},
        policy,
        agent_type="codex",
        runner="server",
        required_command_ids=["lint", "component"],
    )
    assert result["blockers"] == ["environment_command_missing:backend"]
    policy["verification"]["profile"]["rules"] = []
    result = verification_profile_readiness(
        {"environment_profile": "approved"},
        policy,
        agent_type="codex",
        runner="server",
        required_command_ids=["e2e"],
    )
    assert result["blockers"] == [
        "environment_command_missing:e2e",
        "verification_command_missing:e2e",
    ]


REPO = Path(__file__).resolve().parents[3]
EXAMPLE = REPO / "environments" / "preloop" / "profile.json.example"
BROWSER = REPO / "environments" / "preloop" / "browser"
DIGEST = "a" * 64


def _browser_profile() -> EnvironmentProfile:
    """Load the example, then substitute the published-digest placeholder."""
    entry = json.loads(EXAMPLE.read_text())["preloop-browser"]
    rendered = json.dumps(entry).replace("REPLACE_WITH_PUBLISHED_DIGEST", DIGEST)
    return EnvironmentProfile.model_validate(json.loads(rendered))


def test_example_browser_profile_is_a_valid_environment_profile() -> None:
    raw = json.loads(EXAMPLE.read_text())["preloop-browser"]
    assert raw["image"].endswith("REPLACE_WITH_PUBLISHED_DIGEST")
    assert raw["setup_commands"] == ["bash environments/preloop/browser/enable.sh"]
    assert raw["artifact_paths"] == ["result.json"]
    assert raw["env"]["PRELOOP_HARNESS"] == "codex"
    assert raw["env"]["PRELOOP_BROWSER_PROXY"] == "http://${service.egress-proxy}:3128"
    service = raw["services"][0]
    assert service["name"] == "egress-proxy"
    assert service["port"] == 3128
    assert service["env"] == {
        "EGRESS_ALLOWED_ORIGINS": "http://fixture-site:8080",
        "EGRESS_ALLOW_PRIVATE_CIDRS": "172.20.0.0/16",
    }
    profile = _browser_profile()
    assert profile.image.endswith(DIGEST)
    assert profile.services[0].image.endswith(DIGEST)
    from preloop.services.flow_environment import profile_env

    docker_env = profile_env(profile, kubernetes=False)
    kube_env = profile_env(profile, kubernetes=True)
    assert docker_env["PRELOOP_BROWSER_PROXY"] == "http://egress-proxy:3128"
    assert kube_env["PRELOOP_BROWSER_PROXY"] == "http://127.0.0.1:3128"


def test_browser_scripts_pass_bash_n() -> None:
    for name in ("enable.sh", "selfcheck.sh"):
        result = subprocess.run(
            ["bash", "-n", str(BROWSER / name)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr


def _run_enable(
    tmp_path: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged.pop("PRELOOP_BROWSER_PROXY", None)
    merged.pop("PRELOOP_BROWSER_PROXY_HOST", None)
    merged.pop("PRELOOP_BROWSER_CONFIG", None)
    merged["HOME"] = str(tmp_path / "home")
    merged["PRELOOP_BROWSER_STATE_DIR"] = str(tmp_path / "state")
    merged["PRELOOP_HARNESS"] = "codex"
    merged.update(env)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "home").mkdir()
    return subprocess.run(
        ["bash", str(BROWSER / "enable.sh")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=merged,
        timeout=60,
    )


def test_rendered_harness_config_is_isolated_and_selfcheck_fails_closed(
    tmp_path: Path,
) -> None:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    result = _run_enable(
        tmp_path, {"PRELOOP_BROWSER_PROXY": f"http://127.0.0.1:{port}"}
    )
    assert result.returncode != 0
    assert "browser_egress_not_enforced" in result.stdout + result.stderr
    rendered = json.loads(
        (tmp_path / "state" / "playwright-mcp.config.json").read_text()
    )
    args = rendered["browser"]["launchOptions"]["args"]
    assert f"--proxy-server=http://127.0.0.1:{port}" in args
    assert "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1" in args
    assert "--proxy-bypass-list=<-loopback>" in args
    harness = (tmp_path / "home" / ".codex" / "config.toml").read_text()
    assert "--isolated" in harness
    assert "@playwright/mcp@0.0.82" in harness
    assert "/opt/preloop-env-tools/node_modules/.bin/playwright-mcp" in harness
    assert "[mcp_servers.browser]" in harness
    claude = _run_enable(
        tmp_path / "claude",
        {
            "PRELOOP_BROWSER_PROXY": f"http://127.0.0.1:{port}",
            "PRELOOP_HARNESS": "claude",
            "PRELOOP_MCP_JSON": str(tmp_path / "claude" / ".mcp.json"),
        },
    )
    assert claude.returncode != 0
    mcp = json.loads((tmp_path / "claude" / ".mcp.json").read_text())
    assert "--isolated" in mcp["mcpServers"]["browser"]["args"]


def test_removed_proxy_env_aborts_profile_setup(tmp_path: Path) -> None:
    profile = EnvironmentProfile(
        image=IMAGE,
        harness="codex",
        setup_commands=[f"bash {BROWSER / 'enable.sh'}"],
        setup_timeout_seconds=30,
    )
    protocol = tmp_path / "protocol.json"
    protocol.write_text(json.dumps({"version": 1, "harness": "codex"}))
    shell = (
        profile_setup_shell(profile, kubernetes=False, working_dir=str(tmp_path))
        .replace("/workspace/evidence", str(tmp_path / "evidence"))
        .replace("/opt/preloop-environment.json", str(protocol))
    )
    env = os.environ.copy()
    env.pop("PRELOOP_BROWSER_PROXY", None)
    env["HOME"] = str(tmp_path / "home")
    env["PRELOOP_HARNESS"] = "codex"
    (tmp_path / "home").mkdir()
    result = subprocess.run(
        ["bash", "-c", shell],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=tmp_path,
    )
    assert result.returncode != 0
    assert "PRELOOP_SETUP_FAILED setup_failed" in result.stdout
    log = (tmp_path / "evidence" / "setup.log").read_text()
    assert "browser_egress_not_enforced" in log


def _browser_node_path() -> Path:
    """Playwright next to the pinned MCP package, not the frontend copy."""
    return REPO / "environments" / "preloop" / "tools" / "node_modules"


def _chromium_ready() -> bool:
    node_path = _browser_node_path()
    if not (node_path / "playwright").is_dir():
        return False
    probe = subprocess.run(
        [
            "node",
            "-e",
            (
                "const {chromium}=require('playwright');"
                "const fs=require('fs');"
                "process.exit(fs.existsSync(chromium.executablePath())?0:1)"
            ),
        ],
        capture_output=True,
        env={**os.environ, "NODE_PATH": str(node_path)},
        timeout=30,
    )
    return probe.returncode == 0


@pytest.mark.skipif(
    os.environ.get("PRELOOP_BROWSER_CI") != "1" and not _chromium_ready(),
    reason="needs Playwright from environments/preloop/tools and a Chromium build",
)
def test_enable_blocks_metadata_and_non_allowlisted_origin(tmp_path: Path) -> None:
    import socket

    binary = tmp_path / "egress-proxy"
    build = subprocess.run(
        ["go", "build", "-o", str(binary), "."],
        cwd=REPO / "environments" / "egress-proxy",
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert build.returncode == 0, build.stderr
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    proxy_env = os.environ.copy()
    proxy_env["EGRESS_LISTEN"] = f"127.0.0.1:{port}"
    proxy_env["EGRESS_ALLOWED_ORIGINS"] = "http://fixture-site:8080"
    proxy_env["EGRESS_DENY_PRIVATE"] = "true"
    proxy_env["EGRESS_ALLOW_PRIVATE_CIDRS"] = "10.200.0.0/16"
    proxy = subprocess.Popen(
        [str(binary)],
        env=proxy_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        ready = False
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    ready = True
                    break
            except OSError:
                time.sleep(0.05)
        assert ready
        located = subprocess.run(
            [
                "node",
                "-e",
                (
                    "const {chromium}=require('playwright');"
                    "console.log(chromium.executablePath())"
                ),
            ],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "NODE_PATH": str(_browser_node_path()),
            },
            timeout=30,
            check=True,
        )
        browsers = Path(located.stdout.strip())
        while browsers.name != "ms-playwright":
            browsers = browsers.parent
        result = _run_enable(
            tmp_path,
            {
                "PRELOOP_BROWSER_PROXY": f"http://127.0.0.1:{port}",
                "PRELOOP_PLAYWRIGHT_NODE_PATH": str(_browser_node_path()),
                "PLAYWRIGHT_BROWSERS_PATH": str(browsers),
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "browser_probe_blocked http://169.254.169.254/" in result.stdout
        assert "browser_probe_blocked https://example.org/" in result.stdout
        assert "browser_egress_not_enforced" not in result.stdout
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy.kill()
