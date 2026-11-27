"""Runner settings and every Helm gateway proxy share the request budget."""

import yaml

from tests.helm.chart_helpers import helm_template


def test_runner_limits_reach_api_and_gateway() -> None:
    """The socket owner and gateway must agree on configured limits."""
    for template in ("api-deployment.yaml", "gateway-deployment.yaml"):
        doc = yaml.safe_load(
            helm_template(
                "templates/" + template,
                ["runnerModels.silentSeconds=1200", "runnerModels.totalSeconds=7200"],
            )
        )
        env = {
            item["name"]: item.get("value")
            for item in doc["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert env["RUNNER_MODEL_IDLE_SECONDS"] == "60"
        assert env["RUNNER_MODEL_SILENT_SECONDS"] == "1200"
        assert env["RUNNER_MODEL_TOTAL_SECONDS"] == "7200"


def test_raising_total_also_raises_ingress_and_console_timeouts() -> None:
    """Even nonstreaming responses may remain silent for the total budget."""
    overrides = ["ingress.enabled=true", "runnerModels.totalSeconds=7200"]
    for doc in yaml.safe_load_all(helm_template("templates/ingress.yaml", overrides)):
        annotations = doc["metadata"]["annotations"]
        for kind in ("read", "send"):
            assert (
                annotations[f"nginx.ingress.kubernetes.io/proxy-{kind}-timeout"]
                == "7215"
            )
    config = helm_template("templates/configmap-nginx.yaml", overrides)
    assert config.count("proxy_read_timeout 7215s;") == 3
    assert config.count("proxy_send_timeout 7215s;") == 3
