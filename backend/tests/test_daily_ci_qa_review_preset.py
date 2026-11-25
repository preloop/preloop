"""Daily CI and QA review preset (023).

Pins what the preset promises: a daily schedule that feeds the previous
result back for a trend, a spend and wall-clock cap, read-only tracker
tools with publication left to the platform, and the guardrails against
greening tests by skipping them, raising timeouts, or loading shared
environments. Deterministic: parses the shipped YAML, no agent, no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from preloop.models import schemas

PRESET_PATH = (
    Path(__file__).resolve().parents[1] / "presets" / "023-daily-ci-qa-review.yaml"
)


@pytest.fixture(scope="module")
def preset() -> dict[str, Any]:
    """Load the shipped preset YAML.

    Returns:
        The parsed preset mapping.
    """
    return yaml.safe_load(PRESET_PATH.read_text())


@pytest.fixture(scope="module")
def prompt(preset: dict[str, Any]) -> str:
    """Return the prompt with whitespace collapsed for phrase matching.

    Args:
        preset: The parsed preset mapping.

    Returns:
        The normalized prompt text.
    """
    return " ".join(preset["prompt_template"].split())


def test_preset_validates_as_a_flow(preset: dict[str, Any]) -> None:
    """The preset is a valid FlowCreate payload, disabled until cloned."""
    data = {k: v for k, v in preset.items() if k not in {"slug"}}
    flow = schemas.FlowCreate(**data)
    assert flow.is_enabled is False
    assert flow.agent_type == "codex"


def test_runs_daily_with_previous_result_for_the_trend(
    preset: dict[str, Any],
) -> None:
    """A daily schedule that hands the last report to the next run."""
    schedule = preset["schedule_config"]
    assert preset["trigger_event_types"] == ["schedule"]
    assert schedule["type"] == "daily"
    assert schedule["at"] == "06:00"
    assert schedule["payload"]["previous_result_execution_id"] == "last"
    assert schedule["payload"]["ci_window_hours"] == 24


def test_each_run_is_capped_in_spend_and_time(preset: dict[str, Any]) -> None:
    """The budget and wall-clock caps are part of the preset, not the prompt."""
    assert 0 < preset["agent_config"]["limits"]["max_usd"] <= 25
    assert 600 <= preset["timeout_seconds"] <= 7200


def test_agent_has_only_read_tools_and_platform_publishes(
    preset: dict[str, Any],
) -> None:
    """The agent cannot write to the tracker; the platform opens one PR."""
    tools = {tool["name"] for tool in preset["allowed_mcp_tools"]}
    assert tools == {"search_issues", "get_issue", "get_pull_request"}
    clone = preset["git_clone_config"]
    assert clone["create_pull_request"] is True
    assert clone["publication_mode"] == "legacy"


def test_publication_mode_is_runnable_without_a_profile(
    preset: dict[str, Any],
) -> None:
    """Isolated publication needs a trusted gate profile and pinned image.

    The preset is generic, so it cannot ship a repository's profile; it must
    therefore not ask for isolated publication, which would refuse every run
    before the agent starts.
    """
    from preloop.services.verification import (
        pinned_verification_image,
        resolve_verification_policy,
    )

    clone = preset["git_clone_config"]
    policy = resolve_verification_policy(clone)
    image = (clone.get("verification") or {}).get("image", "")
    isolated_ready = (
        policy.mode == "gate"
        and policy.profile is not None
        and pinned_verification_image(image) is not None
    )
    assert clone["publication_mode"] != "isolated" or isolated_ready


def test_prompt_receives_the_schedule_payload(prompt: str) -> None:
    """The payload knobs only reach the agent through a prompt placeholder."""
    assert "{{trigger_event.payload}}" in prompt
    for knob in ("ci_window_hours", "ci_repositories", "focus"):
        assert knob in prompt


def test_prompt_names_the_credential_the_runtime_provides(prompt: str) -> None:
    """CI reads use the tracker token the container exports, GET only."""
    from preloop.utils.git_credentials import git_token_env_var

    assert git_token_env_var(0) in prompt
    assert "GITHUB_TOKEN" not in prompt
    assert "only ever issue GET requests" in prompt


def test_published_body_comes_from_the_result(prompt: str) -> None:
    """The PR body with the numbers is the agent's result metadata."""
    from preloop.utils.pr_metadata import read_result_metadata

    assert 'pull_request {"title", "body"}' in prompt
    raw = b'{"pull_request": {"title": "t", "body": "before 3/20 after 0/20"}}'
    _, body, _ = read_result_metadata(raw)
    assert body == "before 3/20 after 0/20"


@pytest.mark.parametrize(
    "phrase",
    [
        "Never push to, rebase or otherwise touch an existing pull request",
        "approved and mergeable",
        "skipping it, marking it xfail",
        "raising a timeout",
        "no load against staging or production",
        "one request at a time, never in parallel",
        "X-RateLimit-Remaining",
        "Do not start work an open item already covers",
        "Measure before and after",
        "pre-commit run --files",
        "PRELOOP_DISABLE_TELEMETRY=true",
        "Playwright",
    ],
)
def test_prompt_states_the_guardrails(prompt: str, phrase: str) -> None:
    """Each guardrail the preset promises is spelled out to the agent."""
    assert phrase in prompt


def test_prompt_defines_the_report_and_result(prompt: str) -> None:
    """The daily report and the machine result are both instructed."""
    assert "/workspace/evidence/ci-qa-report.md" in prompt
    assert '"status": "success" | "failure"' in prompt
    assert "previous/result.json" in prompt
