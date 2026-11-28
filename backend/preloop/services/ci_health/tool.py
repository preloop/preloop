"""The ``ci_health_report`` MCP tool.

The tool runs on the server with the account's stored tracker credential, so
the agent never holds a CI token, and reads are budgeted and cached across
runs and flows. Steps:

1. Bind the call to its flow execution (same check as the flow state tools).
2. Resolve each repository path to a synced project in the account, and its
   tracker client through the normal tracker-scope checks.
3. Load the ledger (flow state ``preloop.ci_health``) and the pull requests
   this flow's earlier runs published.
4. :func:`~.collector.collect`, then store the ledger with compare-and-set.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Optional

from fastapi import HTTPException
from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool
from pydantic import ValidationError

from preloop.api.loop_safety import run_db_off_loop
from preloop.models import models
from preloop.models.crud import crud_flow_execution, crud_project
from preloop.models.crud import flow_state as crud_flow_state
from preloop.models.db.session import get_session_factory
from preloop.schemas.ci_health import CI_HEALTH_REPORT_TOOL, CiHealthReport
from preloop.services import flow_state_tools
from preloop.services.dynamic_fastmcp import DynamicFastMCP

from . import ledger as ledger_mod
from .collector import Options, Target, collect, precheck_focus
from .providers import CiProvider, GitHubActions, GitLabCi
from .transport import Budget, HttpTransport

logger = logging.getLogger(__name__)

NAME = CI_HEALTH_REPORT_TOOL["name"]
SUPPORTED_TRACKERS = ("github", "gitlab")
OUTCOME_LOOKBACK = 60


def published_pull_requests(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pull requests earlier runs of the flow published, from their results.

    Args:
        rows: :meth:`crud_flow_execution.list_outcomes` rows.

    Returns:
        Items for :func:`ledger.record_pull_requests`.
    """
    published = []
    for row in rows:
        result = row.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("pr_url"), str):
            continue
        raw_change = result.get("change")
        change: dict[str, Any] = raw_change if isinstance(raw_change, dict) else {}
        ended = row.get("end_time") or row.get("start_time")
        published.append(
            {
                "url": result["pr_url"],
                "execution_id": str(row["id"]),
                "opened_at": ended.isoformat() if ended else None,
                "estimated_minutes_saved_per_week": change.get(
                    "estimated_runner_minutes_saved_per_week"
                ),
                "signatures": change.get("signatures") or [],
            }
        )
    return list(reversed(published))


def _default_projects(flow: models.Flow) -> list[str]:
    ids: list[str] = []
    for repo in (flow.git_clone_config or {}).get("repositories") or []:
        if isinstance(repo, dict) and repo.get("project_id"):
            ids.append(str(repo["project_id"]))
    for project_id in flow.trigger_project_ids or []:
        if str(project_id) not in ids:
            ids.append(str(project_id))
    return ids


def _adapter(client: Any, budget: Budget) -> Optional[CiProvider]:
    """Build the CI adapter for a tracker client, reusing its credential."""
    kind = str(getattr(client, "tracker_type", "") or "").lower()
    if kind == "github":

        async def github_headers() -> dict[str, str]:
            headers = dict(await client._get_auth_headers())
            headers["Accept"] = "application/vnd.github+json"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
            return headers

        base = getattr(client, "API_BASE_URL", "https://api.github.com")
        return GitHubActions(HttpTransport(base, github_headers, budget))
    if kind == "gitlab":
        token = getattr(client, "api_key", None) or ""

        async def gitlab_headers() -> dict[str, str]:
            return {"PRIVATE-TOKEN": token}

        base = f"{str(getattr(client, 'url', 'https://gitlab.com')).rstrip('/')}/api/v4"
        return GitLabCi(HttpTransport(base, gitlab_headers, budget))
    return None


