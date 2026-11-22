"""Exercise conservative selection with historical diffs and boundary cases."""

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import ci_import_graph  # noqa: E402
from ci_select import SMOKE, changed_paths, impacted_tests, matches, select  # noqa: E402
from run_backend_shard import discover_files  # noqa: E402

POLICY = json.loads((ROOT / ".github/ci-policy.json").read_text())
INVENTORY = discover_files(ROOT)
REPLAY = json.loads((ROOT / ".github/tests/fixtures/merged-pr-paths.json").read_text())


@pytest.mark.parametrize("pr", REPLAY["prs"], ids=lambda pr: f"pr-{pr['pr']}")
def test_historical_diffs_never_reduce_live_coverage(pr: dict) -> None:
    """Every historical legacy selection survives comparison-mode rollout."""
    result = select(pr["paths"], POLICY, INVENTORY)
    assert result["mode"] == "impact"
    assert result["legacy"] == pr["legacy_jobs"]
    assert all(
        not enabled or result["live"][name]
        for name, enabled in result["legacy"].items()
    )
    assert result["live"]["parity"]
    assert set(result["candidate_tests"]) <= set(INVENTORY)
    assert set(result["fast_tests"]) <= set(INVENTORY)
    assert set(SMOKE) <= set(result["candidate_tests"])
    assert result["backend_count"] == len(result["candidate_tests"])
    if result["full"]:
        assert all(result["live"].values())
    if result["backend_full"]:
        assert result["candidate_tests"] == INVENTORY


def test_replay_is_the_complete_measured_cohort() -> None:
    """Do not silently shrink the historical safety sample."""
    assert len(REPLAY["prs"]) == len({p["pr"] for p in REPLAY["prs"]}) == 100
    assert sum(len(p["paths"]) for p in REPLAY["prs"]) == 1811
    assert all((ROOT / path).is_file() for path in SMOKE)


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "frontend/package-lock.json",
        "cli/go.sum",
        "requirements/docs.txt",
        "backend/preloop/models/alembic/versions/new.py",
        "backend/preloop/api/auth/jwt.py",
        "backend/preloop/services/permissions.py",
        "backend/preloop/services/session.py",
        "frontend/src/services/mfa.ts",
        "cli/internal/auth/token.go",
        "environments/egress-proxy/main.go",
    ],
)
def test_sensitive_paths_force_every_validation(path: str) -> None:
    """Dependencies and security boundaries override all narrow selectors."""
    result = select([path], POLICY, INVENTORY)
    assert result["full"] and all(result["live"].values())


@pytest.mark.parametrize(
    ("event", "ref"),
    [
        ("push", "refs/tags/v1.2.3"),
        ("merge_group", "refs/heads/gh-readonly-queue/main/pr-1"),
        ("schedule", "refs/heads/main"),
        ("workflow_dispatch", "refs/heads/main"),
    ],
)
def test_full_event_never_inherits_pr_filters(event: str, ref: str) -> None:
    """Tags, the merge queue and backstops run the entire inventory."""
    result = select(["docs/index.md"], POLICY, INVENTORY, event=event, ref=ref)
    assert all(result["live"].values())
    assert result["candidate_tests"] == INVENTORY
    assert result["backend_full"] and result["backend_shards"] == 6


def test_label_and_stack_expand_docs_only_changes() -> None:
    """Ready state never gates coverage; stack bases and ci:full expand it."""
    for options in ({"labels": ("ci:full",)}, {"base_ref": "feature/parent"}):
        result = select(["docs/index.md"], POLICY, INVENTORY, **options)
        assert all(result["live"].values())


@pytest.mark.parametrize(
    "path",
    [
        "backend/tests/conftest.py",
        "backend/tests/services/audit/conftest.py",
        "backend/preloop/models/crud/user.py",
        "backend/preloop/models/alembic/versions/new.py",
        "backend/preloop/config.py",
        "pyproject.toml",
        ".github/requirements/app-dev.txt",
        "requirements/base.txt",
        "uv.lock",
        ".github/workflows/ci.yml",
        "scripts/ci_import_graph.py",
    ],
)
def test_shared_roots_select_every_test(path: str) -> None:
    """Fixtures, models, config, packaging and CI files have unbounded reach."""
    tests, reason = impacted_tests([path, "docs/index.md"], INVENTORY)
    assert tests == INVENTORY
    assert reason == f"shared root: {path}"


def test_deleted_test_file_selects_nothing_extra() -> None:
    """A deleted test cannot run, and nothing that remains imports it."""
    tests, _ = impacted_tests(["backend/tests/test_deleted_example.py"], INVENTORY)
    assert tests == []


def test_test_only_diff_maps_complete_file() -> None:
    """A changed test file is never split into guessed individual nodes."""
    path = "backend/tests/endpoints/test_health.py"
    tests, _ = impacted_tests([path], INVENTORY)
    assert tests == [path]


def test_double_star_matches_root_and_nested_paths() -> None:
    """Manifest overrides include root manifests and direct PowerShell files."""
    assert matches("pyproject.toml", "**/pyproject.toml")
    assert matches("scripts/test.ps1", "scripts/**/*.ps1")
    assert matches("scripts/nested/test.ps1", "scripts/**/*.ps1")


