"""The legacy flow action rejects restricted CI tokens, including padded ones."""

from __future__ import annotations

import os
import subprocess

import yaml  # type: ignore[import-untyped]

from tests.ci_workflow import REPO_ROOT

ACTION = REPO_ROOT / ".github" / "actions" / "run-flow" / "action.yml"


def _check_inputs(token: str) -> subprocess.CompletedProcess[str]:
    document = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    script = next(
        step["run"]
        for step in document["runs"]["steps"]
        if step.get("name") == "Check inputs"
    )
    env = os.environ.copy()
    env.update(
        {
            "PRELOOP_ACTION_TOKEN": token,
            "MODE": "hosted",
            "LABELS": "",
            "PAYLOAD": "",
            "RUNNER_OS": "Linux",
        }
    )
    return subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_whitespace_around_a_ci_token_is_still_rejected() -> None:
    for token in ("ci_synthetic", " ci_synthetic", "ci_ synthetic", "\tci_synthetic\n"):
        result = _check_inputs(token)
        assert result.returncode == 1, token
        assert "restricted CI tokens" in result.stdout


def test_a_non_ci_token_is_not_rejected() -> None:
    for token in ("plt_synthetic", "  plt_synthetic  "):
        result = _check_inputs(token)
        assert result.returncode == 0, (token, result.stdout, result.stderr)
