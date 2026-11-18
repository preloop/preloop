"""Chart wiring for the tracker connection-test target policy on the API."""

from __future__ import annotations

import yaml

from tests.helm.chart_helpers import helm_template, load_values, resolve_values_path

API = "templates/api-deployment.yaml"


def _api_env(rendered: str) -> dict[str, dict]:
    for doc in yaml.safe_load_all(rendered):
        if doc and doc.get("kind") == "Deployment":
            container = doc["spec"]["template"]["spec"]["containers"][0]
            return {e["name"]: e for e in container.get("env") or []}
    raise AssertionError("no Deployment rendered")


def test_default_keeps_self_hosted_private_trackers_working() -> None:
    """Off by default, so the variable is not rendered."""
    assert resolve_values_path(load_values(), "trackers.blockPrivateTargets") is False
    assert "TRACKER_BLOCK_PRIVATE_TARGETS" not in _api_env(helm_template(API))


def test_strict_renders_the_setting_on_the_api() -> None:
    """Multi-tenant instances switch the API to strict."""
    env = _api_env(helm_template(API, ["trackers.blockPrivateTargets=true"]))
    assert env["TRACKER_BLOCK_PRIVATE_TARGETS"]["value"] == "true"
