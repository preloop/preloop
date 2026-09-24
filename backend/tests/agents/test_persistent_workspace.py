"""Persistent workspace metadata matches the container clone identity."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import yaml

from preloop.agents.agent_control import AgentControlExecutor
from preloop.services.persistent_workspace import (
    ephemeral_clone_identity,
    workspace_metadata,
    workspace_mode,
)
from preloop.services.prompt_resolvers.base import ResolverContext
from preloop.services.prompt_resolvers.workspace import WorkspaceResolver

SHA = "a" * 40
PR_TRIGGER = {
    "source": "github",
    "payload": {
        "repository": {
            "full_name": "example/repo",
            "clone_url": "https://github.com/example/repo.git",
            "default_branch": "main",
        },
        "pull_request": {
            "number": 7,
            "head": {"ref": "feature", "sha": SHA},
            "base": {"ref": "main"},
        },
    },
}
CLONE_CONFIG = {
    "enabled": True,
    "repositories": [{"repository_url": "https://github.com/example/repo.git"}],
    "source_branch": "main",
    "clone_depth": 1,
    "submodules": True,
}


def test_workspace_matches_container_clone_identity() -> None:
    identity = ephemeral_clone_identity(CLONE_CONFIG, PR_TRIGGER)
    workspace = workspace_metadata(
        git_clone_config=CLONE_CONFIG, trigger_event_data=PR_TRIGGER
    )
    assert identity is not None
    assert workspace["mode"] == "persistent_checkout"
    for key, value in identity.items():
        assert workspace[key] == value
    assert workspace["repository_url"] == "https://github.com/example/repo.git"
    assert workspace["repository_slug"] == "example/repo"
    assert workspace["default_branch"] == "main"
    assert workspace["sha"] == SHA
    assert workspace["pr_number"] == 7
    assert workspace["fetch_ref"] == "pull/7/head"
    assert "clone_depth" not in workspace
    assert "submodules" not in workspace
    assert "token" not in workspace
    assert "@" not in (workspace["repository_url"] or "")


def test_unsafe_slug_is_clone_less() -> None:
    trigger = {
        "source": "github",
        "payload": {
            "repository": {
                "full_name": "acme/my repo",
                "clone_url": "https://github.com/acme/my%20repo.git",
            }
        },
    }
    assert ephemeral_clone_identity(CLONE_CONFIG, trigger) is None
    assert workspace_metadata(
        git_clone_config=CLONE_CONFIG, trigger_event_data=trigger
    ) == {"mode": "clone_less"}
    assert (
        workspace_mode(
            agent_config={"execution_path": "persistent"},
            git_clone_config=CLONE_CONFIG,
            trigger_event_data=trigger,
        )
        == "clone_less"
    )


def test_clone_disabled_is_clone_less() -> None:
    workspace = workspace_metadata(
        git_clone_config={"enabled": False},
        trigger_event_data=PR_TRIGGER,
    )
    assert workspace == {"mode": "clone_less"}


def test_ephemeral_prompt_context_mode() -> None:
    assert (
        workspace_mode(
            agent_config={"execution_path": "ephemeral"},
            git_clone_config=CLONE_CONFIG,
            trigger_event_data=PR_TRIGGER,
        )
        == "ephemeral"
    )
    assert (
        workspace_mode(
            agent_config={},
            git_clone_config=CLONE_CONFIG,
            trigger_event_data=PR_TRIGGER,
        )
        == "ephemeral"
    )


@pytest.mark.asyncio
async def test_workspace_resolver_renders_mode() -> None:
    resolver = WorkspaceResolver()
    context = ResolverContext(
        db=MagicMock(),
        trigger_event_data={},
        flow_id="flow",
        execution_id="exec",
        workspace_mode="persistent_checkout",
    )
    assert await resolver.resolve("mode", context) == "persistent_checkout"


@pytest.mark.asyncio
async def test_executor_metadata_carries_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = uuid4()
    agent = SimpleNamespace(
        id=uuid4(),
        account_id=account_id,
        display_name="Review node",
        lifecycle_state="active",
        agent_kind="openclaw",
        session_source_type="openclaw",
        runtime_session_id=uuid4(),
        control_last_heartbeat_at=None,
    )
    captured: dict = {}

    async def fake_dispatch(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(command_id="cmd-1", local_delivery=True, subject=None)

    monkeypatch.setattr(
        "preloop.agents.agent_control.agent_has_control_config",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.control_heartbeat_is_fresh",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.create_command_history_session",
        lambda *args, **kwargs: SimpleNamespace(id=uuid4()),
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_runtime_session_activity.log_agent_control_message",
        MagicMock(),
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_flow_execution.bind_agent_control_command",
        MagicMock(),
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_managed_agent.get_for_account",
        lambda *args, **kwargs: agent,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.dispatch_operator_message",
        fake_dispatch,
    )
    executor = AgentControlExecutor(
        "codex",
        {"execution_path": "persistent", "target_agent_id": str(agent.id)},
        db=MagicMock(),
        account_id=account_id,
        flow=SimpleNamespace(
            timeout_seconds=1800,
            name="Persistent review",
            account_id=account_id,
            git_clone_config=CLONE_CONFIG,
            agent_config={"execution_path": "persistent"},
        ),
        execution=SimpleNamespace(id=uuid4(), account_id=account_id),
    )
    await executor.start(
        {
            "prompt": "Review the pull request",
            "execution_id": str(executor.execution.id),
            "flow_id": str(uuid4()),
            "flow_name": "Persistent review",
            "account_id": account_id,
            "trigger_event_data": PR_TRIGGER,
        }
    )
    assert captured["metadata"]["workspace"]["mode"] == "persistent_checkout"
    assert captured["metadata"]["workspace"]["sha"] == SHA
    assert captured["metadata"]["workspace"]["repository_slug"] == "example/repo"


def _reviewer_prompt() -> str:
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "presets"
        / "002-pull-request-reviewer.yaml"
    )
    data = yaml.safe_load(path.read_text())
    return data["prompt_template"]


def _render(mode: str) -> str:
    return _reviewer_prompt().replace("{{workspace.mode}}", mode)


def test_preset_renders_persistent_checkout_in_cwd() -> None:
    rendered = _render("persistent_checkout")
    assert "persistent_checkout" in rendered
    assert "current working directory" in rendered
    assert "{{workspace.mode}}" not in rendered


def test_preset_renders_clone_less_from_tracker() -> None:
    rendered = _render("clone_less")
    assert "skip the git checks" in rendered
    assert "diff came from the tracker" in rendered


def test_preset_renders_ephemeral_clone_checks() -> None:
    rendered = _render("ephemeral")
    assert "must exist in the clone" in rendered
    assert "git rev-parse HEAD` in the clone" in rendered


def test_ssh_user_is_kept_and_password_is_stripped() -> None:
    config = {
        "enabled": True,
        "repositories": [
            {"repository_url": "ssh://git:secret@github.com/example/repo.git"}
        ],
    }
    identity = ephemeral_clone_identity(config, PR_TRIGGER)
    assert identity is not None
    assert identity["repository_url"] == "ssh://git@github.com/example/repo.git"


def test_multiple_repositories_stay_clone_less() -> None:
    config = {
        "enabled": True,
        "repositories": [
            {"repository_url": "https://github.com/example/one.git"},
            {"repository_url": "https://github.com/example/two.git"},
        ],
    }
    assert ephemeral_clone_identity(config, PR_TRIGGER) is None
    assert workspace_metadata(
        git_clone_config=config, trigger_event_data=PR_TRIGGER
    ) == {"mode": "clone_less"}


def test_persistent_preset_does_not_hardcode_workspace_path() -> None:
    from pathlib import Path

    from preloop.flow_presets import PRESET_SLUGS, supports_persistent_for_slug

    root = Path(__file__).resolve().parents[2] / "presets"
    checked = 0
    for path in root.glob("*.yaml"):
        data = yaml.safe_load(path.read_text())
        slug = data.get("slug")
        if not isinstance(slug, str) or not supports_persistent_for_slug(slug):
            continue
        # A supported preset may mention the container path only on the
        # ephemeral branch. The escape is that line, not the whole slug.
        for line in path.read_text().splitlines():
            if "/workspace" in line:
                assert "ephemeral" in line, path.name
        checked += 1
    assert checked >= 1
    assert supports_persistent_for_slug("pull-request-reviewer") is True
    assert supports_persistent_for_slug("issue-triage-assistant") is False
    assert supports_persistent_for_slug("observe-eval") is False
    assert supports_persistent_for_slug("sbom-verify") is False
    assert supports_persistent_for_slug("sbom-exploit-check") is False
    assert supports_persistent_for_slug("automated-issue-implementation") is False
    assert supports_persistent_for_slug("portfolio-review") is False
    assert set(PRESET_SLUGS)
    root = Path(__file__).resolve().parents[2] / "presets"
    for path in root.glob("*.yaml"):
        data = yaml.safe_load(path.read_text())
        assert "supports_persistent" in data, path.name
        assert isinstance(data["supports_persistent"], bool), path.name


def test_persistent_preset_rejection_uses_catalog_name() -> None:
    from preloop.services.persistent_workspace import persistent_preset_rejection

    persistent = {"execution_path": "persistent"}
    assert persistent_preset_rejection(persistent, "Pull Request Reviewer") is None
    reason = persistent_preset_rejection(persistent, "Automated Issue Implementation")
    assert reason is not None
    assert "does not support persistent execution" in reason
    assert (
        persistent_preset_rejection(
            {"execution_path": "ephemeral"}, "Automated Issue Implementation"
        )
        is None
    )


def test_workspace_metadata_failure_degrades_to_clone_less(monkeypatch) -> None:
    from preloop.agents import agent_control

    def _boom(**_kwargs):
        raise RuntimeError("workspace helper failed")

    monkeypatch.setattr(agent_control, "workspace_metadata", _boom)
    metadata = agent_control._flow_dispatch_metadata({"flow_id": "flow-1"})
    assert metadata["workspace"] == {"mode": "clone_less"}


def test_create_flow_rejects_persistent_opt_out_preset(db_session, test_user) -> None:
    """POST /flows returns 422 when a catalog preset opts out of persistent."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from preloop.api.auth import get_current_active_user
    from preloop.api.endpoints.flows import router
    from preloop.models.crud import crud_flow
    from preloop.models.db.session import get_db_session
    from preloop.models.schemas.flow import FlowCreate

    preset = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Automated Issue Implementation",
            prompt_template="implement the issue",
            agent_type="codex",
            agent_config={},
            is_preset=True,
        ),
        account_id=test_user.account_id,
    )
    db_session.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_user
    client = TestClient(app)
    response = client.post(
        "/flows",
        json={
            "name": "Persistent copy",
            "prompt_template": "implement the issue",
            "agent_type": "codex",
            "agent_config": {"execution_path": "persistent"},
            "source_preset_id": str(preset.id),
        },
    )
    assert response.status_code == 422, response.text
    assert "does not support persistent execution" in response.text
