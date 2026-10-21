"""Bounded gateway totals joined to the Fleet Map's visible node ids."""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.models.crud.fleet_map import list_resources
from preloop.models.crud.fleet_usage import usage_by_node
from preloop.plugins.account_hooks import VISIBLE_AI_MODEL, filter_viewable
from preloop.schemas.fleet_map import (
    FleetNodeTotals,
    FleetUsageResponse,
    FleetUsageTotals,
)
from preloop.services.built_in_models import built_in_models_enabled
from preloop.services.fleet_map import _allowed, _reject_report_key, canonical_model

MODEL_LIMIT = 1000
MAX_WINDOW = timedelta(days=90)


def build_fleet_usage(
    db: Session,
    *,
    user: models.User,
    active_within_minutes: int | None = None,
    now: datetime | None = None,
) -> FleetUsageResponse:
    """Read SQL totals for authorized model and provider nodes in one window.

    Args:
        db: Request database session.
        user: Authenticated user in the selected account.
        active_within_minutes: Requested window; None means the last 90 days.
        now: Optional UTC clock override for deterministic tests.

    Returns:
        Model/provider dictionaries keyed exactly as the fleet graph, with
        explicit source truncation and missing-data flags.

    Raises:
        HTTPException: The caller cannot read the account's fleet.
    """
    _reject_report_key(user)
    account = crud.crud_account.get(db, id=user.account_id)
    owner = account is not None and str(account.primary_user_id) == str(user.id)
    if not _allowed(db, user, "view_device_fleet", owner):
        raise HTTPException(
            403, "Insufficient permissions. Required: view_device_fleet"
        )
    current = now or datetime.now(UTC)
    current = (
        current.replace(tzinfo=UTC)
        if current.tzinfo is None
        else current.astimezone(UTC)
    )
    window = (
        timedelta(minutes=active_within_minutes)
        if active_within_minutes is not None
        else MAX_WINDOW
    )
    result = FleetUsageResponse(
        active_within_minutes=active_within_minutes,
        since=current - min(window, MAX_WINDOW),
        until=current,
    )
    _node_totals(
        db,
        user=user,
        owner=owner,
        result=result,
        minutes=active_within_minutes,
        now=current,
    )
    if not _allowed(db, user, "view_ai_models", owner):
        return result
    rows = cast(
        list[models.AIModel],
        list_resources(
            db,
            source="models",
            account_id=str(user.account_id),
            limit=MODEL_LIMIT + 1,
            include_built_in=built_in_models_enabled(),
        ),
    )
    result.truncated = len(rows) > MODEL_LIMIT
    result.partial = result.truncated
    rows = rows[:MODEL_LIMIT]
    owned = [row for row in rows if row.account_id is not None]
    visible_ids = {row.id for row in filter_viewable(db, user, VISIBLE_AI_MODEL, owned)}
    mapping = [
        (
            row.id,
            f"model:{canonical_model(row.model_identifier or row.name or str(row.id))[0]}",
            f"provider:{row.provider_name}",
        )
        for row in rows
        if row.account_id is None or row.id in visible_ids
    ]
    targets: dict[Literal["model", "provider"], dict[str, FleetUsageTotals]] = {
        "model": result.models,
        "provider": result.providers,
    }
    for grouping, target in targets.items():
        for aggregate in usage_by_node(
            db,
            account_id=user.account_id,
            model_nodes=mapping,
            grouping=grouping,
            since=result.since,
            until=result.until,
        ):
            values = dict(aggregate)
            node_id = values.pop("node_id")
            values["partial"] |= result.truncated
            target[node_id] = FleetUsageTotals(**values)
            result.partial |= target[node_id].partial
    return result


#: Grouped rows read per node source; hitting it marks the response partial.
NODE_LIMIT = 5000
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
_COUNT_FIELDS = (
    "request_count",
    "tool_calls",
    "approvals_requested",
    "approvals_approved",
    "approvals_declined",
    "prs_opened",
    "prs_reviewed",
    "prs_merged",
    "review_comments",
    "issues_touched",
    "prs_touched",
)


def _add(target: FleetNodeTotals, source: FleetNodeTotals | dict[str, Any]) -> None:
    """Fold totals into ``target`` with the ``FleetUsageTotals`` honesty rules."""
    values = source if isinstance(source, dict) else source.model_dump()
    had_usage = target.request_count > 0
    for name in _COUNT_FIELDS:
        setattr(target, name, getattr(target, name) + int(values.get(name) or 0))
    if not int(values.get("request_count") or 0):
        return
    for name in _TOKEN_FIELDS:
        value = values.get(name)
        if value is not None:
            setattr(target, name, (getattr(target, name) or 0) + int(value))
    currency = values.get("currency")
    cost = values.get("estimated_cost")
    if not had_usage:
        target.currency = currency
        target.estimated_cost = None if cost is None else float(cost)
    elif target.currency is None or currency != target.currency:
        # Mixed (or already mixed) currencies: no conversion is attempted.
        target.currency = target.estimated_cost = None
        target.partial = True
    elif cost is not None:
        target.estimated_cost = (target.estimated_cost or 0) + float(cost)
    target.partial |= bool(values.get("partial"))


