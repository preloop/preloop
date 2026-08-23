"""Synthetic git repositories for scanner-wrapper tests.

Uses dummy passwords and example.invalid pointers only. Never real secret
values or product-specific firmware nouns.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict

import pytest

# Synthetic credential used only inside the fixture history. Tests assert
# this value never appears in tool output.
SYNTHETIC_PASSWORD = "SYNTH-EXAMPLE-PASSWORD-0001"


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
    """Build a tiny repo that once contained a dummy password."""
    repo = tmp_path / "product"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture Bot")

    _write(repo, "README.md", "example product\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial tree")

    _write(repo, "config/service.env", f"password={SYNTHETIC_PASSWORD}\n")
    _git(repo, "add", "config/service.env")
    _git(repo, "commit", "-m", "add service password for local tests")
    add_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "rm", "config/service.env")
    _git(repo, "commit", "-m", "remove leftover service password from tree")
    head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    return {
        "repo": repo,
        "add_sha": add_sha,
        "head_sha": head_sha,
    }
