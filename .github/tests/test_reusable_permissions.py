"""Guard against reusable-workflow permission mismatches (startup failures)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RANK = {"none": 0, "read": 1, "write": 2}


def _calls() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    rows = []
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        for name, job in workflow.get("jobs", {}).items():
            uses = job.get("uses", "")
            if uses.startswith("./"):
                grant = job.get("permissions", workflow.get("permissions", {}))
                rows.append(
                    (
                        f"{path.name}:{name}",
                        grant,
                        yaml.safe_load((ROOT / uses[2:]).read_text()),
                    )
                )
    return rows


@pytest.mark.parametrize(("caller", "grant", "callee"), _calls())
def test_caller_grants_every_scope_callee_jobs_declare(
    caller: str, grant: dict[str, str], callee: dict[str, Any]
) -> None:
    """GitHub fails the whole run at startup when a callee asks for more."""
    assert isinstance(grant, dict), caller
    default = callee.get("permissions", {})
    for job_name, job in callee["jobs"].items():
        wanted = job.get("permissions", default)
        assert isinstance(wanted, dict), f"{caller} -> {job_name}"
        for scope, level in wanted.items():
            assert RANK[level] <= RANK[grant.get(scope, "none")], (
                f"{caller} -> {job_name} needs {scope}: {level}"
            )
