"""The missing-workflow warning only names real require_approval rules (#2558)."""

import pytest
import yaml

from preloop.services.policy.loader import load_policy_from_string


def _warnings(action: str, workflow: str | None = None) -> list[str]:
    """Validate a one-tool policy and return its warnings.

    Args:
        action: Action of the tool's only condition.
        workflow: Optional approval workflow for the tool.

    Returns:
        The validation warnings.
    """
    tool: dict = {
        "name": "Bash",
        "source": "builtin",
        "conditions": [{"expression": "args.command == 'rm'", "action": action}],
    }
    data: dict = {"version": "1.0", "metadata": {"name": "Example"}, "tools": [tool]}
    if workflow:
        tool["approval_workflow"] = workflow
        data["approval_workflows"] = [{"name": workflow}]
    policy, result = load_policy_from_string(yaml.safe_dump(data))
    assert policy is not None, result.errors
    return result.warnings


@pytest.mark.parametrize("action", ["deny", "allow"])
def test_no_warning_without_require_approval(action: str) -> None:
    """A deny-only or allow-only tool needs no approval workflow."""
    assert not any("approval_workflow" in w for w in _warnings(action))


def test_warning_for_require_approval_without_workflow() -> None:
    """A require_approval condition without a workflow is still reported."""
    warnings = _warnings("require_approval")
    assert any(
        "Bash" in w and "require_approval" in w and "approval_workflow" in w
        for w in warnings
    )


def test_no_warning_when_workflow_set() -> None:
    """A require_approval condition with a workflow is fine."""
    assert not any(
        "no approval_workflow" in w for w in _warnings("require_approval", "review")
    )
