"""Pinned images and npm lockfiles for the open Scorecard alerts."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Dockerfiles that Scorecard reported as unpinned, and the lockfile their
# npm ci command installs.
PINNED = {
    "examples/sandboxed-agent-rig/agent/Dockerfile": (
        "examples/sandboxed-agent-rig/agent/package-lock.json"
    ),
    "backend/tests/fixtures/deepseek_interactive/Dockerfile": (
        "backend/tests/fixtures/deepseek_interactive/dsh/package-lock.json"
    ),
}


def test_flagged_dockerfiles_pin_images_and_use_npm_ci() -> None:
    """protects=Scorecard Pinned-Dependencies stays clear on these images;
    fails_when=a base image drops its digest or npm install comes back.
    """
    for dockerfile, lockfile in PINNED.items():
        text = (ROOT / dockerfile).read_text()
        from_lines = [
            line
            for line in text.splitlines()
            if line.startswith("FROM ") and "${" not in line
        ]
        assert from_lines, dockerfile
        for line in from_lines:
            assert "@sha256:" in line, line
        assert "npm install" not in text, dockerfile
        assert "npm ci" in text, dockerfile
        assert (ROOT / lockfile).is_file(), lockfile
