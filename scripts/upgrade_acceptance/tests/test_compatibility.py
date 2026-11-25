"""Regressions for the isolated real-runner compatibility fixture."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compatibility import host_profile, unchanged_object  # noqa: E402


def test_host_profile_has_a_runnable_executable_and_existing_workspace(
    tmp_path: Path,
) -> None:
    """The runner validates the workspace before starting its executable."""
    env: dict[str, str] = {}
    host_profile(tmp_path, env, "synthetic-acceptance-token")
    profiles = json.loads(Path(env["PRELOOP_RUNNER_HOST_PROFILES"]).read_text())
    profile = profiles["profiles"][0]
    assert Path(profile["workspace_root"]).is_dir()
    assert os.access(profile["executable"], os.X_OK)


@pytest.mark.parametrize("status", [401, 404, 500])
def test_identical_errors_do_not_prove_a_write_was_refused(status: int) -> None:
    """A missing or unreadable fixture must not produce false no-write evidence."""
    error = SimpleNamespace(status_code=status, json=lambda: {"detail": "error"})
    assert not unchanged_object(error, error)


def test_guard_readback_requires_identical_successful_content() -> None:
    """A guard must preserve the object, not merely produce two successful reads."""
    before = SimpleNamespace(status_code=200, json=lambda: {"enabled": False})
    after = SimpleNamespace(status_code=200, json=lambda: {"enabled": True})
    assert unchanged_object(before, before)
    assert not unchanged_object(before, after)
