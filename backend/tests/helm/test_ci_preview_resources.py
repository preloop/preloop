"""Preview resources must survive real chart rendering."""

import shutil
import subprocess

import pytest
import yaml

from tests.helm.chart_helpers import CHART_DIR, helm_template_all, load_values

PROFILE = "values-ci.yaml"


def test_preview_render_bounds_workloads_database_and_autoscaling() -> None:
    documents = [
        doc
        for doc in yaml.safe_load_all(helm_template_all(values_files=[PROFILE]))
        if doc
    ]
    deployments = {
        doc["metadata"]["labels"]["app.kubernetes.io/component"]: doc
        for doc in documents
        if doc["kind"] == "Deployment"
    }
    expected = {
        "api": ("500m", "1Gi"),
        "gateway": ("500m", "2Gi"),
        "preloop-sync-worker-default": ("250m", "768Mi"),
        "preloop-sync-worker-flow-execution": ("250m", "768Mi"),
        "preloop-sync-scheduler": ("250m", "512Mi"),
        "preloop-sync-monitor": ("250m", "512Mi"),
        "console": ("100m", "256Mi"),
    }
    for component, (cpu, memory) in expected.items():
        deployment = deployments[component]
        assert deployment["spec"].get("replicas", 1) == 1
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert container["resources"]["limits"] == {"cpu": cpu, "memory": memory}
    hpa = next(doc for doc in documents if doc["kind"] == "HorizontalPodAutoscaler")
    assert hpa["spec"]["minReplicas"] == hpa["spec"]["maxReplicas"] == 1
    database = next(doc for doc in documents if doc["kind"] == "Cluster")
    assert database["spec"]["instances"] == 1
    assert database["spec"]["resources"]["limits"] == {"cpu": "500m", "memory": "1Gi"}
    assert database["spec"]["postgresql"]["parameters"]["shared_buffers"] == "256MB"
    # Render defaults separately: applying the overlay must not mutate them.
    defaults = list(yaml.safe_load_all(helm_template_all()))
    default_worker = next(
        doc
        for doc in defaults
        if doc
        and doc["kind"] == "Deployment"
        and doc["metadata"]["labels"].get("app.kubernetes.io/component")
        == "preloop-sync-worker-default"
    )
    assert (
        default_worker["spec"]["template"]["spec"]["containers"][0]["resources"]
        == load_values()["worker"]["resources"]
    )


def test_preview_render_limits_real_nats_dependency() -> None:
    helm = shutil.which("helm")
    if helm is None or not list((CHART_DIR / "charts").glob("nats-*.tgz")):
        pytest.skip("Full dependency render runs after helm-render-check.sh in Helm CI")
    rendered = subprocess.run(
        [
            helm,
            "template",
            "preview",
            str(CHART_DIR),
            "-f",
            str(CHART_DIR / PROFILE),
            "--set",
            "existingSecret=preview-fixture",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    documents = [doc for doc in yaml.safe_load_all(rendered.stdout) if doc]
    nats = next(
        doc
        for doc in documents
        if doc["kind"] == "StatefulSet" and doc["metadata"]["name"] == "preview-nats"
    )
    containers = {c["name"]: c for c in nats["spec"]["template"]["spec"]["containers"]}
    assert containers["nats"]["resources"]["limits"] == {
        "cpu": "250m",
        "memory": "512Mi",
    }
    assert containers["reloader"]["resources"]["limits"] == {
        "cpu": "50m",
        "memory": "128Mi",
    }
    assert not any("nats-box" in doc["metadata"]["name"] for doc in documents)
