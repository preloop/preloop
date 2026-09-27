"""The shipped release backport preset (issue #961)."""

from pathlib import Path

import yaml

from preloop.models.schemas.flow import FlowCreate
from preloop.services.backport import backport_event_matches, resolve_backport_plan

PRESET = Path(__file__).resolve().parents[1] / "presets" / "018-release-backport.yaml"


def _load() -> dict:
    return yaml.safe_load(PRESET.read_text())


def test_preset_is_a_valid_flow_with_backport_enabled():
    data = _load()
    fields = {k: v for k, v in data.items() if k not in {"slug", "supports_persistent"}}
    flow = FlowCreate(**fields)
    assert flow.git_clone_config is not None
    assert flow.git_clone_config.create_pull_request is False
    plan = resolve_backport_plan(data["git_clone_config"])
    assert plan is not None
    assert plan.source_branch not in plan.target_branches
    assert len(plan.target_branches) == 2


def test_preset_triggers_on_github_and_gitlab_merges_only():
    data = _load()
    assert sorted(data["trigger_event_types"]) == [
        "merge_request_merged",
        "pull_request_merged",
    ]
    config = data["git_clone_config"]
    source = config["backport"]["source_branch"]
    merged = {
        "type": "pull_request_merged",
        "payload": {"pull_request": {"base": {"ref": source}}},
    }
    elsewhere = {
        "type": "pull_request_merged",
        "payload": {"pull_request": {"base": {"ref": "main"}}},
    }
    assert backport_event_matches(config, merged)
    assert not backport_event_matches(config, elsewhere)


def test_preset_carries_the_bitbucket_note():
    text = PRESET.read_text()
    assert "Bitbucket" in text and "#955" in text


def test_preset_grants_no_tools():
    data = _load()
    assert data["allowed_mcp_tools"] == []
    assert data["allowed_mcp_servers"] == []
