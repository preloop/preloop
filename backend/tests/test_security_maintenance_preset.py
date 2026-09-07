"""Pin the conservative security-maintenance implementation overlay."""

from pathlib import Path

import pytest
import yaml

PRESET_FILE = "014-security-maintenance-implementation.yaml"
PRESETS_DIR = Path(__file__).resolve().parents[1] / "presets"
BANNED_TOOLS = (
    "request_approval",
    "get_approval_status",
    "approval_workflow",
    "create_pull_request",
)


@pytest.fixture
def preset():
    return yaml.safe_load((PRESETS_DIR / PRESET_FILE).read_text())


def test_isolated_gate_and_no_agent_approval(preset):
    names = [item["name"] for item in preset["allowed_mcp_tools"]]
    assert "request_approval" not in names
    for banned in BANNED_TOOLS:
        assert banned not in names
    git = preset["git_clone_config"]
    assert git["publication_mode"] == "isolated"
    assert git["verification"]["mode"] == "gate"
    assert git["create_pull_request"] is True
    assert "does not backport" in preset["description"]
    assert "backport" not in preset["prompt_template"].lower()
