"""Values-file opt-outs reach the API through the chart's extraEnv path."""

from pathlib import Path

import pytest
import yaml

from tests.helm.chart_helpers import helm_template


@pytest.mark.parametrize("key", ["PRELOOP_DISABLE_TELEMETRY", "DISABLE_VERSION_CHECK"])
def test_values_file_persists_api_opt_out(tmp_path: Path, key: str) -> None:
    """Rendering the same values repeatedly retains either supported flag."""
    values = tmp_path / "telemetry.yaml"
    values.write_text(yaml.safe_dump({"extraEnv": [{"name": key, "value": "true"}]}))
    for _ in range(2):
        rendered = helm_template(
            "templates/api-deployment.yaml", values_files=[str(values)]
        )
        deployment = yaml.safe_load(rendered)
        containers = deployment["spec"]["template"]["spec"]["containers"]
        api_env = {entry["name"]: entry for entry in containers[0]["env"]}
        assert api_env[key]["value"] == "true"
