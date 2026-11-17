"""Exercise conservative selection with historical diffs and boundary cases."""

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from ci_select import SMOKE, changed_paths, impacted_tests, matches, select  # noqa: E402
from run_backend_shard import discover_files  # noqa: E402

POLICY = json.loads((ROOT / ".github/ci-policy.json").read_text())
INVENTORY = discover_files(ROOT)
REPLAY = json.loads((ROOT / ".github/tests/fixtures/merged-pr-paths.json").read_text())


@pytest.mark.parametrize("pr", REPLAY["prs"], ids=lambda pr: f"pr-{pr['pr']}")
def test_historical_diffs_never_reduce_live_coverage(pr: dict) -> None:
    """Every historical legacy selection survives comparison-mode rollout."""
    result = select(pr["paths"], POLICY, INVENTORY)
    assert result["mode"] == "comparison"
    assert result["legacy"] == pr["legacy_jobs"]
    assert all(
        not enabled or result["live"][name]
        for name, enabled in result["legacy"].items()
    )
    assert result["live"]["parity"]
    assert set(result["candidate_tests"]) <= set(INVENTORY)
    assert set(result["fast_tests"]) <= set(INVENTORY)
    if result["full"]:
        assert all(result["live"].values())
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
    "event", ["push", "merge_group", "schedule", "workflow_dispatch"]
)
def test_full_event_never_inherits_pr_filters(event: str) -> None:
    """Full gate and backstops discover the entire inventory."""
    result = select(["docs/index.md"], POLICY, INVENTORY, event=event)
    assert all(result["live"].values())
    assert result["candidate_tests"] == INVENTORY


def test_label_and_stack_expand_docs_only_changes() -> None:
    """Ready state never gates coverage; stack bases and ci:full expand it."""
    for options in ({"labels": ("ci:full",)}, {"base_ref": "feature/parent"}):
        result = select(["docs/index.md"], POLICY, INVENTORY, **options)
        assert all(result["live"].values())


def test_unknown_and_deleted_test_impact_selects_all() -> None:
    """A selector miss is evidence to expand, never evidence to skip."""
    for path in [
        "backend/tests/deleted_test.py",
        "backend/preloop/new_area/new.py",
        "backend/tests/conftest.py",
    ]:
        tests, _ = impacted_tests([path], INVENTORY)
        assert tests == INVENTORY


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
