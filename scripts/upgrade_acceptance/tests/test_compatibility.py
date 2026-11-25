"""Regressions for the isolated real-runner compatibility fixture."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compatibility import host_profile  # noqa: E402


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
