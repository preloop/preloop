"""Guards for the governance review Helm values (#1714)."""

from __future__ import annotations

from typing import Dict, List

import yaml

from tests.helm.chart_helpers import helm_template, load_values, resolve_values_path

TEMPLATES = (
    "templates/api-deployment.yaml",
    "templates/spacesync-worker-deployment.yaml",
    "templates/spacesync-scheduler-deployment.yaml",
)
ENV_NAMES = (
    "GOVERNANCE_REVIEW_ENABLED",
    "PRELOOP_GOVERNANCE_ANALYSIS_MODEL",
    "GOVERNANCE_REVIEW_MODEL_ANALYSIS_ENABLED",
)


def _container_envs(rendered: str) -> List[Dict[str, str]]:
    envs = []
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") != "Deployment":
            continue
        container = doc["spec"]["template"]["spec"]["containers"][0]
        envs.append(
            {item["name"]: item.get("value") for item in container.get("env", [])}
        )
    assert envs, "template rendered no deployment"
    return envs


def test_values_leave_governance_review_unset() -> None:
    values = load_values()
    assert resolve_values_path(values, "governanceReview.enabled") is None
    assert resolve_values_path(values, "governanceReview.analysisModel") == ""


def test_governance_env_absent_when_unset() -> None:
    for template in TEMPLATES:
        for env in _container_envs(helm_template(template)):
            for name in ENV_NAMES:
                assert name not in env, (template, name)


def test_governance_env_present_when_set() -> None:
    overrides = [
        "governanceReview.enabled=false",
        "governanceReview.modelAnalysisEnabled=false",
        "governanceReview.analysisModel=example-flash",
    ]
    for template in TEMPLATES:
        for env in _container_envs(helm_template(template, overrides)):
            assert env["GOVERNANCE_REVIEW_ENABLED"] == "false", template
            assert env["GOVERNANCE_REVIEW_MODEL_ANALYSIS_ENABLED"] == "false", template
            assert env["PRELOOP_GOVERNANCE_ANALYSIS_MODEL"] == "example-flash"


def test_governance_enabled_true_renders() -> None:
    rendered = helm_template(TEMPLATES[2], ["governanceReview.enabled=true"])
    env = _container_envs(rendered)[0]
    assert env["GOVERNANCE_REVIEW_ENABLED"] == "true"
    assert "PRELOOP_GOVERNANCE_ANALYSIS_MODEL" not in env