def _node_totals(
    db: Session,
    *,
    user: models.User,
    owner: bool,
    result: FleetUsageResponse,
    minutes: int | None,
    now: datetime,
) -> None:
    """Fill the agent, person, flow, device and work dictionaries.

    One grouped read per source: gateway rows by principal, flow, user and
    linked work item; tool calls; approvals; plus the code-activity build for
    PR and issue actions. Keys are exactly the fleet graph's node ids.
    """
    from collections import defaultdict

    from preloop.models.crud import fleet_activity
    from preloop.models.crud.fleet_map import list_resources as read_source
    from preloop.plugins.account_hooks import VISIBLE_MANAGED_AGENT
    from preloop.plugins.account_hooks import filter_viewable as viewable
    from preloop.services.fleet_code_activity import build_code_activity, short_pr
    from preloop.services.fleet_work import _identity

    see_agents = _allowed(db, user, "view_agents", owner)
    see_flows = _allowed(db, user, "view_flows", owner)
    see_users = _allowed(db, user, "view_users", owner)
    if not (see_agents or see_flows):
        return
    account_id = user.account_id
    since, until = result.since, result.until
    totals: dict[str, FleetNodeTotals] = defaultdict(FleetNodeTotals)
    agents: list[Any] = []
    if see_agents:
        loaded = read_source(
            db, source="agents", account_id=str(account_id), limit=NODE_LIMIT + 1
        )
        result.truncated |= len(loaded) > NODE_LIMIT
        agents = viewable(db, user, VISIBLE_MANAGED_AGENT, loaded[:NODE_LIMIT])
    by_principal = {
        (row.session_source_type, row.session_source_id): f"agent:{row.id}"
        for row in agents
        if getattr(row, "session_source_type", None)
    }
    agent_ids = {f"agent:{row.id}" for row in agents}

    def capped(rows: list[Any]) -> list[Any]:
        if len(rows) > NODE_LIMIT:
            result.truncated = result.partial = True
        return rows[:NODE_LIMIT]

    def usage(grouping: fleet_activity.UsageGrouping) -> list[Any]:
        return capped(
            fleet_activity.usage_totals(
                db,
                account_id=account_id,
                grouping=grouping,
                since=since,
                until=until,
                limit=NODE_LIMIT + 1,
            )
        )

    if see_agents:
        for row in usage("principal"):
            node = by_principal.get((row["key_a"], row["key_b"]))
            if node:
                _add(totals[node], dict(row))
    if see_flows:
        for row in usage("flow"):
            _add(totals[f"flow:{row['key_a']}"], dict(row))
        for row in usage("pull_request"):
            key = (short_pr(row["key_a"]) or "").lower()
            if key:
                _add(totals[f"work:pull_request:{key}"], dict(row))
        for row in usage("issue"):
            identity = _identity(row["key_a"], "issue") or _identity(
                row["key_b"], "issue"
            )
            if identity:
                _add(totals[f"work:issue:{identity[0]}"], dict(row))
    direct_users = usage("user") if see_users else []

    for row in capped(
        fleet_activity.tool_call_counts(
            db, account_id=account_id, since=since, until=until, limit=NODE_LIMIT + 1
        )
    ):
        node = (
            f"flow:{row.flow_id}"
            if row.flow_id and see_flows
            else by_principal.get((row.principal_type, row.principal_id))
        )
        if node:
            totals[node].tool_calls += int(row.tool_calls)
    for row in capped(
        fleet_activity.approval_counts(
            db, account_id=account_id, since=since, until=until, limit=NODE_LIMIT + 1
        )
    ):
        node = (
            f"flow:{row.flow_id}"
            if row.flow_id and see_flows
            else (
                f"agent:{row.managed_agent_id}"
                if f"agent:{row.managed_agent_id}" in agent_ids
                else None
            )
        )
        if not node:
            continue
        item = totals[node]
        if row.status != "cancelled":
            item.approvals_requested += int(row.count)
        if row.status == "approved":
            item.approvals_approved += int(row.count)
        elif row.status in {"declined", "expired"}:
            item.approvals_declined += int(row.count)

    activity = build_code_activity(
        db, user=user, active_within_minutes=minutes, now=now
    )
    result.truncated |= activity.truncated or activity.relations_truncated
    for node, counts in activity.counts.items():
        if node.startswith("user:"):
            continue
        item = totals[node]
        item.prs_opened += counts.opened
        item.prs_reviewed += counts.reviewed
        item.prs_merged += counts.merged
        item.review_comments += counts.commented
    touched: dict[str, set[str]] = defaultdict(set)
    for relation in activity.relations:
        if relation.kind != "touched":
            continue
        touched[relation.source].add(relation.target)
        totals[relation.target].tool_calls += relation.count
    for node, targets in touched.items():
        totals[node].issues_touched = sum(t.startswith("work:issue:") for t in targets)
        totals[node].prs_touched = sum(
            t.startswith("work:pull_request:") for t in targets
        )

    # People: the agents they own, plus their own direct gateway requests.
    if see_users:
        for row in agents:
            node = f"agent:{row.id}"
            if row.owner_user_id and node in totals:
                _add(totals[f"user:{row.owner_user_id}"], totals[node])
        for row in direct_users:
            _add(totals[f"user:{row['key_a']}"], dict(row))

    # Devices: the managed agents they host (explicit inventory links).
    from preloop.plugins.base import get_plugin_manager

    service = get_plugin_manager().get_service("device_fleet")
    if service is not None and agents:
        try:
            projection = service.read(db, account_id=account_id)
        except Exception:  # noqa: BLE001 - optional projection
            projection = None
        for device in (projection or {}).get("items", [])[:NODE_LIMIT]:
            hosted = {
                f"agent:{entry.get('managed_agent_id')}"
                for entry in device.get("agents", [])
                if entry.get("managed_agent_id")
            }
            for node in sorted(hosted):
                if node in totals:
                    _add(totals[f"device:{device['device_key']}"], totals[node])

    for node, item in totals.items():
        if not any(getattr(item, name) for name in _COUNT_FIELDS):
            continue
        target = {
            "agent": result.agents,
            "user": result.people,
            "flow": result.flows,
            "device": result.devices,
            "work": result.work,
        }.get(node.split(":", 1)[0])
        if target is not None:
            target[node] = item
            result.partial |= item.partial
