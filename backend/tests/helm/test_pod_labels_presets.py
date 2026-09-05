"""Guards for pod `app:` labels, preset sync after migrate, nginx body size.

`app:` used to live only on Deployment metadata. Kubernetes copies pod
*template* labels onto pods, not Deployment metadata, so
``kubectl -l app=api`` found nothing. Extra pod labels are safe; changing
``spec.selector.matchLabels`` on a live Deployment is an immutable-field
error.

The post-upgrade migration Job used to run alembic only. Global flow
presets then drifted until someone ran ``scripts/sync_flow_presets.py``
by hand. Console nginx defaulted ``client_max_body_size`` to 1m, so
avatar uploads over 1 MB 413'd as HTML before the API saw them.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

import pytest
import yaml

from tests.helm.chart_helpers import helm_template, load_values, resolve_values_path

# Workloads that set `app:` on Deployment metadata. Each extra pod-template
# label must match metadata without being added to the selector.
APP_LABEL_DEPLOYMENTS: List[Tuple[str, str]] = [
    ("templates/api-deployment.yaml", "api"),
    ("templates/frontend-deployment.yaml", "console"),
    ("templates/gateway-deployment.yaml", "gateway"),
    ("templates/spacesync-scheduler-deployment.yaml", "preloop-sync-scheduler"),
    ("templates/spacesync-monitor-deployment.yaml", "preloop-sync-monitor"),
]


def _docs(rendered: str) -> List[Dict]:
    return [doc for doc in yaml.safe_load_all(rendered) if doc]


def _first_deployment(rendered: str) -> Dict:
    docs = [doc for doc in _docs(rendered) if doc.get("kind") == "Deployment"]
    assert docs, "expected a Deployment document"
    return docs[0]


@pytest.mark.parametrize("template,app", APP_LABEL_DEPLOYMENTS)
def test_pod_template_copies_app_label(template: str, app: str) -> None:
    """Pods must carry the same `app:` key as Deployment metadata."""
    dep = _first_deployment(helm_template(template))
    assert dep["metadata"]["labels"]["app"] == app
    assert dep["spec"]["template"]["metadata"]["labels"]["app"] == app
    assert "app" not in dep["spec"]["selector"]["matchLabels"]


def test_worker_pod_templates_copy_app_label() -> None:
    """Each worker pool Deployment copies `app:` onto the pod template."""
    docs = [
        doc
        for doc in _docs(helm_template("templates/spacesync-worker-deployment.yaml"))
        if doc.get("kind") == "Deployment"
    ]
    assert docs, "expected worker pool Deployments"
    for dep in docs:
        app = dep["metadata"]["labels"]["app"]
        assert app.startswith("preloop-sync-worker-")
        assert dep["spec"]["template"]["metadata"]["labels"]["app"] == app
        assert "app" not in dep["spec"]["selector"]["matchLabels"]


def test_migration_job_syncs_flow_presets_after_alembic() -> None:
    """Alembic must succeed first; preset sync uses the same container."""
    rendered = helm_template("templates/migration-job.yaml")
    job = _docs(rendered)[0]
    assert job["kind"] == "Job"
    container = job["spec"]["template"]["spec"]["containers"][0]
    args = " ".join(container.get("args") or [])
    assert "alembic upgrade head" in args
    assert "python /app/scripts/sync_flow_presets.py --no-propagate" in args
    assert args.index("alembic upgrade head") < args.index(
        "python /app/scripts/sync_flow_presets.py --no-propagate"
    )
    assert "--cleanup" not in args
    assert "init" not in (job["metadata"].get("name") or "")


def test_console_nginx_sets_client_max_body_size_from_gateway_proxy() -> None:
    """Server-level body size must match ingress, not nginx's 1m default."""
    values = load_values()
    expected = resolve_values_path(values, "gateway.proxy.bodySize")
    assert expected, "gateway.proxy.bodySize must be set"

    rendered = helm_template("templates/configmap-nginx.yaml")
    nginx = _docs(rendered)[0]["data"]["default.conf.template"]
    server_start = nginx.find("server {")
    first_location = nginx.find("location ")
    preamble = nginx[server_start:first_location]
    match = re.search(r"client_max_body_size\s+(\S+)\s*;", preamble)
    assert match, "server block is missing client_max_body_size before locations"
    assert match.group(1) == expected
