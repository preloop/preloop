"""Guards for shared OTLP Helm values on API and gateway deployments."""

from __future__ import annotations

import yaml

from tests.helm.chart_helpers import helm_template, load_values, resolve_values_path


def test_values_declare_otlp_disabled_by_default() -> None:
    values = load_values()
    assert resolve_values_path(values, "otlp.enabled") is False
    assert resolve_values_path(values, "otlp.endpoint") == ""
    assert resolve_values_path(values, "otlp.protocol") == "http/protobuf"
    assert resolve_values_path(values, "otlp.headersSecret.key") == "otlp-headers"
    assert resolve_values_path(values, "otlp.resource.serviceName") == "preloop"


def test_otlp_env_absent_when_disabled() -> None:
    for template in (
        "templates/gateway-deployment.yaml",
        "templates/api-deployment.yaml",
    ):
        rendered = helm_template(template)
        assert "OTLP_ENABLED" not in rendered
        assert "OTLP_ENDPOINT" not in rendered


def test_otlp_env_present_on_api_and_gateway_when_enabled() -> None:
    overrides = [
        "otlp.enabled=true",
        "otlp.endpoint=http://otel-collector:4318",
        "otlp.protocol=http/protobuf",
        "otlp.resource.serviceName=preloop",
        "otlp.resource.deploymentEnvironment=staging",
        "otlp.samplerRatio=0.1",
    ]
    for template in (
        "templates/gateway-deployment.yaml",
        "templates/api-deployment.yaml",
    ):
        rendered = helm_template(template, overrides)
        env_names = {
            item["name"]
            for item in yaml.safe_load(rendered)["spec"]["template"]["spec"][
                "containers"
            ][0]["env"]
        }
        assert "OTLP_ENABLED" in env_names
        assert "OTLP_ENDPOINT" in env_names
        assert "OTLP_PROTOCOL" in env_names
        assert "OTLP_SERVICE_NAME" in env_names
        assert "OTLP_DEPLOYMENT_ENVIRONMENT" in env_names
        assert "OTLP_SAMPLER_RATIO" in env_names
        assert "http://otel-collector:4318" in rendered


def test_otlp_headers_use_external_secret_ref() -> None:
    rendered = helm_template(
        "templates/gateway-deployment.yaml",
        [
            "otlp.enabled=true",
            "otlp.endpoint=http://otel-collector:4318",
            "otlp.headersSecret.name=langfuse-otlp",
            "otlp.headersSecret.key=otlp-headers",
        ],
    )
    env = yaml.safe_load(rendered)["spec"]["template"]["spec"]["containers"][0]["env"]
    headers = next(item for item in env if item["name"] == "OTLP_HEADERS")
    assert headers["valueFrom"]["secretKeyRef"]["name"] == "langfuse-otlp"
    assert headers["valueFrom"]["secretKeyRef"]["key"] == "otlp-headers"
