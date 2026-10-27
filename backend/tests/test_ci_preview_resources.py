"""Preview sizing is explicit; ordinary chart consumers keep their defaults."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_only_preview_deployments_use_the_small_profile() -> None:
    ci = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
    users = {
        name
        for name, job in ci.items()
        if isinstance(job, dict) and job.get("extends") == ".deploy:template"
    }
    assert users == {"deploy:test", "deploy:review"}
    command = next(
        step
        for step in ci[".deploy:template"]["script"]
        if step.startswith("helm upgrade")
    )
    assert "--values ./helm/preloop/values-ci.yaml" in command


def test_preview_profile_bounds_database_and_nats_memory() -> None:
    profile = yaml.safe_load((ROOT / "helm/preloop/values-ci.yaml").read_text())
    assert profile["gateway"]["autoscaling"] == {"minReplicas": 1, "maxReplicas": 1}
    database = profile["database"]["cnpg"]
    assert database["resources"]["limits"]["memory"] == "1Gi"
    assert database["parameters"]["shared_buffers"] == "256MB"
    assert database["parameters"]["work_mem"] == "4MB"
    assert profile["nats"]["natsBox"]["enabled"] is False
    for component in ("container", "reloader"):
        limits = profile["nats"][component]["merge"]["resources"]["limits"]
        assert limits["cpu"] and limits["memory"]
    defaults = yaml.safe_load((ROOT / "helm/preloop/values.yaml").read_text())
    assert defaults["worker"]["resources"]["limits"]["memory"] == "2Gi"
