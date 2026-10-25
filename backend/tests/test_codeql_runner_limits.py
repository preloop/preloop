"""Guard CodeQL resource limits on shared self-hosted runners."""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/codeql.yml"


def test_codeql_limits_shared_runners_without_constraining_hosted() -> None:
    """Init limits cover extraction and analysis only on self-hosted runners."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["analyze"]["steps"]
    init = next(step for step in steps if step["name"] == "Initialize CodeQL")
    analyze = next(step for step in steps if step["name"] == "Perform CodeQL analysis")

    assert init["with"]["ram"] == (
        "${{ runner.environment == 'self-hosted' && '2560' || '' }}"
    )
    assert init["with"]["threads"] == (
        "${{ runner.environment == 'self-hosted' && '2' || '' }}"
    )
    assert "ram" not in analyze["with"]
    assert "threads" not in analyze["with"]


def test_javascript_codeql_sets_up_node_before_extraction() -> None:
    """The JavaScript extractor has Node on self-hosted ubuntu-latest runners."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["analyze"]["steps"]
    setup_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Set up Node.js for CodeQL"
    )
    setup = steps[setup_index]
    assert setup["if"] == "matrix.language == 'javascript-typescript'"
    assert setup["uses"] == (
        "actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1"
    )
    assert setup["with"]["node-version"] == "22"
    assert setup_index < next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Initialize CodeQL"
    )
