"""The agent role may create report setup Secrets but read none."""

from __future__ import annotations

import yaml

from tests.helm.chart_helpers import helm_template


def test_agent_role_cannot_read_namespace_secrets() -> None:
    """Agents often share the release namespace and its application Secrets."""
    rendered = helm_template("templates/agent-rbac.yaml")
    rules = [
        rule
        for doc in yaml.safe_load_all(rendered)
        if doc and doc.get("kind") == "Role"
        for rule in doc.get("rules") or []
        if "secrets" in rule.get("resources", [])
    ]
    assert rules
    assert all(rule["verbs"] == ["create"] for rule in rules)
