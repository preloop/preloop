"""Guards for the optional CNPG PgBouncer Pooler in the Helm chart (EE #47).

The Pooler is opt-in: the application is not yet safe behind transaction
pooling (session advisory locks, LISTEN, asyncpg prepared statements), so the
chart must not deploy it, or route anything to it, unless asked.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Dict, List

import pytest
import yaml

from tests.helm.chart_helpers import (
    TEST_JWT_SECRET,
    helm_template_all,
    load_values,
    offline_chart,
)


def _poolers(rendered: str) -> List[Dict]:
    """Return every rendered CNPG Pooler document."""
    return [
        doc
        for doc in yaml.safe_load_all(rendered)
        if isinstance(doc, dict) and doc.get("kind") == "Pooler"
    ]


def test_pooler_disabled_by_default() -> None:
    """Default values keep PgBouncer off and render no Pooler."""
    assert load_values()["database"]["pgbouncer"]["enabled"] is False
    assert _poolers(helm_template_all()) == []


def test_pooler_renders_when_enabled() -> None:
    """Enabling renders a transaction-mode Pooler bound to the chart cluster."""
    rendered = helm_template_all(
        [
            "database.pgbouncer.enabled=true",
            "database.pgbouncer.instances=3",
            "database.pgbouncer.parameters.default_pool_size=25",
        ]
    )
    poolers = _poolers(rendered)
    assert len(poolers) == 1
    pooler = poolers[0]
    assert pooler["apiVersion"] == "postgresql.cnpg.io/v1"
    assert pooler["metadata"]["name"] == "preloop-db-pooler-rw"
    spec = pooler["spec"]
    assert spec["cluster"]["name"] == "preloop-db"
    assert spec["instances"] == 3
    assert spec["type"] == "rw"
    assert spec["pgbouncer"]["poolMode"] == "transaction"
    params = spec["pgbouncer"]["parameters"]
    assert params["default_pool_size"] == "25"
    assert params["max_client_conn"] == "400"


def test_pooler_follows_custom_cluster_name() -> None:
    """A custom cnpg.name must be the Pooler's cluster reference."""
    rendered = helm_template_all(
        ["database.pgbouncer.enabled=true", "database.cnpg.name=maindb"]
    )
    assert _poolers(rendered)[0]["spec"]["cluster"]["name"] == "maindb"


def test_pooler_does_not_reroute_database_url() -> None:
    """Enabling the Pooler must not change the app's DATABASE_URL."""
    base = [
        d
        for d in yaml.safe_load_all(helm_template_all())
        if isinstance(d, dict) and d.get("kind") == "Secret"
    ]
    enabled = [
        d
        for d in yaml.safe_load_all(
            helm_template_all(["database.pgbouncer.enabled=true"])
        )
        if isinstance(d, dict) and d.get("kind") == "Secret"
    ]
    assert base == enabled


@pytest.mark.parametrize(
    "overrides",
    [
        ["database.external=true", "database.externalDatabase.host=db.example.com"],
        ["database.enabled=false"],
    ],
    ids=["external", "disabled"],
)
def test_pooler_without_chart_cluster_fails(overrides: List[str]) -> None:
    """There is no chart CNPG cluster to pool for external or disabled DBs."""
    helm = shutil.which("helm")
    if helm is None:  # pragma: no cover - depends on the local toolchain
        pytest.skip("helm binary not available")
    with offline_chart() as chart_dir:
        command = [helm, "template", "preloop", str(chart_dir)]
        for override in [
            f"environment.jwtSecret={TEST_JWT_SECRET}",
            "database.pgbouncer.enabled=true",
            *overrides,
        ]:
            command += ["--set", override]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "database.pgbouncer" in result.stderr
