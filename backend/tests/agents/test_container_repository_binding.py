"""Container credential and template handling for a bound Jira execution."""

from unittest.mock import patch

import pytest

from preloop.agents.container import ContainerAgentExecutor, interpolate_git_config_text


def _executor() -> ContainerAgentExecutor:
    return ContainerAgentExecutor(agent_type="codex", config={}, image="test")


def _context(**extra):
    context = {
        "git_clone_config": {"enabled": True},
        "trigger_tracker_id": "jira-tracker",
        "trigger_project_id": "jira-project",
        "git_credentials_map": {
            "jira-tracker": {"token": "jira-secret", "tracker_type": "jira"},
            "github-tracker": {"token": "host-token", "tracker_type": "github"},
        },
    }
    context.update(extra)
    return context


def test_bound_run_uses_only_the_host_tracker_credential() -> None:
    executor = _executor()
    context = _context(repository_binding={"repository": "acme/api"})
    with patch.object(
        executor,
        "_get_token_from_project",
        side_effect=AssertionError("trigger project lookup forbidden"),
    ):
        assert executor._resolve_repository_token(
            {"tracker_id": "github-tracker"}, context
        ) == ("host-token", "github")
        # A missing host credential is missing: never the Jira token.
        context["git_credentials_map"].pop("github-tracker")
        assert executor._resolve_repository_token(
            {"tracker_id": "github-tracker"}, context
        ) == (None, None)


def test_unbound_run_still_falls_back_to_the_trigger_tracker() -> None:
    """A code-host trigger keeps its existing fallback to its own tracker."""
    executor = _executor()
    context = _context(trigger_tracker_id="github-tracker")
    token, tracker_type = executor._resolve_repository_token({}, context)
    assert (token, tracker_type) == ("host-token", "github")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"issue": {"key": "PROJ-1", "fields": {"summary": "Fix login"}}}, "Fix login"),
        ({"issue": {"title": "GitHub title"}}, "GitHub title"),
        ({"object_attributes": {"title": "GitLab title"}}, "GitLab title"),
    ],
)
def test_title_placeholder_reads_the_jira_summary(payload, expected) -> None:
    template = "Implements: {{trigger_event.payload.object_attributes.title}}"
    assert interpolate_git_config_text(template, {"payload": payload}) == (
        f"Implements: {expected}"
    )
