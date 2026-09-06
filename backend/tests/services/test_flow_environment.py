"""Environment approval, protocol preflight and setup deadline tests."""

import json
import subprocess
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