def test_renames_include_deleted_sensitive_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real git diff must retain the old path when a security file is renamed."""
    import subprocess

    monkeypatch.chdir(tmp_path)

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], text=True).strip()

    git("init", "-q")
    git("config", "user.name", "Jane Doe")
    git("config", "user.email", "jane@example.com")
    (tmp_path / "auth.py").write_text("pass\n")
    git("add", ".")
    git("commit", "-qm", "initial")
    base = git("rev-parse", "HEAD")
    git("mv", "auth.py", "ordinary.py")
    git("commit", "-qm", "rename")
    assert changed_paths(base, "HEAD") == ["auth.py", "ordinary.py"]


def test_every_pr_rerun_forces_full_validation() -> None:
    """Approval reruns cannot reuse the earlier subset selector decision."""
    result = select(["docs/index.md"], POLICY, INVENTORY, attempt=2)
    assert result["full"] and all(result["live"].values())


FAKE_INVENTORY = [
    "backend/tests/endpoints/test_health.py",
    "backend/tests/services/test_widget.py",
    "backend/tests/services/test_widget_edge.py",
    "backend/tests/services/test_other.py",
    "backend/tests/api/test_uses_widget.py",
    "backend/tests/api/test_patches_widget.py",
    "backend/tests/integration/test_flows.py",
    *SMOKE,
]
FAKE_SOURCES = {
    "backend/tests/api/test_uses_widget.py": (
        "from preloop.services import (\n    other,\n    widget,\n)\n"
    ),
    "backend/tests/api/test_patches_widget.py": (
        'patch("preloop.services.widget.build")\n'
    ),
    "backend/tests/services/test_other.py": "from preloop.services import widgets\n",
}
WIDGET = "backend/preloop/services/widget.py"


def fake_read(path: str) -> str:
    """Return synthetic test sources keyed by path."""
    return FAKE_SOURCES.get(path, "")


FAKE_GRAPH = {
    path: ci_import_graph.direct_references(
        fake_read(path), ci_import_graph.module_name(path), False, is_test=True
    )
    for path in FAKE_INVENTORY
} | {WIDGET: {"top": [], "lazy": []}}


def test_module_change_maps_to_importers_only() -> None:
    """A module maps to the tests that load it, not its area or integration."""
    tests, reason = impacted_tests([WIDGET], FAKE_INVENTORY, fake_read, FAKE_GRAPH)
    assert reason == (
        "import graph: 1 changed or mentioning modules select 2 test files"
    )
    # Import graph, not file names: test_widget*.py import nothing here.
    assert tests == [
        "backend/tests/api/test_patches_widget.py",
        "backend/tests/api/test_uses_widget.py",
    ]


def test_fast_within_budget_keeps_module_selection() -> None:
    """A cheap selection is recorded with its impact reason."""
    result = select(
        [WIDGET],
        POLICY,
        FAKE_INVENTORY,
        durations={},
        read=fake_read,
        graph=FAKE_GRAPH,
    )
    assert result["fast_reason"].startswith("import graph:")
    assert "backend/tests/api/test_uses_widget.py" in result["fast_tests"]


def test_fast_over_budget_drops_importers_and_records_reason() -> None:
    """Importers that blow the budget leave smoke, changed and test_<mod>."""
    durations = {f"{path}::t": 1.0 for path in FAKE_INVENTORY}
    durations["backend/tests/api/test_uses_widget.py::t"] = 10_000.0
    changed = "backend/tests/services/test_other.py"
    result = select(
        [WIDGET, changed],
        POLICY,
        FAKE_INVENTORY,
        durations=durations,
        read=fake_read,
        graph=FAKE_GRAPH,
    )
    assert result["fast_reason"].startswith("over fast budget")
    assert result["fast_reason"].endswith("test_<module> files")
    assert set(result["fast_tests"]) == set(SMOKE) | {
        changed,
        "backend/tests/services/test_widget.py",
        "backend/tests/services/test_widget_edge.py",
    }
    assert result["fast_estimated_seconds"] < 10_000
    assert result["candidate_tests"] != FAKE_INVENTORY


def test_conftest_falls_back_to_budget_not_full_suite() -> None:
    """Shared fixtures keep the full heavy tier, but fast stays narrow."""
    paths = ["backend/tests/conftest.py", WIDGET]
    result = select(
        paths,
        POLICY,
        FAKE_INVENTORY,
        durations={},
        read=fake_read,
        graph=FAKE_GRAPH,
    )
    assert result["candidate_tests"] == sorted(set(FAKE_INVENTORY))
    assert result["fast_reason"] == (
        "shared root: backend/tests/conftest.py: smoke, changed tests and "
        "module importers"
    )
    assert "backend/tests/integration/test_flows.py" not in result["fast_tests"]
    assert "backend/tests/api/test_uses_widget.py" in result["fast_tests"]


def test_full_suite_fast_lane_fits_budget_on_real_inventory() -> None:
    """Sensitive CI paths still give a fast lane, not the whole inventory."""
    durations = json.loads((ROOT / ".github/pytest-split-durations.json").read_text())
    result = select(
        ["scripts/ci_select.py", "backend/preloop/services/flow_orchestrator.py"],
        POLICY,
        INVENTORY,
        durations=durations,
    )
    assert result["full"] and result["candidate_tests"] == INVENTORY
    assert result["fast_estimated_seconds"] <= 240
