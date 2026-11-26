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

from compatibility import (  # noqa: E402
    host_profile,
    onboarded_gateway_token,
    persisted_native_result,
    unchanged_object,
)
from fixture import FixtureError  # noqa: E402


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


@pytest.mark.parametrize("key", [None, "", "  ", "account-key"])
def test_gateway_cannot_hide_a_missing_agent_credential(key: str | None) -> None:
    """A successful account-token request cannot prove agent onboarding."""
    with pytest.raises(FixtureError, match="independent agent key"):
        onboarded_gateway_token({"apiKey": key}, "account-key")


def test_gateway_uses_the_installed_agent_credential() -> None:
    assert (
        onboarded_gateway_token({"apiKey": "agent-key"}, "account-key") == "agent-key"
    )


@pytest.mark.parametrize("missing", ["status", "harness", "session_id"])
def test_flow_success_requires_the_persisted_native_result(missing: str) -> None:
    """Success status or streamed output alone cannot prove result persistence."""
    result = {
        "status": "success",
        "harness": "cursor_cli",
        "session_id": "synthetic-acceptance",
    }
    execution = {"status": "SUCCEEDED", "result": result}
    assert persisted_native_result(execution)
    del result[missing]
    assert not persisted_native_result(execution)
    assert not persisted_native_result({"status": "SUCCEEDED", "result": None})
