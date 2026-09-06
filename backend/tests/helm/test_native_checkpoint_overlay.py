"""Native checkpoint deployment uses existing shared env without changing defaults."""

from typing import Any

import pytest
import yaml

from tests.helm.chart_helpers import CHART_DIR, helm_template, load_values

OVERLAY = "values-native-checkpoints.yaml"


def test_checkpoint_defaults_remain_disabled() -> None:
    assert not any(
        item["name"] == "FLOW_ARTIFACT_DIRECT_UPLOAD"
        for item in load_values()["extraEnv"]
    )


@pytest.mark.parametrize(
    "template",
    [
        "templates/api-deployment.yaml",
        "templates/gateway-deployment.yaml",
        "templates/spacesync-worker-deployment.yaml",
        "templates/spacesync-scheduler-deployment.yaml",
    ],
)
def test_checkpoint_overlay_reaches_api_and_execution_workers(template: str) -> None:
    documents = list(
        yaml.safe_load_all(helm_template(template, values_files=[OVERLAY]))
    )
    assert documents
    signing_refs: list[dict[str, Any]] = []
    for document in documents:
        for container in document["spec"]["template"]["spec"]["containers"]:
            env_items = container["env"]
            env = {item["name"]: item for item in env_items}
            assert len(env) == len(env_items)
            assert env["FLOW_ARTIFACT_DIRECT_UPLOAD"]["value"] == "true"
            assert env["WORKSPACE_SNAPSHOT_MAX_BYTES"]["value"] == "16777216"
            assert env["FLOW_NATIVE_SESSION_RETENTION_HOURS"]["value"] == "168"
            assert env["WORKSPACE_SNAPSHOT_TTL_HOURS"]["value"] == "168"
            signing_refs.append(env["SECRET_KEY"]["valueFrom"])
    assert all(
        ref == {"secretKeyRef": {"name": "preloop", "key": "jwt-secret"}}
        for ref in signing_refs
    )


def test_checkpoint_overlay_preserves_keys_and_proxy_limits() -> None:
    overlay = yaml.safe_load((CHART_DIR / OVERLAY).read_text())
    assert set(overlay) == {"extraEnv"}
    env = {item["name"]: item for item in overlay["extraEnv"]}
    assert "SECRET_KEY" not in env and "SECURITY__ENCRYPTION_KEY" not in env
    assert load_values()["gateway"]["proxy"]["bodySize"] == "32m"
    assert int(env["WORKSPACE_SNAPSHOT_MAX_BYTES"]["value"]) < 32 * 1024**2
