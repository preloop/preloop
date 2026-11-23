"""The trigger repository is cloned only when the Git workspace is enabled.

A GitLab merge request event resolves to a synced project. With cloning off
the run gets no repository; with it on, the entry carries the project's
default branch so the clone never assumes ``main``.
"""

from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

REPO_URL = "https://gitlab.example.com/group/example.git"


def _orchestrator(
    git_clone_config: Optional[dict[str, Any]],
) -> FlowExecutionOrchestrator:
    event = {
        "source": "gitlab",
        "tracker_id": "trigger-tracker",
        "project_id": "permitted-project",
        "payload": {
            "object_kind": "note",
            "project": {
                "path_with_namespace": "group/example",
                "git_http_url": REPO_URL,
            },
        },
    }
    orchestrator = FlowExecutionOrchestrator(
        db=MagicMock(),
        flow_id="flow",
        trigger_event_data=event,
        nats_client=AsyncMock(),
    )
    orchestrator.flow = SimpleNamespace(
        account_id="account",
        trigger_project_ids=["permitted-project"],
        git_clone_config=git_clone_config,
    )
    return orchestrator


def _project(default_branch: Optional[str]) -> SimpleNamespace:
    meta = {"default_branch": default_branch} if default_branch else {}
    return SimpleNamespace(meta_data=meta, settings={})


@pytest.mark.parametrize(
    "config",
    [
        {"enabled": False, "repositories": []},
        {"enabled": False, "repositories": [], "source_branch": "main"},
        {"enabled": False},
    ],
)
def test_disabled_flow_gets_no_trigger_repository(config: dict[str, Any]) -> None:
    orchestrator = _orchestrator(config)
    with patch("preloop.models.crud.crud_project.get", return_value=_project("master")):
        effective = orchestrator._git_clone_config_for_trigger()

    assert effective.get("repositories") in (None, [])
    assert effective["enabled"] is False


def test_enabled_flow_clones_the_trigger_project_on_its_default_branch() -> None:
    orchestrator = _orchestrator(
        {"enabled": True, "repositories": [], "source_branch": None}
    )
    with patch("preloop.models.crud.crud_project.get", return_value=_project("master")):
        effective = orchestrator._git_clone_config_for_trigger()

    [entry] = effective["repositories"]
    assert entry["project_id"] == "permitted-project"
    assert entry["default_branch"] == "master"
    # The stored flow config is never modified.
    assert "default_branch" not in str(orchestrator.flow.git_clone_config)


def test_explicit_repository_gets_its_project_default_branch() -> None:
    orchestrator = _orchestrator(
        {
            "enabled": True,
            "repositories": [{"project_id": "permitted-project", "tracker_id": "t"}],
        }
    )
    with patch(
        "preloop.models.crud.crud_project.get", return_value=_project("develop")
    ):
        effective = orchestrator._git_clone_config_for_trigger()

    assert effective["repositories"][0]["default_branch"] == "develop"


def test_unknown_default_branch_leaves_the_entry_unannotated() -> None:
    orchestrator = _orchestrator({"enabled": True, "repositories": []})
    with patch("preloop.models.crud.crud_project.get", return_value=_project(None)):
        effective = orchestrator._git_clone_config_for_trigger()

    assert "default_branch" not in effective["repositories"][0]


def test_preset_runner_does_not_guess_main() -> None:
    from preloop.services.preset_runner import _default_branch

    assert _default_branch(SimpleNamespace(settings={}, meta_data={})) is None
    assert (
        _default_branch(
            SimpleNamespace(settings={}, meta_data={"default_branch": "master"})
        )
        == "master"
    )


def test_api_flow_without_source_branch_is_not_pinned_to_main() -> None:
    from preloop.models.schemas.flow import GitCloneConfig

    assert GitCloneConfig(enabled=True).source_branch is None
    assert GitCloneConfig(enabled=True, source_branch="release").source_branch == (
        "release"
    )
