"""Cached downloads must not turn into shared mutable Python environments."""

import importlib.util
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ACTION = ROOT / ".github/actions/install-ci-python-deps"
SPEC = importlib.util.spec_from_file_location("ci_dependencies", ACTION / "install.py")
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def test_installer_keeps_hash_checks_and_recreates_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / "requirements.txt"
    lock.write_text("example==1 --hash=sha256:example\n")
    run = Mock()
    monkeypatch.setattr(installer.subprocess, "run", run)
    binaries = installer.install(tmp_path, tmp_path / "job", lock.name, True)
    commands = [call.args[0] for call in run.call_args_list]
    assert "--clear" in commands[0]
    assert str(tmp_path / "job/preloop-deps") == commands[0][-1]
    assert "--require-hashes" in commands[2]
    assert commands[2][-2:] == ["-r", str(lock)]
    assert commands[3][-3:] == ["--no-deps", "-e", str(tmp_path)]
    assert binaries.is_relative_to(tmp_path / "job")
    assert all(call.kwargs["check"] for call in run.call_args_list)


def test_failed_install_is_not_published_to_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "requirements.txt").touch()
    output = tmp_path / "github-path"
    for name, value in {
        "GITHUB_WORKSPACE": str(tmp_path),
        "RUNNER_TEMP": str(tmp_path / "job"),
        "REQUIREMENTS_PATH": "requirements.txt",
        "GITHUB_PATH": str(output),
        "INSTALL_PROJECT": "false",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        Mock(side_effect=subprocess.CalledProcessError(1, ["uv"])),
    )
    with pytest.raises(subprocess.CalledProcessError):
        installer.main()
    assert not output.exists()


def test_archive_cache_is_hosted_only_and_keyed_to_lockfile() -> None:
    action = yaml.safe_load((ACTION / "action.yml").read_text())
    setup = action["runs"]["steps"][0]
    assert (
        setup["with"]["enable-cache"] == "${{ runner.environment == 'github-hosted' }}"
    )
    assert setup["with"]["cache-dependency-glob"] == "${{ inputs.requirements }}"
    assert setup["with"]["cache-local-path"].startswith("${{ runner.tool_cache }}")
    assert setup["with"]["prune-cache"] == "false"
