"""Pull and merge request activity per Fleet Map node, bounded by a window.

Two sources, both account-scoped and capped:

* Successful governed tool calls that open, comment on, review or merge a
  pull or merge request (``runtime_session_activity``, partial index
  ``ix_runtime_session_activity_code_tools``). A call made inside a flow run
  belongs to the flow; any other call belongs to the managed agent whose
  runtime principal made it, and rolls up to the agent's owner.
* Merges the forge reported by webhook (``issue_cost_pull_request``), which
  belong to the flows whose runs published or reviewed that pull request.

The optional work history additionally joins bounded tracker snapshots and
explicit execution subjects/links through ``fleet_work`` CRUD.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.models.crud import fleet_map as fleet_crud
from preloop.models.crud import fleet_work as work_crud
from preloop.schemas.fleet_map import (
    CodeAction,
    FleetCodeActivityResponse,
    FleetCodeCounts,
    FleetCodeEvent,
    FleetCodeFlow,
)
from preloop.services.fleet_map import _allowed, _reject_report_key, canonical_model
from preloop.services.fleet_work import build_work_items

logger = logging.getLogger(__name__)

#: Rows read from each source; the response says when a cap was hit.
CALL_LIMIT = 5000
MERGE_LIMIT = 500
EVENT_LIMIT = 40
FLOW_LIMIT = 200
GATEWAY_LIMIT = 5000
#: "Any time" still reads a bounded window.
MAX_WINDOW = timedelta(days=90)

_OPENED = {"create_pull_request", "create_merge_request"}
_MERGED = {"merge_pull_request", "merge_merge_request"}
_REVIEWED = {
    "approve_merge_request",
    "create_pull_request_review",
    "pull_request_review_write",
    "submit_pending_pull_request_review",
}
# Comment tools that only ever target a pull or merge request. The generic
# ``add_comment`` counts only when the call recorded a pull request target
# (``code_target`` in the activity metadata, set by the tool wrapper).
_COMMENTED = {
    "add_comment_to_pending_review",
    "create_merge_request_note",
}
_TARGETED_COMMENT = "add_comment"
_PR_PATH = re.compile(
    r"^/(?P<repo>.+?)/(?:-/)?(?:pull|pulls|merge_requests|pull-requests)/(?P<n>\d+)"
)


def code_action(tool_name: str | None, metadata: Any = None) -> CodeAction | None:
    """Classify one successful tool call as a pull or merge request action.

    ``update_pull_request`` counts only when it carried a review: the
    arguments summary keeps the key names, never the values.

    Args:
        tool_name: Client-visible tool name.
        metadata: The activity row's metadata, with ``arguments_summary``.

    Returns:
        The action, or None for anything that is not one.
    """
    work_ref = metadata.get("work_ref") if isinstance(metadata, dict) else None
    if isinstance(work_ref, dict):
        # A classified call (built-in, GitHub/GitLab MCP, gh/glab) is
        # authoritative: only pull/merge request actions count here.
        if work_ref.get("kind") != "pull_request":
            return None
        action = work_ref.get("action")
        return (
            action if action in {"opened", "commented", "reviewed", "merged"} else None
        )
    name = (tool_name or "").strip()
    if name in _OPENED:
        return "opened"
    if name in _MERGED:
        return "merged"
    if name in _REVIEWED:
        return "reviewed"
    if name in _COMMENTED:
        return "commented"
    if name == _TARGETED_COMMENT:
        target = metadata.get("code_target") if isinstance(metadata, dict) else None
        return "commented" if target == "pull_request" else None
    if name == "update_pull_request":
        summary = (
            metadata.get("arguments_summary") if isinstance(metadata, dict) else None
        )
        if isinstance(summary, dict) and "review_action" in summary:
            return "reviewed"
    return None


_PR_URL = re.compile(r"/(?:pull|pulls|merge_requests|pull-requests)/\d+")


def issue_count(metadata: Any) -> str | None:
    """The ``FleetCodeCounts`` issue field a classified call increments."""
    work_ref = metadata.get("work_ref") if isinstance(metadata, dict) else None
    if not isinstance(work_ref, dict) or work_ref.get("kind") != "issue":
        return None
    return {
        "issue_opened": "issues_opened",
        "issue_updated": "issues_updated",
        "commented": "issues_commented",
    }.get(str(work_ref.get("action")))


def pull_request_target(arguments: Any) -> bool:
    """Whether a generic comment call is aimed at a pull or merge request.

    True for a ``target`` naming a pull or merge request URL, or a GitLab
    ``!N`` reference, and for an inline comment (``path``/``line`` only exist
    on a diff). Only this boolean is kept, never the target itself.
    """
    if not isinstance(arguments, dict):
        return False
    if arguments.get("path") and arguments.get("line") is not None:
        return True
    target = arguments.get("target")
    if not isinstance(target, str):
        return False
    return bool(_PR_URL.search(target)) or bool(re.search(r"![0-9]+$", target))


def short_pr(url: str | None) -> str | None:
    """``https://github.com/acme/api/pull/42`` reads as ``acme/api#42``."""
    if not url:
        return None
    match = _PR_PATH.match(urlparse(url).path or "")
    if not match:
        return None
    mark = "!" if "merge_requests" in url else "#"
    return f"{match['repo']}{mark}{match['n']}"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def build_code_activity(
    db: Session,
    *,
    user: models.User,
    active_within_minutes: int | None = None,
    now: datetime | None = None,
) -> FleetCodeActivityResponse:
    """Count pull and merge request actions per map node in the window.

    Args:
        db: Request database session.
        user: Authenticated user in the selected account.
        active_within_minutes: Window; None reads the last 90 days.
        now: Clock override for tests.

    Returns:
        Counts per agent, flow and person node, the flows to place, and the
        most recent events.

    Raises:
        HTTPException: The caller cannot view the account's fleet.
    """
    _reject_report_key(user)
    account = crud.crud_account.get(db, id=user.account_id)
    owner = account is not None and str(account.primary_user_id) == str(user.id)
    if not _allowed(db, user, "view_device_fleet", owner):
        raise HTTPException(
            403, "Insufficient permissions. Required: view_device_fleet"
        )
    see_agents = _allowed(db, user, "view_agents", owner)
    see_flows = _allowed(db, user, "view_flows", owner)
    see_users = _allowed(db, user, "view_users", owner)
    current = now or datetime.now(UTC)
    window = (
        timedelta(minutes=active_within_minutes)
        if active_within_minutes
        else MAX_WINDOW
    )
    since = current - min(window, MAX_WINDOW)
    result = FleetCodeActivityResponse(
        active_within_minutes=active_within_minutes, since=since
    )
    account_id = str(user.account_id)
    if not (see_agents or see_flows):
        return result

    calls = fleet_crud.list_code_tool_calls(
        db,
        account_id=account_id,
        since=since,
        until=current,
        limit=CALL_LIMIT + 1,
        include_work=True,
    )
    result.truncated = len(calls) > CALL_LIMIT
    calls = calls[:CALL_LIMIT]
    flow_of_execution: dict[str, str] = {}
    flow_rows: dict[str, tuple[str, Any]] = {}
    if see_flows:
        for execution_id, flow_id, name, model_id in fleet_crud.flows_for_executions(
            db,
            account_id=account_id,
            execution_ids={c.flow_execution_id for c in calls if c.flow_execution_id},
        ):
            flow_of_execution[str(execution_id)] = str(flow_id)
            flow_rows[str(flow_id)] = (name, model_id)
    agents_by_principal: dict[tuple[str, str], models.ManagedAgent] = {}
    if see_agents:
        for agent in fleet_crud.agents_for_principals(
            db,
            account_id=account_id,
            principals={
                (c.runtime_principal_type, c.runtime_principal_id)
                for c in calls
                if not c.flow_execution_id
                and c.runtime_principal_type
                and c.runtime_principal_id
            },
        ):
            agents_by_principal[
                (agent.session_source_type, agent.session_source_id)
            ] = agent

    counts: dict[str, FleetCodeCounts] = defaultdict(FleetCodeCounts)
    flow_tool_merges: dict[str, list[FleetCodeEvent]] = defaultdict(list)
    flow_forge_merges: dict[str, int] = defaultdict(int)
    events: list[FleetCodeEvent] = []
    work = build_work_items(
        db,
        account_id=account_id,
        since=since,
        until=current,
        calls=calls,
        flow_of_execution=flow_of_execution,
        agents_by_principal=agents_by_principal,
        see_flows=see_flows,
        calls_truncated=result.truncated,
        pull_request_limit=MERGE_LIMIT,
    )
    result.work, result.work_truncated = work.work, work.truncated
    result.relations = work.relations or []
    result.relations_truncated = work.relations_truncated
    last_flow, pull_requests = work.flow_activity, work.merged_pull_requests
    result.truncated |= work.pull_requests_truncated

    def count(node_id: str, action: CodeAction) -> None:
        item = counts[node_id]
        setattr(item, action, getattr(item, action) + 1)

    from preloop.services.fleet_work_refs import work_key, work_url

    for call in calls:
        issue_field = issue_count(call.metadata_)
        if issue_field is not None:
            flow_id = flow_of_execution.get(str(call.flow_execution_id or ""))
            actor = (
                None
                if flow_id or call.flow_execution_id
                else agents_by_principal.get(
                    (call.runtime_principal_type or "", call.runtime_principal_id or "")
                )
            )
            target = (
                f"flow:{flow_id}"
                if flow_id
                else (f"agent:{actor.id}" if actor is not None else None)
            )
            if target:
                item = counts[target]
                setattr(item, issue_field, getattr(item, issue_field) + 1)
                if see_users and actor is not None and actor.owner_user_id:
                    owner_item = counts[f"user:{actor.owner_user_id}"]
                    setattr(
                        owner_item, issue_field, getattr(owner_item, issue_field) + 1
                    )
                if flow_id:
                    at = _aware(call.timestamp)
                    last_flow[flow_id] = max(last_flow.get(flow_id, at), at)
            continue
        action = code_action(call.tool_name, call.metadata_)
        if action is None:
            continue
        at = _aware(call.timestamp)
        owner_id: str | None = None
        flow_id = flow_of_execution.get(str(call.flow_execution_id or ""))
        if flow_id:
            node_id = f"flow:{flow_id}"
            last_flow[flow_id] = max(last_flow.get(flow_id, at), at)
        else:
            actor = agents_by_principal.get(
                (call.runtime_principal_type or "", call.runtime_principal_id or "")
            )
            if actor is None:
                continue
            node_id = f"agent:{actor.id}"
            if see_users and actor.owner_user_id:
                owner_id = f"user:{actor.owner_user_id}"
                count(owner_id, action)
        work_ref = (
            call.metadata_.get("work_ref") if isinstance(call.metadata_, dict) else None
        )
        found = work_key(work_ref)
        event = FleetCodeEvent(
            id=str(call.id),
            at=at,
            action=action,
            node_id=node_id,
            owner_id=owner_id,
            pr=found[1] if found else None,
            url=work_url(work_ref),
            work_id=f"work:{found[0]}:{found[1]}" if found else None,
            source=work_ref.get("via") if isinstance(work_ref, dict) else "builtin",
        )
        if flow_id and action == "merged":
            # The forge reports the same merge; settled after the webhooks.
            flow_tool_merges[flow_id].append(event)
            continue
        count(node_id, action)
        if len(events) < EVENT_LIMIT:
            events.append(event)

    if see_flows:
        merges = [
            (row, flow_id)
            for row in pull_requests[:MERGE_LIMIT]
            if row.merged_at and since <= _aware(row.merged_at) <= current
            for flow_id in (row.merge_flows or [])[:5]
        ]
        result.truncated |= any(len(row.merge_flows or []) > 5 for row in pull_requests)
        missing = {flow_id for _, flow_id in merges if str(flow_id) not in flow_rows}
        missing.update(flow_id for flow_id in last_flow if flow_id not in flow_rows)
        for flow_id, name, model_id in fleet_crud.flows_by_id(
            db, account_id=account_id, flow_ids=missing
        ):
            flow_rows[str(flow_id)] = (name, model_id)
        for row, flow_id in merges:
            flow_id = str(flow_id)
            if flow_id not in flow_rows:
                continue
            at = _aware(row.merged_at)
            flow_forge_merges[flow_id] += 1
            count(f"flow:{flow_id}", "merged")
            last_flow[flow_id] = max(last_flow.get(flow_id, at), at)
            events.append(
                FleetCodeEvent(
                    id=f"merge:{row.pr_key}:{flow_id}",
                    at=at,
                    action="merged",
                    node_id=f"flow:{flow_id}",
                    pr=short_pr(row.pr_key),
                    url=row.pr_key if row.pr_key.startswith("https://") else None,
                    work_id=f"work:pull_request:{key}"
                    if (key := (short_pr(row.pr_key) or "").lower())
                    else None,
                    source="webhook",
                )
            )

    # A flow that merged through a tool call is also credited by the forge's
    # merge webhook. Count the larger of the two, never both.
    for flow_id, merged in flow_tool_merges.items():
        for event in merged[flow_forge_merges.get(flow_id, 0) :]:
            count(event.node_id, "merged")
            events.append(event)

    run_counts: dict[str, int] = {}
    used_models: dict[str, Counter[str]] = defaultdict(Counter)
    if see_flows:
        from preloop.models.crud import fleet_activity

        runs = fleet_activity.flow_runs(
            db,
            account_id=account_id,
            since=since,
            until=current,
            limit=FLOW_LIMIT + 1,
        )
        result.truncated |= len(runs) > FLOW_LIMIT
        for run in runs[:FLOW_LIMIT]:
            flow_id = str(run.flow_id)
            flow_rows.setdefault(flow_id, (run.name, run.ai_model_id))
            run_counts[flow_id] = int(run.runs)
            at = _aware(run.last_at)
            last_flow[flow_id] = max(last_flow.get(flow_id, at), at)
        gateway = fleet_activity.gateway_edges(
            db,
            account_id=account_id,
            since=since,
            until=current,
            limit=GATEWAY_LIMIT + 1,
        )
        result.truncated |= len(gateway) > GATEWAY_LIMIT
        unseen = set()
        for row in gateway[:GATEWAY_LIMIT]:
            if not row.flow_id or not row.ai_model_id:
                continue
            flow_id = str(row.flow_id)
            used_models[flow_id][str(row.ai_model_id)] += int(row.requests)
            at = _aware(row.last_at)
            last_flow[flow_id] = max(last_flow.get(flow_id, at), at)
            if flow_id not in flow_rows:
                unseen.add(flow_id)
        for flow_id, name, model_id in fleet_crud.flows_by_id(
            db, account_id=account_id, flow_ids=unseen
        ):
            flow_rows[str(flow_id)] = (name, model_id)
    model_nodes: dict[str, str] = {}
    for model_row in fleet_crud.models_by_id(
        db,
        account_id=account_id,
        model_ids={model_id for _, model_id in flow_rows.values() if model_id}
        | {model_id for used in used_models.values() for model_id in used},
    ):
        key, _ = canonical_model(
            getattr(model_row, "model_identifier", None)
            or model_row.name
            or str(model_row.id)
        )
        model_nodes[str(model_row.id)] = f"model:{key}"

    def flow_models(flow_id: str, model_id: Any) -> list[str]:
        """Observed ledger models, most requests first; else the configured one."""
        ranked: Counter[str] = Counter()
        for used, requests in used_models.get(flow_id, Counter()).items():
            if used in model_nodes:
                ranked[model_nodes[used]] += requests
        ordered = [
            node for node, _ in sorted(ranked.items(), key=lambda i: (-i[1], i[0]))
        ]
        if not ordered and model_id and str(model_id) in model_nodes:
            ordered = [model_nodes[str(model_id)]]
        return ordered[:5]

    # Most recently active first, so the cap keeps the freshest flows.
    ordered_flows = sorted(
        (flow_id for flow_id in flow_rows if flow_id in last_flow),
        key=lambda flow_id: (-last_flow[flow_id].timestamp(), flow_id),
    )
    result.truncated |= len(ordered_flows) > FLOW_LIMIT
    result.flows = [
        FleetCodeFlow(
            id=f"flow:{flow_id}",
            resource_id=flow_id,
            label=name,
            href=f"/console/flows/{flow_id}",
            model_node_id=(observed[0] if observed else None),
            model_node_ids=observed,
            run_count=run_counts.get(flow_id, 0),
            last_active_at=last_flow.get(flow_id),
        )
        for flow_id in ordered_flows[:FLOW_LIMIT]
        for name, model_id in [flow_rows[flow_id]]
        for observed in [flow_models(flow_id, model_id)]
    ]
    result.counts = dict(counts)
    events.sort(key=lambda item: item.at, reverse=True)
    # Drop hidden flows before the cap, so visible events backfill it.
    result.events = events
    _drop_hidden_flows(db, user, result)
    result.events = result.events[:EVENT_LIMIT]
    return result


