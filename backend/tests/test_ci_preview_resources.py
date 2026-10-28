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
    assert "--post-renderer ./helm/preloop/ci-post-renderer.py" in command
