"""Installer opt-outs survive upgrades and Compose runs from a clean shell."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = REPO_ROOT / "scripts/install-oss.sh"
OPT_OUTS = ("PRELOOP_DISABLE_TELEMETRY", "DISABLE_VERSION_CHECK")


def _install(home: Path, settings: dict[str, str | None]) -> str:
    """Run the installer with downloads, Docker, and API calls stubbed out."""
    home.mkdir(exist_ok=True)
    prelude = INSTALLER.read_text().rsplit('main "$@"', 1)[0]
    # Exercise main's real env-writing and summary paths without network I/O.
    stubs = """
docker_preflight() { :; }
curl() { cp "$RELEASE_COMPOSE" "$4"; }
generate_secret() { printf '%s' synthetic-install-secret; }
docker() {
  printf '%s\\n' "${PRELOOP_DISABLE_TELEMETRY:-}" "${DISABLE_VERSION_CHECK:-}" > "${INSTALL_DIR}/compose-opt-outs"
}
reset_registration_if_fresh_db() { :; }
main
"""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "PRELOOP_DISABLE_TELEMETRY": "true",
        "PRELOOP_VERSION": "1.0.2",
        "PRELOOP_URL": "http://localhost:3000",
        "PRELOOP_SKIP_SMTP": "1",
        "PRELOOP_SKIP_ADMIN": "1",
        "PRELOOP_BOOTSTRAP_TOKEN": "synthetic-setup-token",
        "RELEASE_COMPOSE": str(REPO_ROOT / "docker-compose.release.yaml"),
        **settings,
    }
    result = subprocess.run(
        ["sh", "-c", prelude + stubs],
        env={key: value for key, value in env.items() if value is not None},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("key", OPT_OUTS)
@pytest.mark.parametrize("value", ["true", "1", "t", " YES "])
def test_installer_persists_opt_out(tmp_path: Path, key: str, value: str) -> None:
    """Both backend flags accept the backend's truthy spellings."""
    output = _install(tmp_path, {key: value})
    env_text = (tmp_path / ".preloop-oss/.env").read_text()
    assert f"{key}=true\n" in env_text
    assert "Version check-in: OFF" in output


@pytest.mark.parametrize("key", OPT_OUTS)
@pytest.mark.parametrize("override", [None, "", "false", "0"])
def test_upgrade_keeps_existing_opt_out(
    tmp_path: Path, key: str, override: str | None
) -> None:
    """A later installer environment must never revoke a stored opt-out."""
    _install(tmp_path, {key: "true"})
    output = _install(tmp_path, {key: override, "PRELOOP_VERSION": "1.0.3"})
    env_text = (tmp_path / ".preloop-oss/.env").read_text()
    assert f"{key}=true\n" in env_text
    assert "PRELOOP_VERSION=1.0.3\n" in env_text
    assert "Version check-in: OFF" in output
    effective = (tmp_path / ".preloop-oss/compose-opt-outs").read_text().splitlines()
    assert effective[OPT_OUTS.index(key)] == "true"


@pytest.mark.parametrize("key", OPT_OUTS)
@pytest.mark.parametrize(
    "value", ["YES", '"true"', "'1'", " t ", "true # opt out", '"yes" # opt out']
)
def test_upgrade_keeps_hand_written_opt_out(
    tmp_path: Path, key: str, value: str
) -> None:
    """Existing opt-outs are honored even when hand-written with quotes."""
    install_dir = tmp_path / ".preloop-oss"
    install_dir.mkdir()
    (install_dir / ".env").write_text(f"{key}={value}\n")
    _install(tmp_path, {key: "false"})
    assert f"{key}=true\n" in (install_dir / ".env").read_text()


def test_installer_reports_enabled_check_in(tmp_path: Path) -> None:
    """The summary describes the default when neither opt-out is enabled."""
    # All Docker and network operations are stubs, so this sends no telemetry.
    output = _install(tmp_path, {"PRELOOP_DISABLE_TELEMETRY": ""})
    assert "Version check-in: ON" in output


def test_legacy_flag_alone_disables_check_in(tmp_path: Path) -> None:
    """The summary recognizes the legacy opt-out independently."""
    output = _install(
        tmp_path, {"PRELOOP_DISABLE_TELEMETRY": None, "DISABLE_VERSION_CHECK": "true"}
    )
    assert "Version check-in: OFF" in output


@pytest.mark.parametrize("key", OPT_OUTS)
@pytest.mark.parametrize("older_release", [False, True])
def test_release_compose_reads_persisted_opt_out(
    tmp_path: Path, key: str, older_release: bool
) -> None:
    """Real Compose resolves the install's .env without inherited opt-outs."""
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose is unavailable")
    # Docker Desktop discovers its plugin under HOME on macOS. Link only the
    # executable into the scratch home; no real Docker configuration is read.
    plugin = Path.home() / ".docker/cli-plugins/docker-compose"
    if plugin.is_file():
        plugins = tmp_path / ".docker/cli-plugins"
        plugins.mkdir(parents=True)
        (plugins / "docker-compose").symlink_to(plugin.resolve())
    clean_env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    version = subprocess.run(
        [docker, "compose", "version"], env=clean_env, capture_output=True, timeout=15
    )
    if version.returncode:
        pytest.skip("Docker Compose is unavailable")
    _install(tmp_path, {key: "true"})
    _install(tmp_path, {key: None, "PRELOOP_VERSION": "1.0.3"})
    compose_args = []
    if older_release:
        compose_file = tmp_path / ".preloop-oss/docker-compose.yaml"
        compose_file.write_text(
            "\n".join(
                line
                for line in compose_file.read_text().splitlines()
                if not any(f"{flag}:" in line for flag in OPT_OUTS)
            )
            + "\n"
        )
        compose_args = ["-f", "docker-compose.yaml", "-f", "docker-compose.auth.yaml"]
    result = subprocess.run(
        [docker, "compose", *compose_args, "config", "--format", "json"],
        cwd=tmp_path / ".preloop-oss",
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    api_env = json.loads(result.stdout)["services"]["api"]["environment"]
    assert api_env[key] == "true"
