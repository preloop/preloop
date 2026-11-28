"""Input schema and catalog entry for the ``ci_health_report`` tool."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

GlobList = list[str]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class LedgerUpdate(_Closed):
    """An agent's change to one ledger entry."""

    signature_id: str = Field(min_length=1, max_length=64)
    status: Literal["open", "fixed", "ignored"] | None = None
    linked: str | None = Field(
        default=None, max_length=300, description="Issue or pull request URL."
    )
    note: str | None = Field(default=None, max_length=280)


class CiHealthReport(_Closed):
    """Arguments of ``ci_health_report``; every field is optional."""

    repositories: list[str] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "Repository paths (owner/name, group/sub/name) synced in this "
            "account. Default: the flow's own repositories."
        ),
    )
    window_hours: int = Field(default=24, ge=1, le=168)
    branches: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Only runs on these branches. Default: all branches.",
    )
    protected_branches: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Branches whose failures are read first. Default: the "
        "default branch.",
    )
    critical_path_jobs: GlobList = Field(
        default_factory=list,
        max_length=30,
        description="Globs of the jobs that gate a merge, e.g. 'test*', 'lint'.",
    )
    max_runs: int = Field(default=300, ge=1, le=1000)
    max_failed_logs: int = Field(default=20, ge=0, le=50)
    max_requests: int = Field(default=500, ge=10, le=2000)
    rate_limit_floor: int = Field(default=300, ge=0, le=5000)
    report_only: Literal["auto", "on", "off"] = Field(
        default="auto",
        description="auto: report only for the first report_only_days of the "
        "flow's ledger, then changes are allowed.",
    )
    report_only_days: int = Field(default=14, ge=0, le=90)
    ledger_updates: list[LedgerUpdate] = Field(default_factory=list, max_length=50)


def _schema() -> dict[str, Any]:
    schema = CiHealthReport.model_json_schema()
    return schema


CI_HEALTH_REPORT_TOOL: dict[str, Any] = {
    "name": "ci_health_report",
    "description": (
        "Read CI (GitHub Actions or GitLab CI) for the flow's repositories "
        "server-side and return one compact JSON report: per workflow and job "
        "success rate, reruns, queue and duration p50/p95, runner minutes, "
        "critical-path gate, shard imbalance, failure signatures with failure "
        "lines from failed-job logs, and the flow's failure ledger (first/last "
        "seen, counts, flaky, status). Updates the ledger; changed_since_last_run "
        "is false when no new run finished. Read-only towards the provider. "
        "Only works inside a flow run."
    ),
    "source": "builtin",
    "default_enabled": False,
    "requires_tracker": True,
    "required_tracker_types": [],
    "schema": _schema(),
}
