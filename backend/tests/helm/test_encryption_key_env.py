"""Every workload that derives the credential key receives the dedicated key.

Stored credentials are sealed with ``SECURITY__ENCRYPTION_KEY`` when it is set
and with a key derived from ``SECRET_KEY`` otherwise. A pod that gets
``SECRET_KEY`` but not ``SECURITY__ENCRYPTION_KEY`` silently falls back to the
derived key, so it cannot read what the API wrote and anything it seals (the
credential backfill runs in the sync scheduler) is unreadable elsewhere.
"""

from __future__ import annotations

from typing import Dict, Iterator, List

import yaml

from tests.helm.chart_helpers import helm_template_all

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "Job", "CronJob", "DaemonSet"}


def _containers(doc: Dict) -> List[Dict]:
    spec = doc["spec"]
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    pod = spec["template"]["spec"]
    return list(pod.get("initContainers") or []) + list(pod.get("containers") or [])


def _workload_envs(rendered: str) -> Iterator[tuple[str, Dict[str, Dict]]]:
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") not in WORKLOAD_KINDS:
            continue
        for container in _containers(doc):
            name = f"{doc['kind']}/{doc['metadata']['name']}/{container['name']}"
            yield name, {e["name"]: e for e in container.get("env") or []}


def test_dedicated_key_reaches_every_workload_with_secret_key() -> None:
    rendered = helm_template_all(["security.encryptionKey=test-fernet-key-value"])
    checked = 0
    missing = []
    for name, env in _workload_envs(rendered):
        if "SECRET_KEY" not in env:
            continue
        checked += 1
        if "SECURITY__ENCRYPTION_KEY" not in env:
            missing.append(name)
    assert checked, "no workload renders SECRET_KEY"
    assert not missing, missing


def test_dedicated_key_absent_when_unset() -> None:
    rendered = helm_template_all()
    for name, env in _workload_envs(rendered):
        assert "SECURITY__ENCRYPTION_KEY" not in env, name
