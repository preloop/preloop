"""Synthetic git repositories for repo-audit tests.

Uses dummy passwords and example.invalid pointers only. Never real secret
values, MQTT passwords, or product-specific firmware nouns.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict

import pytest

# Synthetic credential used only inside the fixture history. Tests assert
# this value never appears in tool output.
SYNTHETIC_PASSWORD = "SYNTH-EXAMPLE-PASSWORD-0001"
JUNK_FILENAME = "should not be public leftover notes"
README_POINTER = (
    "The device CA private key is documented at "
    "https://keys.example.invalid/ca.key and must not be fetched.\n"
)
WORKFLOW = """\
name: ci
on:
  pull_request_target:
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
"""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture()
def synthetic_history_repo(tmp_path: Path) -> Dict[str, object]:
    """Build a repo that added, then removed, a dummy password at a known SHA."""
    repo = tmp_path / "product"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture Bot")

    _write(repo, "README.md", "example product\n")
    _write(repo, "certs/example.pem", "not-a-real-cert\n")
    _git(repo, "add", "README.md", "certs/example.pem")
    _git(repo, "commit", "-m", "initial tree")

    _write(
        repo,
        "config/service.env",
        f"password={SYNTHETIC_PASSWORD}\n",
    )
    _git(repo, "add", "config/service.env")
    add = _git(repo, "commit", "-m", "add service password for local tests")
    add_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "rm", "config/service.env")
    _git(repo, "commit", "-m", "remove leftover service password from tree")
    remove_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "rm", "certs/example.pem")
    _git(repo, "commit", "-m", "delete expired example certificate")
    deleted_pem_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _write(repo, JUNK_FILENAME, "pager fragment leftover\n")
    _write(repo, "README.md", README_POINTER)
    _write(repo, ".github/workflows/ci.yml", WORKFLOW)
    _write(repo, ".github/workflows/legacy.yml.off", "name: leftover\n")
    _git(
        repo,
        "add",
        JUNK_FILENAME,
        "README.md",
        ".github/workflows/ci.yml",
        ".github/workflows/legacy.yml.off",
    )
    _git(repo, "commit", "-m", "docs and leftover hygiene")
    head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    return {
        "repo": repo,
        "add_sha": add_sha,
        "remove_sha": remove_sha,
        "deleted_pem_sha": deleted_pem_sha,
        "head_sha": head_sha,
        "add_stdout": add.stdout,
    }
