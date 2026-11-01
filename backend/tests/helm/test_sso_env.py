"""Chart wiring for enterprise single sign-on settings on the API."""

from __future__ import annotations

import shutil
import subprocess

import pytest
import yaml

from tests.helm.chart_helpers import (
    TEST_JWT_SECRET,
    helm_template,
    helm_template_all,
    load_values,
    offline_chart,
    resolve_values_path,
)

API = "templates/api-deployment.yaml"
SSO_VARS = (
    "ENTERPRISE_SSO_ENABLED",
    "SSO_PUBLIC_URL",
    "GATEWAY_IDP_ALLOW_PRIVATE_ISSUERS",
)


def _api_env(rendered: str) -> dict[str, dict]:
    for doc in yaml.safe_load_all(rendered):
        if doc and doc.get("kind") == "Deployment":
            container = doc["spec"]["template"]["spec"]["containers"][0]
            return {e["name"]: e for e in container.get("env") or []}
    raise AssertionError("no Deployment rendered")


def test_values_default_off() -> None:
    values = load_values()
    assert resolve_values_path(values, "sso.enabled") is False
    assert resolve_values_path(values, "sso.publicUrl") == ""
    assert resolve_values_path(values, "sso.allowPrivateIssuers") is False


def test_off_renders_disabled_flag_only() -> None:
    env = _api_env(helm_template(API))
    assert env["ENTERPRISE_SSO_ENABLED"]["value"] == "false"
    assert "SSO_PUBLIC_URL" not in env
    assert "GATEWAY_IDP_ALLOW_PRIVATE_ISSUERS" not in env


def test_off_ignores_other_sso_settings() -> None:
    env = _api_env(
        helm_template(
            API,
            [
                "sso.publicUrl=https://sso.example.com",
                "sso.allowPrivateIssuers=true",
            ],
        )
    )
    assert env["ENTERPRISE_SSO_ENABLED"]["value"] == "false"
    assert "SSO_PUBLIC_URL" not in env
    assert "GATEWAY_IDP_ALLOW_PRIVATE_ISSUERS" not in env


def test_on_with_defaults_uses_preloop_url() -> None:
    env = _api_env(helm_template(API, ["sso.enabled=true"]))
    assert env["ENTERPRISE_SSO_ENABLED"]["value"] == "true"
    assert "SSO_PUBLIC_URL" not in env
    assert env["PRELOOP_URL"]["value"] == "https://preloop.ai"
    assert "GATEWAY_IDP_ALLOW_PRIVATE_ISSUERS" not in env


def test_on_with_all_settings() -> None:
    env = _api_env(
        helm_template(
            API,
            [
                "sso.enabled=true",
                "sso.publicUrl=https://sso.example.com/",
                "sso.allowPrivateIssuers=true",
            ],
        )
    )
    assert env["ENTERPRISE_SSO_ENABLED"]["value"] == "true"
    assert env["SSO_PUBLIC_URL"]["value"] == "https://sso.example.com"
    assert env["GATEWAY_IDP_ALLOW_PRIVATE_ISSUERS"]["value"] == "true"


def _render_fails(public_url: str) -> str:
    helm = shutil.which("helm")
    if helm is None:  # pragma: no cover - depends on the local toolchain
        pytest.skip("helm binary not available")
    with offline_chart() as chart_dir:
        result = subprocess.run(
            [
                helm,
                "template",
                "preloop",
                str(chart_dir),
                "--show-only",
                API,
                "--set",
                f"environment.jwtSecret={TEST_JWT_SECRET}",
                "--set",
                "sso.enabled=true",
                "--set",
                f"sso.publicUrl={public_url}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode != 0
    return result.stderr


@pytest.mark.parametrize(
    "public_url",
    [
        "https://sso.example.com/preloop",
        "http://sso.example.com",
        "https://user@sso.example.com",
        "https://sso example.com",
    ],
)
def test_non_https_origin_fails_the_render(public_url: str) -> None:
    assert "sso.publicUrl must be an https origin" in _render_fails(public_url)


def test_https_origin_with_port_renders() -> None:
    env = _api_env(
        helm_template(
            API, ["sso.enabled=true", "sso.publicUrl=https://sso.example.com:8443"]
        )
    )
    assert env["SSO_PUBLIC_URL"]["value"] == "https://sso.example.com:8443"


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_sso_settings_stay_on_the_api(enabled: str) -> None:
    rendered = helm_template_all(
        [f"sso.enabled={enabled}", "sso.publicUrl=https://sso.example.com"]
    )
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") != "Deployment":
            continue
        name = doc["metadata"]["name"]
        for container in doc["spec"]["template"]["spec"]["containers"]:
            names = {e["name"] for e in container.get("env") or []}
            present = names & set(SSO_VARS)
            if name.endswith("-api"):
                assert "ENTERPRISE_SSO_ENABLED" in present
            else:
                assert not present, name