def visible_flow_ids(db: Session, user: models.User, flow_ids: set[str]) -> set[str]:
    """Flow ids the resource authorizer lets ``user`` view, as the flow lists do."""
    from preloop.models.crud import fleet_activity
    from preloop.plugins.account_hooks import VISIBLE_FLOW, filter_viewable

    rows = fleet_activity.flows_by_ids(
        db, account_id=user.account_id, flow_ids=flow_ids
    )
    return {str(row.id) for row in filter_viewable(db, user, VISIBLE_FLOW, rows)}


def _drop_hidden_flows(
    db: Session, user: models.User, result: FleetCodeActivityResponse
) -> None:
    """Remove flows the caller may not view from every part of the response."""
    named = {
        node.removeprefix("flow:")
        for node in (
            [flow.id for flow in result.flows]
            + list(result.counts)
            + [event.node_id for event in result.events]
            + [node for item in result.work for node in item.node_ids]
            + [relation.source for relation in result.relations]
        )
        if node.startswith("flow:")
    }
    if not named:
        return
    visible = visible_flow_ids(db, user, named)

    def shown(node: str) -> bool:
        return not node.startswith("flow:") or node.removeprefix("flow:") in visible

    result.flows = [flow for flow in result.flows if shown(flow.id)]
    result.counts = {node: c for node, c in result.counts.items() if shown(node)}
    result.events = [event for event in result.events if shown(event.node_id)]
    for item in result.work:
        item.node_ids = [node for node in item.node_ids if shown(node)]
    result.relations = [r for r in result.relations if shown(r.source)]


