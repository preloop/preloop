"""Guards for the AKS reference sizing page and its tier overlays.

docs/operations/sizing-aks.md promises that every number traces to the chart
or to arithmetic on it. These tests recompute those numbers from the rendered
overlays and from values.yaml, and fail when the page and the chart drift.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Tuple

import pytest
import yaml

from tests.helm.chart_helpers import (
    CHART_DIR,
    REPO_ROOT,
    helm_template_all,
    load_values,
)

DOC = REPO_ROOT / "docs" / "operations" / "sizing-aks.md"
TIERS = ("small", "medium", "large")
# Dsv5 memory in GiB, from the Azure page the doc cites.
VM_MEMORY_GIB = {"D4s_v5": 16, "D8s_v5": 32}
VM_VCPU = {"D4s_v5": 4, "D8s_v5": 8}


def _doc() -> str:
    return DOC.read_text()


def _overlay(tier: str) -> Dict:
    return yaml.safe_load((CHART_DIR / f"values-aks-{tier}.yaml").read_text())


def _rendered(tier: str) -> List[Dict]:
    out = helm_template_all(values_files=[f"values-aks-{tier}.yaml"])
    return [doc for doc in yaml.safe_load_all(out) if doc]


def _cpu_m(value: str) -> int:
    return int(value[:-1]) if value.endswith("m") else int(float(value) * 1000)


def _mem_mi(value: str) -> int:
    if value.endswith("Gi"):
        return int(float(value[:-2]) * 1024)
    assert value.endswith("Mi"), value
    return int(value[:-2])


def _row(table_text: str, first_cell: str) -> List[str]:
    for line in table_text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0] == first_cell:
            return cells
    raise AssertionError(f"no table row starting with {first_cell!r}")


def _section(heading: str) -> str:
    text = _doc()
    start = text.index(heading)
    nxt = text.find("\n## ", start + len(heading))
    return text[start : nxt if nxt != -1 else len(text)]


class _Shape:
    """Pods, resources and pools of one tier, read from the rendered chart."""

    def __init__(self, tier: str) -> None:
        docs = _rendered(tier)
        self.hpa = {
            d["metadata"]["name"]: d["spec"]
            for d in docs
            if d["kind"] == "HorizontalPodAutoscaler"
        }
        # Preloop's own pods only. When the NATS subchart has been fetched
        # into charts/ (CI's render step does that), nats-box renders too;
        # the page documents it as outside the totals.
        self.deployments = [
            d
            for d in docs
            if d["kind"] == "Deployment"
            and d["metadata"]["labels"].get("app.kubernetes.io/name") == "preloop"
        ]
        self.kinds = {d["kind"] for d in docs}
        self.docs = docs

    def replicas(self, deployment: Dict, gateway_at: str) -> int:
        name = deployment["metadata"]["name"]
        if name in self.hpa:
            return self.hpa[name][gateway_at]
        return deployment["spec"]["replicas"]

    def by_suffix(self, suffix: str) -> Dict:
        matches = [
            d for d in self.deployments if d["metadata"]["name"].endswith(suffix)
        ]
        assert len(matches) == 1, suffix
        return matches[0]

    def connections(self, gateway_at: str) -> Tuple[int, int]:
        steady = ceiling = 0
        for dep in self.deployments:
            container = dep["spec"]["template"]["spec"]["containers"][0]
            env = {e["name"]: e.get("value") for e in container.get("env", [])}
            if "DATABASE_POOL_SIZE" not in env:
                continue
            size = int(env["DATABASE_POOL_SIZE"])
            overflow = int(env["DATABASE_MAX_OVERFLOW"])
            n = self.replicas(dep, gateway_at)
            steady += n * (size * 2 + 1)
            ceiling += n * ((size + overflow) * 2 + 1)
        return steady, ceiling

    def resources(self) -> Tuple[int, int, int, int]:
        cpu_r = mem_r = cpu_l = mem_l = 0
        for dep in self.deployments:
            n = self.replicas(dep, "maxReplicas")
            for c in dep["spec"]["template"]["spec"]["containers"]:
                res = c["resources"]
                cpu_r += n * _cpu_m(res["requests"]["cpu"])
                mem_r += n * _mem_mi(res["requests"]["memory"])
                cpu_l += n * _cpu_m(res["limits"]["cpu"])
                mem_l += n * _mem_mi(res["limits"]["memory"])
        return cpu_r, mem_r, cpu_l, mem_l


@pytest.fixture(scope="module")
def shapes() -> Dict[str, _Shape]:
    return {tier: _Shape(tier) for tier in TIERS}


def test_tier_table_matches_rendered_replicas(shapes: Dict[str, _Shape]) -> None:
    table = _section("## Choosing a tier")
    for col, tier in enumerate(TIERS, start=1):
        shape = shapes[tier]
        gw = shape.hpa["preloop-gateway"]
        assert _row(table, "API replicas")[col] == str(
            shape.by_suffix("-api")["spec"]["replicas"]
        )
        assert _row(table, "Gateway HPA min / max")[col] == (
            f"{gw['minReplicas']} / {gw['maxReplicas']}"
        )
        assert _row(table, "Default worker replicas")[col] == str(
            shape.by_suffix("-worker-default")["spec"]["replicas"]
        )
        assert _row(table, "Flow-execution worker replicas")[col] == str(
            shape.by_suffix("-worker-flow-execution")["spec"]["replicas"]
        )


def test_gateway_hpa_targets_match_chart_defaults(
    shapes: Dict[str, _Shape],
) -> None:
    defaults = load_values()["gateway"]["autoscaling"]
    for shape in shapes.values():
        metrics = {
            m["resource"]["name"]: m["resource"]["target"]["averageUtilization"]
            for m in shape.hpa["preloop-gateway"]["metrics"]
        }
        assert metrics["cpu"] == defaults["targetCPUUtilizationPercentage"]
        assert metrics["memory"] == defaults["targetMemoryUtilizationPercentage"]
        assert shape.hpa["preloop-gateway"]["minReplicas"] == defaults["minReplicas"]
    assert "CPU\ntarget 75%" in _doc() or "CPU target 75%" in _doc()
    assert "memory\ntarget 90%" in _doc() or "memory target 90%" in _doc()


def test_concurrent_agents_derive_from_max_inflight_and_quota(
    shapes: Dict[str, _Shape],
) -> None:
    values = load_values()
    inflight = values["flowExecution"]["maxInflight"]
    per_account = values["flowExecution"]["maxRunningPerAccount"]
    table = _section("## Choosing a tier")
    for col, tier in enumerate(TIERS, start=1):
        assert _overlay(tier)["flowExecution"]["workerEnabled"] is True
        flow = shapes[tier].by_suffix("-worker-flow-execution")["spec"]["replicas"]
        assert _row(table, "Concurrent hosted agents (instance)")[col] == str(
            flow * inflight
        )
        assert _row(table, "Concurrent hosted agents (one account)")[col] == str(
            per_account
        )
    large = shapes["large"].by_suffix("-worker-flow-execution")["spec"]["replicas"]
    assert large * inflight == int(values["agentExecution"]["resourceQuota"]["maxJobs"])


def test_connection_budget_table_is_recomputed_from_rendered_pools(
    shapes: Dict[str, _Shape],
) -> None:
    table = _section("### Connection budget")
    for tier in TIERS:
        lo = shapes[tier].connections("minReplicas")
        hi = shapes[tier].connections("maxReplicas")
        assert _row(table, tier.capitalize())[1:5] == [str(n) for n in (*lo, *hi)]


def test_medium_budget_matches_values_yaml_comment(
    shapes: Dict[str, _Shape],
) -> None:
    comment = (CHART_DIR / "values.yaml").read_text()
    lo = re.search(r"TOTAL at gw min 2:\s+steady (\d+), ceiling (\d+)", comment)
    hi = re.search(r"TOTAL at gw max 5:\s+steady (\d+), ceiling (\d+)", comment)
    assert lo and hi
    assert shapes["medium"].connections("minReplicas") == tuple(
        int(x) for x in lo.groups()
    )
    assert shapes["medium"].connections("maxReplicas") == tuple(
        int(x) for x in hi.groups()
    )


def test_every_ceiling_fits_the_smallest_suggested_sku(
    shapes: Dict[str, _Shape],
) -> None:
    skus = _section("### Connection budget")
    user_connections = int(_row(skus, "D2ds_v5")[4].replace(",", ""))
    assert user_connections == 859 - 15
    for shape in shapes.values():
        assert shape.connections("maxReplicas")[1] <= user_connections


def test_resource_totals_are_recomputed_from_rendered_pods(
    shapes: Dict[str, _Shape],
) -> None:
    table = _section("## Pod resources and HPA")
    for tier in TIERS:
        cpu_r, mem_r, cpu_l, mem_l = shapes[tier].resources()
        assert _row(table, tier.capitalize())[1:5] == [
            f"{cpu_r}m",
            f"{mem_r}Mi",
            f"{cpu_l}m",
            f"{mem_l}Mi",
        ]


def test_node_pool_minimums_follow_the_stated_rule(
    shapes: Dict[str, _Shape],
) -> None:
    table = _section("## Node pools").split("**Agent pool.**")[0]
    for tier in TIERS:
        mem_gib = shapes[tier].resources()[3] / 1024
        row = _row(table, tier.capitalize())
        assert row[1] == f"{shapes[tier].resources()[3]}Mi"
        for col, sku in ((2, "D4s_v5"), (3, "D8s_v5")):
            assert row[col] == str(math.ceil(mem_gib / VM_MEMORY_GIB[sku]) + 1)
        count, _, suggested = row[4].partition(" x Standard_")
        assert int(count) >= int(row[2 if suggested == "D4s_v5" else 3])


def test_agent_pool_derives_from_limit_range(shapes: Dict[str, _Shape]) -> None:
    values = load_values()
    container = values["agentExecution"]["limitRange"]["container"]
    req_cpu = _cpu_m(container["defaultRequestCpu"])
    req_mem = _mem_mi(container["defaultRequestMemory"])
    lim_cpu = _cpu_m(container["defaultCpu"])
    lim_mem = _mem_mi(container["defaultMemory"])
    inflight = values["flowExecution"]["maxInflight"]
    table = _section("## Node pools").split("**Agent pool.**")[1]
    for tier in TIERS:
        agents = (
            shapes[tier].by_suffix("-worker-flow-execution")["spec"]["replicas"]
            * inflight
        )
        row = _row(table, tier.capitalize())
        assert row[1] == str(agents)
        assert row[2] == f"{agents * req_cpu}m / {agents * req_mem // 1024}Gi"
        assert row[3] == (
            f"{agents * lim_cpu // 1000} CPU / {agents * lim_mem // 1024}Gi"
        )
        node_min = max(
            math.ceil(agents * req_cpu / 1000 / VM_VCPU["D8s_v5"]),
            math.ceil(agents * req_mem / 1024 / VM_MEMORY_GIB["D8s_v5"]),
        )
        node_max = max(
            math.ceil(agents * lim_cpu / 1000 / VM_VCPU["D8s_v5"]),
            math.ceil(agents * lim_mem / 1024 / VM_MEMORY_GIB["D8s_v5"]),
        )
        assert row[4] == f"{node_min} / {node_max}"


def test_overlays_use_external_postgres_from_a_secret(
    shapes: Dict[str, _Shape],
) -> None:
    for tier, shape in shapes.items():
        db = _overlay(tier)["database"]
        assert db["external"] is True
        assert db["urlFromSecret"]["name"] == "preloop-db"
        assert "Cluster" not in shape.kinds  # no in-cluster CloudNativePG
        assert _overlay(tier)["existingSecret"] == "preloop-app"
        assert _overlay(tier)["autoscaling"]["enabled"] is False


def test_overlays_keep_nats_jetstream_on_premium_disk() -> None:
    for tier in TIERS:
        nats = _overlay(tier)["nats"]
        assert nats["enabled"] is True
        jetstream = nats["config"]["jetstream"]
        assert jetstream["enabled"] is True
        pvc = jetstream["fileStore"]["pvc"]
        assert pvc["enabled"] is True
        assert pvc["storageClassName"] == "managed-csi-premium"
        assert f"PVC is\n{pvc['size']}" in _doc() or f"PVC is {pvc['size']}" in _doc()


def test_page_carries_the_not_load_tested_disclaimer_and_recipe() -> None:
    text = _doc()
    assert '!!! warning "Not load tested"' in text
    assert "## Load test recipe" in text
    assert "scripts/capacity/lab.sh run" in text
    assert (REPO_ROOT / "scripts" / "capacity" / "lab.sh").exists()
    # No invented throughput: the requests-per-day row stays unmeasured.
    row = _row(_section("## Choosing a tier"), "Governed requests per day")
    assert row[1:] == ["not measured"] * 3


def test_azure_figures_cite_microsoft_learn() -> None:
    text = _doc()
    for url in (
        "https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-limits",
        "https://learn.microsoft.com/en-us/azure/postgresql/extensions/how-to-use-pgvector",
        "https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/dsv5-series",
        "https://learn.microsoft.com/en-us/azure/aks/azure-csi-disk-storage-provision",
    ):
        assert url in text


def test_page_is_linked_from_install_docs_and_nav() -> None:
    assert "docs/operations/sizing-aks.md" in (CHART_DIR / "README.md").read_text()
    install = (REPO_ROOT / "docs" / "operations" / "installation.md").read_text()
    assert "(sizing-aks.md)" in install
    assert "operations/sizing-aks.md" in (REPO_ROOT / "mkdocs.yml").read_text()


def test_published_text_has_no_em_dash() -> None:
    paths = [DOC] + [CHART_DIR / f"values-aks-{tier}.yaml" for tier in TIERS]
    for path in paths:
        assert "—" not in path.read_text(), path