async def ci_health_report(ctx: Optional[Context] = None, **arguments: Any) -> dict:
    """Collect the CI health report for the calling flow.

    Args:
        ctx: MCP request context.
        **arguments: :class:`CiHealthReport` fields.

    Returns:
        The report (see the flow guide for its shape).
    """
    context = flow_state_tools.caller_context(NAME)
    try:
        request = CiHealthReport(**arguments)
    except ValidationError as exc:
        error = exc.errors()[0]
        where = ".".join(str(p) for p in error.get("loc", ()))
        raise ToolError(f"ci_health_request_invalid: {where}: {error['msg']}") from None
    # The full request: an approved call is replayed from these stored
    # arguments, ledger updates included.
    await flow_state_tools._approved(
        NAME, context, request.model_dump(mode="json"), ctx
    )
    now = datetime.now(UTC)

    with get_session_factory()() as db:

        def prepare() -> dict[str, Any]:
            bound = flow_state_tools.bound_execution(db, context, NAME)
            account_id = bound.account_id
            projects: list[tuple[str, Optional[models.Project], str]] = []
            if request.repositories:
                for path in request.repositories:
                    found = crud_project.list_by_repository_path(
                        db,
                        account_id=account_id,
                        path=path,
                        tracker_types=list(SUPPORTED_TRACKERS),
                    )
                    if len(found) == 1:
                        projects.append((path, found[0], ""))
                    else:
                        reason = "not synced" if not found else "ambiguous path"
                        projects.append((path, None, reason))
            else:
                for project_id in _default_projects(bound.flow):
                    project = crud_project.get(db, id=project_id, account_id=account_id)
                    if project is not None:
                        projects.append((project.slug or project.name, project, ""))
            row = crud_flow_state.get_state(
                db,
                account_id=account_id,
                flow_id=bound.flow.id,
                key=ledger_mod.LEDGER_KEY,
            )
            outcomes = crud_flow_execution.list_outcomes(
                db,
                flow_id=bound.flow.id,
                account_id=account_id,
                limit=OUTCOME_LOOKBACK,
            )
            details: Any = bound.execution.trigger_event_details or {}
            payload = details.get("payload") if isinstance(details, dict) else None
            return {
                "bound": bound,
                "projects": projects,
                "ledger": row.value if row else None,
                "version": row.version if row else 0,
                "published": published_pull_requests(outcomes),
                "precheck": (payload or {}).get("precheck")
                if isinstance(payload, dict)
                else None,
            }

        prepared = await run_db_off_loop(prepare)
        bound = prepared["bound"]
        budget = Budget(
            max_requests=request.max_requests, floor=request.rate_limit_floor
        )
        targets: list[Target] = []
        unavailable: list[dict[str, str]] = []
        from preloop.api.common import get_tracker_client

        for path, project, reason in prepared["projects"]:
            if project is None:
                unavailable.append({"repository": path, "reason": reason})
                continue
            try:
                client = await get_tracker_client(
                    project.organization_id, project.id, db, bound.user
                )
            except HTTPException as exc:
                unavailable.append(
                    {"repository": path, "reason": f"tracker access {exc.status_code}"}
                )
                continue
            adapter = _adapter(client, budget)
            if adapter is None:
                unavailable.append(
                    {"repository": path, "reason": "unsupported tracker"}
                )
                continue
            targets.append(Target(adapter, project.slug or path))
        if not targets:
            raise ToolError(
                "ci_health_no_repository: none of the repositories is a synced "
                "GitHub or GitLab project in this account: "
                + "; ".join(f"{u['repository']} ({u['reason']})" for u in unavailable)
            )

        # Provider reads can take minutes; no database connection is held
        # across them. Only scalar ids leave this session.
        account_id = bound.account_id
        flow_id = bound.flow.id
        execution_id = bound.execution.id

    options = Options(
        window_hours=request.window_hours,
        branches=request.branches,
        critical_path_jobs=request.critical_path_jobs,
        protected_branches=request.protected_branches,
        max_runs=request.max_runs,
        max_failed_logs=request.max_failed_logs,
        report_only=request.report_only,
        report_only_days=request.report_only_days,
        focus_urls=precheck_focus(prepared["precheck"]),
    )
    report, ledger = await collect(
        targets,
        ledger_value=prepared["ledger"],
        now=now,
        options=options,
        ledger_updates=[u.model_dump() for u in request.ledger_updates],
        published_pull_requests=prepared["published"],
        execution_id=str(execution_id),
        unavailable=unavailable,
    )
    ledger_mod.fit(
        ledger, crud_flow_state.MAX_VALUE_BYTES, crud_flow_state.encoded_size
    )

    def store(db: Any) -> Optional[int]:
        # A fresh session: the binding is checked again before the write, and
        # the compare-and-set guards against a concurrent run.
        flow_state_tools.bound_execution(db, context, NAME)
        try:
            row = crud_flow_state.put_state(
                db,
                account_id=account_id,
                flow_id=flow_id,
                key=ledger_mod.LEDGER_KEY,
                value=ledger,
                expected_version=prepared["version"],
                execution_id=execution_id,
            )
        except crud_flow_state.FlowStateError as exc:
            db.rollback()
            logger.info("CI health ledger not saved: %s", exc.code)
            return None
        return row.version

    with get_session_factory()() as db:
        version = await run_db_off_loop(lambda: store(db))

    report["ledger"]["saved"] = version is not None
    report["ledger"]["version"] = version
    report["requests"] = {
        "used": budget.used,
        "max": budget.max_requests,
        "provider_remaining": budget.remaining,
        "stopped": budget.stopped,
    }
    report["precheck_focus"] = sorted(options.focus_urls)[:20]
    return report


def register_ci_health_tool(mcp: DynamicFastMCP) -> None:
    """Register ``ci_health_report`` with the catalog schema.

    Args:
        mcp: The server.
    """
    tool = FunctionTool.from_function(
        _wire_handler,
        name=NAME,
        description=CI_HEALTH_REPORT_TOOL["description"],
        output_schema=None,
    )
    tool.parameters = CI_HEALTH_REPORT_TOOL["schema"]
    mcp.add_tool(tool)


async def _wire_handler(
    repositories: Optional[list[str]] = None,
    window_hours: Optional[int] = None,
    branches: Optional[list[str]] = None,
    protected_branches: Optional[list[str]] = None,
    critical_path_jobs: Optional[list[str]] = None,
    max_runs: Optional[int] = None,
    max_failed_logs: Optional[int] = None,
    max_requests: Optional[int] = None,
    rate_limit_floor: Optional[int] = None,
    report_only: Optional[str] = None,
    report_only_days: Optional[int] = None,
    ledger_updates: Optional[list[dict[str, Any]]] = None,
    ctx: Optional[Context] = None,
) -> dict:
    """FastMCP needs named parameters; drop the ones the caller omitted."""
    given = {
        "repositories": repositories,
        "window_hours": window_hours,
        "branches": branches,
        "protected_branches": protected_branches,
        "critical_path_jobs": critical_path_jobs,
        "max_runs": max_runs,
        "max_failed_logs": max_failed_logs,
        "max_requests": max_requests,
        "rate_limit_floor": rate_limit_floor,
        "report_only": report_only,
        "report_only_days": report_only_days,
        "ledger_updates": ledger_updates,
    }
    return await ci_health_report(
        ctx=ctx, **{k: v for k, v in given.items() if v is not None}
    )
