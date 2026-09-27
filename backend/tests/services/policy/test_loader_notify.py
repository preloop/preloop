"""Policy-as-code accepts the model I/O ``notify`` action (#959)."""

from __future__ import annotations

import yaml

from preloop.services.policy.loader import (
    export_policy_to_yaml,
    load_policy_from_string,
)

NOTIFY_YAML = """
version: "1.0"
metadata:
  name: Notify Policy
model_io:
  - id: notify-codename
    target: model.request
    description: Mentions of the internal codename
    conditions:
      - expression: "request.text.contains('project-x')"
        action: notify
"""


def test_notify_rule_loads_without_workflow_or_warning() -> None:
    policy, result = load_policy_from_string(NOTIFY_YAML, format="yaml")

    assert result.is_valid is True
    assert result.errors == []
    assert not any("notify-codename" in warning for warning in result.warnings)
    assert policy is not None
    assert policy.model_io[0].conditions[0].action == "notify"
    assert policy.model_io[0].approval_workflow is None


def test_notify_rule_round_trips_through_yaml_export() -> None:
    policy, _result = load_policy_from_string(NOTIFY_YAML, format="yaml")
    assert policy is not None

    exported = yaml.safe_load(export_policy_to_yaml(policy))

    assert exported["model_io"][0]["conditions"][0]["action"] == "notify"


def test_notify_on_a_tool_condition_is_a_validation_error() -> None:
    content = """
version: "1.0"
metadata:
  name: Bad
tools:
  - name: bash
    source: builtin
    conditions:
      - expression: "true"
        action: notify
"""
    policy, result = load_policy_from_string(content, format="yaml")

    assert policy is None
    assert result.is_valid is False
    assert any("notify" in error.message for error in result.errors)