def emit_code_tool_event(
    *,
    account_id: Any,
    tool_name: str | None,
    metadata: Any,
    managed_agent_id: Any = None,
    flow_id: Any = None,
    execution_id: Any = None,
    runtime_session_id: Any = None,
    timestamp: str | None = None,
) -> None:
    """Broadcast one successful code tool call on ``code_activity``.

    Silent for any other tool, and never raises: a live map light must not
    fail the tool call that caused it.
    """
    action: str | None = code_action(tool_name, metadata)
    # A merge inside a flow run is announced by the forge's merge webhook,
    # which names the pull request; announcing the tool call too would show
    # one merge twice.
    if action == "merged" and flow_id:
        return
    work_ref = metadata.get("work_ref") if isinstance(metadata, dict) else None
    if action is None and isinstance(work_ref, dict):
        action = str(work_ref.get("action") or "") or None
    if action is None:
        return
    from preloop.services.fleet_work_refs import work_key, work_url

    key = work_key(work_ref)
    try:
        from preloop.services.account_realtime import (
            ACCOUNT_TOPIC_CODE_ACTIVITY,
            build_account_event,
            emit_account_event,
        )

        emit_account_event(
            build_account_event(
                account_id=str(account_id),
                topic=ACCOUNT_TOPIC_CODE_ACTIVITY,
                event_type="code_activity",
                payload={
                    "action": action,
                    "tool_name": tool_name,
                    "work_id": f"work:{key[0]}:{key[1]}" if key else None,
                    "kind": work_ref.get("kind")
                    if isinstance(work_ref, dict)
                    else None,
                    "url": work_url(work_ref),
                    "source": work_ref.get("via")
                    if isinstance(work_ref, dict)
                    else None,
                    "managed_agent_id": str(managed_agent_id)
                    if managed_agent_id
                    else None,
                    "flow_id": str(flow_id) if flow_id else None,
                    "timestamp": timestamp,
                },
                execution_id=str(execution_id) if execution_id else None,
                runtime_session_id=str(runtime_session_id)
                if runtime_session_id
                else None,
            )
        )
    except Exception:  # noqa: BLE001 - realtime is best effort
        logger.debug("code_activity broadcast failed", exc_info=True)


def emit_merge_event(db: Session, *, account_id: Any, pr_key: str, at: Any) -> None:
    """Broadcast a forge-reported merge to the flows that worked on it.

    Reads at most five linked flow runs by the unique ``(account, pr_key)``
    pair. Never raises.
    """
    try:
        from preloop.services.account_realtime import (
            ACCOUNT_TOPIC_CODE_ACTIVITY,
            build_account_event,
            emit_account_event,
        )

        flow_ids = work_crud.flows_for_pull_request(
            db, account_id=account_id, pr_key=pr_key
        )
        emit_account_event(
            build_account_event(
                account_id=str(account_id),
                topic=ACCOUNT_TOPIC_CODE_ACTIVITY,
                event_type="code_activity",
                payload={
                    "action": "merged",
                    "flow_ids": flow_ids,
                    "pr": short_pr(pr_key),
                    "url": pr_key if pr_key.startswith("https://") else None,
                    "timestamp": at.isoformat() if hasattr(at, "isoformat") else None,
                },
            )
        )
    except Exception:  # noqa: BLE001 - realtime is best effort
        logger.debug("merge broadcast failed", exc_info=True)
