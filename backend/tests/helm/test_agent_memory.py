"""All Job launchers inherit the configured namespace memory policy."""

import pytest
import yaml

from tests.helm.chart_helpers import helm_template


@pytest.mark.parametrize("component", ["api", "gateway", "spacesync-worker"])
@pytest.mark.parametrize("overridden", [False, True])
def test_agent_memory_policy_reaches_launchers(
    component: str, overridden: bool
) -> None:
    overrides = (
        [
            "agentExecution.limitRange.container.defaultMemory=3Gi",
            "agentExecution.limitRange.container.defaultRequestMemory=1Gi",
        ]
        if overridden
        else []
    )
    rendered = helm_template(f"templates/{component}-deployment.yaml", overrides)
    deployments = [doc for doc in yaml.safe_load_all(rendered) if doc]
    assert deployments
    for deployment in deployments:
        env = {
            item["name"]: item.get("value")
            for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert env["AGENT_MEMORY_LIMIT"] == ("3Gi" if overridden else "4Gi")
        assert env["AGENT_MEMORY_REQUEST"] == ("1Gi" if overridden else "512Mi")
