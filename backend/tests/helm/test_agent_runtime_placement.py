"""Agent runtime and node placement values reach the Job launchers.

Issue #1076: ``agentExecution.runtimeClassName`` must render into the API
settings, and the default chart must be unchanged. The three deployments
that can create agent Jobs all receive the env vars.
"""

import json

import pytest
import yaml

from tests.helm.chart_helpers import helm_template


def _envs(component: str, overrides: list[str]) -> list[dict]:
    rendered = helm_template(f"templates/{component}-deployment.yaml", overrides)
    deployments = [doc for doc in yaml.safe_load_all(rendered) if doc]
    assert deployments
    return [
        {
            item["name"]: item.get("value")
            for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        for deployment in deployments
    ]


@pytest.mark.parametrize("component", ["api", "gateway", "spacesync-worker"])
def test_default_chart_renders_no_placement_env(component: str) -> None:
    for env in _envs(component, []):
        assert "AGENT_RUNTIME_CLASS_NAME" not in env
        assert "AGENT_NODE_SELECTOR" not in env
        assert "AGENT_TOLERATIONS" not in env


@pytest.mark.parametrize("component", ["api", "gateway", "spacesync-worker"])
def test_placement_values_render_as_settings(component: str) -> None:
    envs = _envs(
        component,
        [
            "agentExecution.runtimeClassName=kata-containers",
            "agentExecution.nodeSelector.runtime=kata",
            "agentExecution.tolerations[0].key=dedicated",
            "agentExecution.tolerations[0].operator=Equal",
            "agentExecution.tolerations[0].value=agents",
            "agentExecution.tolerations[0].effect=NoSchedule",
        ],
    )
    for env in envs:
        assert env["AGENT_RUNTIME_CLASS_NAME"] == "kata-containers"
        assert json.loads(env["AGENT_NODE_SELECTOR"]) == {"runtime": "kata"}
        assert json.loads(env["AGENT_TOLERATIONS"]) == [
            {
                "key": "dedicated",
                "operator": "Equal",
                "value": "agents",
                "effect": "NoSchedule",
            }
        ]
