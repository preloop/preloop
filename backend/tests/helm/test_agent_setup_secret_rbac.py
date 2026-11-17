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


def test_scheduled_flow_runners_get_operator_alert_channels() -> None:
    """The scheduler runs scheduled flows by default; it needs the alert env."""
    for template in (
        "templates/spacesync-scheduler-deployment.yaml",
        "templates/spacesync-worker-deployment.yaml",
        "templates/api-deployment.yaml",
    ):
        rendered = helm_template(
            template,
            overrides=[
                "notifications.operatorAccountIds[0]=synthetic-account",
                "notifications.productTeamEmail=ops@example.com",
                "flowExecution.workerEnabled=true",
            ],
        )
        env = {
            item["name"]: item.get("value")
            for doc in yaml.safe_load_all(rendered)
            if doc and doc.get("kind") == "Deployment"
            for container in doc["spec"]["template"]["spec"]["containers"]
            for item in container.get("env") or []
        }
        assert env.get("OPERATOR_ACCOUNT_IDS") == "synthetic-account", template
        assert env.get("PRODUCT_TEAM_EMAIL") == "ops@example.com", template
