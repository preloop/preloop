"""Guards that the gateway chart wires GATEWAY_ERROR_ALERT_INTERVAL_SECONDS.

The process reads that env var in ``gateway_error_alerts.py`` (default 300).
The Helm chart must expose the same knob on the gateway deployment, keep the
chart default at 300 so existing deploys do not change throttle behavior, and
omit the env var when the value is emptied so operators can fall back to the
process default without a secret.
"""

from __future__ import annotations

from typing import Dict, List

import yaml

from tests.helm.chart_helpers import helm_template, load_values, resolve_values_path

DEFAULT_ALERT_INTERVAL_SECONDS = 300


def _docs(rendered: str) -> List[Dict]:
    return [doc for doc in yaml.safe_load_all(rendered) if doc]


def _container_env(rendered: str) -> Dict[str, Dict]:
    deployment = _docs(rendered)[0]
    env = deployment["spec"]["template"]["spec"]["containers"][0].get("env") or []
    return {item["name"]: item for item in env}


def test_values_declare_the_code_default_interval() -> None:
    values = load_values()
    assert (
        resolve_values_path(values, "environment.gatewayErrorAlertIntervalSeconds")
        == DEFAULT_ALERT_INTERVAL_SECONDS
    )


def test_gateway_render_sets_alert_interval_env() -> None:
    env = _container_env(helm_template("templates/gateway-deployment.yaml"))
    item = env["GATEWAY_ERROR_ALERT_INTERVAL_SECONDS"]
    assert "valueFrom" not in item
    assert item["value"] == str(DEFAULT_ALERT_INTERVAL_SECONDS)


def test_gateway_alert_interval_is_overridable() -> None:
    env = _container_env(
        helm_template(
            "templates/gateway-deployment.yaml",
            ["environment.gatewayErrorAlertIntervalSeconds=60"],
        )
    )
    assert env["GATEWAY_ERROR_ALERT_INTERVAL_SECONDS"]["value"] == "60"


def test_gateway_alert_interval_omitted_when_empty() -> None:
    env = _container_env(
        helm_template(
            "templates/gateway-deployment.yaml",
            ["environment.gatewayErrorAlertIntervalSeconds="],
        )
    )
    assert "GATEWAY_ERROR_ALERT_INTERVAL_SECONDS" not in env


def test_api_does_not_set_gateway_alert_interval() -> None:
    env = _container_env(helm_template("templates/api-deployment.yaml"))
    assert "GATEWAY_ERROR_ALERT_INTERVAL_SECONDS" not in env
