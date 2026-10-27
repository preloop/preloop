"""Clone tracker selection survives trigger repository auto-detection."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.agents.container import ContainerAgentExecutor
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

REPO_URL = "https://github.com/example-org/example-repo.git"


def _orchestrator(
    repositories: list[dict], *, triggered: bool = True
) -> FlowExecutionOrchestrator:
    event = (
        {
            "source": "github",
            "tracker_id": "trigger-tracker",
            "project_id": "permitted-project",
            "payload": {
                "repository": {
                    "full_name": "example-org/example-repo",
                    "clone_url": REPO_URL,
                }
            },
        }
        if triggered
        else {}
    )
    orchestrator = FlowExecutionOrchestrator(
        db=MagicMock(),
        flow_id="flow",
        trigger_event_data=event,
        nats_client=AsyncMock(),
    )
    orchestrator.flow = SimpleNamespace(
        trigger_project_ids=["permitted-project"],
        git_clone_config={
            "enabled": True,
            "publication_mode": "legacy",
            "repositories": repositories,
        },
    )
    return orchestrator


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manual", "auto-detect", "explicit-project"])
async def test_configured_clone_tracker_wins_over_trigger_credential(mode: str) -> None:
    entry = {
        "tracker_id": "clone-tracker",
        "project_id": "permitted-project" if mode == "explicit-project" else None,
        "repository_url": None,
        "clone_path": "/workspace/source",
        "branch": "release",
    }
    orchestrator = _orchestrator([entry], triggered=mode != "manual")
    original = deepcopy(orchestrator.flow.git_clone_config)
    context = {
        "git_clone_config": orchestrator._git_clone_config_for_trigger(),
        "trigger_project_id": "permitted-project",
    }

    async def credentials(tracker_id: str) -> dict:
        return {"token": f"dummy-{tracker_id}", "tracker_type": "github"}

    with (
        patch.object(orchestrator, "_get_tracker_credentials_by_id", credentials),
        patch.object(
            orchestrator, "_resolve_project_tracker_id", return_value="trigger-tracker"
        ),
    ):
        await orchestrator._attach_git_credentials(context)

    repository = context["git_clone_config"]["repositories"][0]
    executor = ContainerAgentExecutor(agent_type="codex", config={}, image="test")
    assert executor._resolve_repository_token(repository, context) == (
        "dummy-clone-tracker",
        "github",
        None,
    )
    assert repository["tracker_id"] == "clone-tracker"
    assert repository["clone_path"] == "/workspace/source"
    assert repository["branch"] == "release"
    if mode != "manual":
        assert repository["project_id"] == "permitted-project"
        assert repository["repository_url"] == REPO_URL
    assert orchestrator.flow.git_clone_config == original


def test_explicit_match_takes_precedence_over_auto_detect_entry() -> None:
    orchestrator = _orchestrator(
        [
            {"tracker_id": "auto-tracker"},
            {"tracker_id": "explicit-tracker", "project_id": "permitted-project"},
        ]
    )
    clone = orchestrator._git_clone_config_for_trigger()
    assert clone["repositories"][0]["tracker_id"] == "explicit-tracker"


def test_other_repository_tracker_is_not_used_as_auto_detect_default() -> None:
    orchestrator = _orchestrator(
        [{"tracker_id": "other-tracker", "project_id": "other-project"}]
    )
    clone = orchestrator._git_clone_config_for_trigger()
    assert clone["repositories"] == [
        {
            "project_id": "permitted-project",
            "clone_path": "/workspace",
            "repository_url": REPO_URL,
        }
    ]


def test_auto_detect_entry_does_not_authorize_an_unselected_project() -> None:
    orchestrator = _orchestrator([{"tracker_id": "clone-tracker"}])
    orchestrator.trigger_event_data["project_id"] = "unselected-project"
    with (
        patch(
            "preloop.services.flow_trigger_service.FlowTriggerService._extract_project_id",
            return_value="unselected-project",
        ),
        patch.object(orchestrator, "_projects_matching_identity", return_value=[]),
    ):
        assert orchestrator._git_clone_config_for_trigger()["repositories"] == []
