"""Daily CI and QA review preset (023).

Pins what the preset promises: one server-side collector call instead of
agent-side CI paging, a failure ledger across runs, the knobs (value bar,
pull request cap, protected branches, critical-path jobs, budget,
report-only mode for the first two weeks, repositories), an early stop
when nothing changed, publication left to the platform, and the guardrails
against greening tests by skipping them, raising timeouts, or loading
shared environments. Deterministic: parses the shipped YAML, no agent, no
network.
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


KNOBS = {
    "ci_repositories": [],
    "ci_window_hours": 24,
    "critical_path_jobs": [],
    "protected_branches": [],
    "report_only": "auto",
    "report_only_days": 14,
    "value_threshold_dev_minutes_per_week": 60,
    "value_threshold_runner_minutes_per_week": 60,
    "max_open_pull_requests": 2,
    "focus": "",
}


def test_schedule_payload_carries_the_knobs(preset: dict[str, Any]) -> None:
    """Every knob is in the payload with a generic default."""
    schedule = preset["schedule_config"]
    assert preset["trigger_event_types"] == ["schedule"]
    assert schedule["type"] == "daily"
    assert schedule["at"] == "06:00"
    assert schedule["payload"] == KNOBS


def test_prompt_reads_every_knob(prompt: str) -> None:
    """The knobs only reach the agent through the payload placeholder."""
    assert "{{trigger_event.payload}}" in prompt
    for knob in KNOBS:
        assert knob in prompt, knob
    assert "precheck" in prompt


def test_each_run_is_capped_in_spend_and_time(preset: dict[str, Any]) -> None:
    """The budget and wall-clock caps are part of the preset, not the prompt."""
    assert 0 < preset["agent_config"]["limits"]["max_usd"] <= 25
    assert 600 <= preset["timeout_seconds"] <= 7200


def test_tools_are_the_collector_and_read_only_tracker_tools(
    preset: dict[str, Any],
) -> None:
    """CI is read by the collector; the platform opens the one pull request."""
    from preloop.api.endpoints.tools import BUILTIN_TOOLS

    tools = {tool["name"] for tool in preset["allowed_mcp_tools"]}
    assert tools == {
        "ci_health_report",
        "get_flow_state",
        "search_issues",
        "get_issue",
        "get_pull_request",
    }
    assert tools <= {tool["name"] for tool in BUILTIN_TOOLS}
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


def test_agent_never_reads_ci_with_a_token(prompt: str) -> None:
    """The collector holds the credential; the prompt names no token."""
    from preloop.utils.git_credentials import git_token_env_var

    assert git_token_env_var(0) not in prompt
    assert "GITHUB_TOKEN" not in prompt
    assert "ci_health_report is the only CI reader" in prompt
    assert prompt.index("Call ci_health_report first") < prompt.index("## 3. Assess")


def test_published_body_comes_from_the_result(prompt: str) -> None:
    """The PR body with the numbers is the agent's result metadata."""
    from preloop.utils.pr_metadata import read_result_metadata

    assert 'pull_request {"title", "body"}' in prompt
    raw = b'{"pull_request": {"title": "t", "body": "before 3/20 after 0/20"}}'
    _, body, _ = read_result_metadata(raw)
    assert body == "before 3/20 after 0/20"


def test_result_feeds_the_value_summary(prompt: str) -> None:
    """The fields the ledger and the weekly summary read are instructed."""
    for field in (
        "estimated_runner_minutes_saved_per_week",
        "estimated_dev_minutes_saved_per_week",
        "change.signatures",
        "early_stop",
    ):
        assert field in prompt, field
    # The collector reads exactly these keys from an execution result.
    from preloop.services.ci_health.tool import published_pull_requests

    published = published_pull_requests(
        [
            {
                "id": "e1",
                "end_time": None,
                "start_time": None,
                "result": {
                    "pr_url": "https://github.com/example-org/app/pull/1",
                    "change": {
                        "estimated_runner_minutes_saved_per_week": 30,
                        "signatures": ["abc"],
                    },
                },
            }
        ]
    )
    assert published[0]["estimated_minutes_saved_per_week"] == 30
    assert published[0]["signatures"] == ["abc"]


@pytest.mark.parametrize(
    "phrase",
    [
        "Never push to, rebase or otherwise touch an existing pull request",
        "approved and mergeable",
        "skipping it, marking it xfail",
        "raising a timeout",
        "no load against staging or production",
        "drop any candidate an open item already covers",
        "Measure before and after",
        "pre-commit run --files",
        "PRELOOP_DISABLE_TELEMETRY=true",
        "Playwright",
        "If changed_since_last_run is false",
        "If mode.report_only is true: change nothing",
        "max_open_pull_requests open pull requests",
        "If no candidate clears the value bar: change nothing",
        'Do not mark a signature "fixed" yourself',
    ],
)
def test_prompt_states_the_guardrails(prompt: str, phrase: str) -> None:
    """Each guardrail and decision rule is spelled out to the agent."""
    assert phrase in prompt


def test_prompt_defines_the_report_and_result(prompt: str) -> None:
    """The report and the machine result are both instructed."""
    assert "/workspace/evidence/ci-qa-report.md" in prompt
    assert '"status": "success" | "failure"' in prompt


def test_preset_names_no_deployment(preset: dict[str, Any]) -> None:
    """The preset is generic: no organisation, repository or job names."""
    text = PRESET_PATH.read_text().lower()
    for word in ("spacecode", "preloop-ee", "preloop/preloop"):
        assert word not in text
