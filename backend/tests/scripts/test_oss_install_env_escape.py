"""Compose ``.env`` escaping for OSS installer secrets that contain ``$``.

Docker Compose interpolates ``$VAR`` in ``.env`` files. The installer must
write a literal dollar as ``$$`` and unescape on read so re-runs round-trip.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_OSS = REPO_ROOT / "scripts" / "install-oss.sh"
RELEASE_COMPOSE = REPO_ROOT / "docker-compose.release.yaml"

LEADING_DOLLAR_PASSWORD = "$UsErPassWoRd"
COMPOSE_WARN_LEAK = 'The "UsErPassWoRd" variable is not set'


def _installer_prelude() -> str:
    """Return install-oss.sh without the trailing ``main`` invocation."""
    text = INSTALL_OSS.read_text(encoding="utf-8")
    marker = 'main "$@"'
    if not text.rstrip().endswith(marker):
        raise AssertionError('scripts/install-oss.sh must end with main "$@"')
    return text.rsplit(marker, 1)[0]


def _run_installer_sh(
    snippet: str,
    *args: str,
    install_dir: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a POSIX ``sh`` snippet after defining installer helpers (not main)."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    if install_dir is not None:
        env["INSTALL_DIR"] = str(install_dir)
    return subprocess.run(
        ["sh", "-c", _installer_prelude() + snippet, "install-oss-test", *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def compose_env_escape(value: str) -> str:
    """Escape ``$`` the way ``scripts/install-oss.sh`` writes ``.env`` values."""
    proc = _run_installer_sh('compose_env_escape "$1"', value)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.rstrip("\n")


def compose_env_unescape(value: str) -> str:
    """Unescape ``$$`` the way ``env_value`` reads secrets back."""
    proc = _run_installer_sh('compose_env_unescape "$1"', value)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.rstrip("\n")


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "compose", "version"],
            check=True,
            capture_output=True,
            timeout=15,
        )
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ):
        return False
    return True


def test_escape_leading_dollar_password() -> None:
    assert compose_env_escape(LEADING_DOLLAR_PASSWORD) == "$$UsErPassWoRd"


def test_unescape_round_trip_leading_dollar() -> None:
    escaped = compose_env_escape(LEADING_DOLLAR_PASSWORD)
    assert compose_env_unescape(escaped) == LEADING_DOLLAR_PASSWORD


@pytest.mark.parametrize(
    ("secret", "escaped"),
    [
        ("a$b$c", "a$$b$$c"),
        ("pw$$word", "pw$$$$word"),
        ("$a$$b$", "$$a$$$$b$$"),
        ("no-dollar", "no-dollar"),
        ("", ""),
    ],
)
def test_escape_unescape_round_trip_multiple_dollars(secret: str, escaped: str) -> None:
    assert compose_env_escape(secret) == escaped
    assert compose_env_unescape(escaped) == secret


def test_set_env_value_writes_escaped_and_env_value_unescapes(
    tmp_path: Path,
) -> None:
    proc = _run_installer_sh(
        'set_env_value SMTP_PASSWORD "$1"\n'
        'env_value SMTP_PASSWORD "${INSTALL_DIR}/.env"\n',
        LEADING_DOLLAR_PASSWORD,
        install_dir=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    on_disk = (tmp_path / ".env").read_text(encoding="utf-8")
    assert on_disk.split("=", 1)[1].strip() == "$$UsErPassWoRd"
    assert proc.stdout.rstrip("\n") == LEADING_DOLLAR_PASSWORD


def test_set_env_value_re_run_does_not_double_escape(tmp_path: Path) -> None:
    first = _run_installer_sh(
        'set_env_value SMTP_PASSWORD "$1"',
        LEADING_DOLLAR_PASSWORD,
        install_dir=tmp_path,
    )
    assert first.returncode == 0, first.stderr
    second = _run_installer_sh(
        'SMTP_PASSWORD="$(env_value SMTP_PASSWORD "${INSTALL_DIR}/.env")"\n'
        'set_env_value SMTP_PASSWORD "$SMTP_PASSWORD"\n',
        install_dir=tmp_path,
    )
    assert second.returncode == 0, second.stderr
    on_disk = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "SMTP_PASSWORD=$$UsErPassWoRd\n" == on_disk


def test_compose_env_assign_first_install_write_path(tmp_path: Path) -> None:
    proc = _run_installer_sh(
        'compose_env_assign SMTP_PASSWORD "$1" > "${INSTALL_DIR}/.env"\n',
        LEADING_DOLLAR_PASSWORD,
        install_dir=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / ".env").read_text(encoding="utf-8") == (
        "SMTP_PASSWORD=$$UsErPassWoRd\n"
    )


def test_release_compose_interpolates_smtp_password_from_env() -> None:
    text = RELEASE_COMPOSE.read_text(encoding="utf-8")
    assert text.count("SMTP_PASSWORD: ${SMTP_PASSWORD:-}") == 2


@pytest.mark.skipif(
    not _docker_compose_available(),
    reason="docker compose is not available",
)
def test_compose_config_keeps_literal_leading_dollar_password(tmp_path: Path) -> None:
    """Installer-escaped ``.env`` must not interpolate or leak via compose WARN."""
    env_file = tmp_path / ".env"
    write = _run_installer_sh(
        'compose_env_assign SMTP_PASSWORD "$1" > "${INSTALL_DIR}/.env"\n',
        LEADING_DOLLAR_PASSWORD,
        install_dir=tmp_path,
    )
    assert write.returncode == 0, write.stderr
    assert env_file.read_text(encoding="utf-8") == "SMTP_PASSWORD=$$UsErPassWoRd\n"

    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n"
        "  probe:\n"
        "    image: alpine\n"
        "    environment:\n"
        "      SMTP_PASSWORD: ${SMTP_PASSWORD:-}\n",
        encoding="utf-8",
    )

    env = {k: v for k, v in os.environ.items() if k != "SMTP_PASSWORD"}
    env["COMPOSE_PROJECT_NAME"] = "preloop-oss-smtp-dollar-test"
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "--env-file",
            str(env_file),
            "config",
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=30,
    )
    combined = f"{proc.stdout}\n{proc.stderr}"
    assert COMPOSE_WARN_LEAK not in combined
    assert proc.returncode == 0, proc.stderr

    data = json.loads(proc.stdout)
    raw = data["services"]["probe"]["environment"]["SMTP_PASSWORD"]
    assert raw, "SMTP_PASSWORD was interpolated to empty"
    assert compose_env_unescape(raw) == LEADING_DOLLAR_PASSWORD
