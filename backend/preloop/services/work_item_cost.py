"""Cost per work item: what each issue and pull request cost, and who drove it.

Contributors are flow executions and agentic runtime sessions. A contributor
is linked to the work items it worked on by the issue cost attribution of
flow runs and by classified ``work_ref`` tool calls. Its gateway spend in the
window is split evenly between its work items (see ``SPLIT_RULE``), then
issues are grouped with the pull requests that close them or that the issue
cost attribution tied to them.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import fleet_map as fleet_crud
from preloop.models.crud import work_item_cost as crud
from preloop.schemas.work_item_cost import (
    WorkItemCostReport,
    WorkItemGroup,
    WorkItemKind,
    WorkItemRef,
    WorkItemSlice,
)
from preloop.services.fleet_principals import agent_index, resolve_principal
from preloop.services.fleet_work_refs import parse_reference, work_key, work_url

#: Row caps per source; a hit sets ``truncated`` on the report.
LINK_LIMIT = 20_000
USAGE_SLICE_LIMIT = 50_000
#: Breakdown rows kept per dimension of one group.
SLICE_LIMIT = 8

#: Runtime principal type of a flow run's own model calls.
FLOW_RUN_PRINCIPAL = "flow_execution"
FLOW_RUN_AGENT = "flow_runs"

Contributor = tuple[str, str]  # ("execution" | "session", id)
ItemKey = tuple[WorkItemKind, str]


@dataclass
class _Totals:
    cost: float = 0.0
    tokens: float = 0.0
    requests: float = 0.0

    def add(self, cost: float, tokens: float, requests: float) -> None:
        self.cost += cost
        self.tokens += tokens
        self.requests += requests


@dataclass
class _Item:
    totals: _Totals = field(default_factory=_Totals)
    partial: bool = False
    executions: set[str] = field(default_factory=set)
    sessions: set[str] = field(default_factory=set)
    agents: dict[str, _Totals] = field(default_factory=lambda: defaultdict(_Totals))
    flows: dict[str, _Totals] = field(default_factory=lambda: defaultdict(_Totals))
    models: dict[str, _Totals] = field(default_factory=lambda: defaultdict(_Totals))
    users: dict[str, _Totals] = field(default_factory=lambda: defaultdict(_Totals))


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[ItemKey, ItemKey] = {}

    def find(self, key: ItemKey) -> ItemKey:
        self.parent.setdefault(key, key)
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def union(self, a: ItemKey, b: ItemKey) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            # Keep an issue as the root so a group leads with its issue.
            if root_a[0] == "pull_request" and root_b[0] == "issue":
                root_a, root_b = root_b, root_a
            self.parent[root_b] = root_a


def pull_request_key(url: Optional[str]) -> Optional[str]:
    """``owner/repo#N`` (or ``!N``) of a normalized pull request URL.

    Args:
        url: Pull/merge request URL as stored in ``pr_key``.

    Returns:
        The canonical key, or None when the URL names no pull request.
    """
    ref = parse_reference(url, kind="pull_request")
    if not ref or ref.get("kind") != "pull_request" or not ref.get("repo"):
        return None
    return f"{ref['repo']}{ref['ref']}"


def _number(value: Any, default: float = 0.0) -> float:
    return float(value) if value is not None else default


def _slices(values: dict[str, _Totals], labels: dict[str, str]) -> list[WorkItemSlice]:
    ordered = sorted(
        values.items(), key=lambda kv: (kv[1].cost, kv[1].tokens), reverse=True
    )
    return [
        WorkItemSlice(
            key=key,
            label=labels.get(key, key),
            estimated_cost=round(totals.cost, 6),
            total_tokens=int(round(totals.tokens)),
            request_count=round(totals.requests, 2),
        )
        for key, totals in ordered[:SLICE_LIMIT]
    ]


def build_report(
    db: Session,
    *,
    account_id: uuid.UUID,
    user: models.User,
    start: datetime,
    end: datetime,
    limit: int = 50,
    offset: int = 0,
) -> WorkItemCostReport:
    """Spend per work item over ``[start, end]``, most expensive group first.

    Args:
        db: Request database session.
        account_id: Selected membership's owning tenant.
        user: Caller, for agent visibility.
        start: Inclusive window start (timezone-aware).
        end: Inclusive window end (timezone-aware).
        limit: Groups returned.
        offset: Groups skipped.

    Returns:
        The report; ``total_groups`` and the totals cover every group.
    """
    links: dict[Contributor, set[ItemKey]] = defaultdict(set)
    urls: dict[ItemKey, str] = {}
    titles: dict[ItemKey, str] = {}
    uf = _UnionFind()
    truncated = False

    executions = crud.execution_links(
        db, account_id=account_id, since=start, until=end, limit=LINK_LIMIT
    )
    truncated |= len(executions) >= LINK_LIMIT
    for row in executions:
        contributor: Contributor = ("execution", str(row.execution_id))
        issue: Optional[ItemKey] = None
        if row.issue_key:
            issue = ("issue", str(row.issue_key))
            links[contributor].add(issue)
            if row.issue_url:
                urls[issue] = str(row.issue_url)
            if row.issue_title:
                titles[issue] = str(row.issue_title)
        if row.pr_key and (key := pull_request_key(row.pr_key)):
            pull: ItemKey = ("pull_request", key)
            links[contributor].add(pull)
            urls.setdefault(pull, str(row.pr_key))
            if issue is not None:
                uf.union(issue, pull)

    calls = crud.work_ref_calls(
        db, account_id=account_id, since=start, until=end, limit=LINK_LIMIT
    )
    truncated |= len(calls) >= LINK_LIMIT
    # A create call has no number yet: its closing references attach to the
    # pull requests the same contributor names in that repository.
    pending: dict[tuple[Contributor, str], set[str]] = defaultdict(set)
    for call in calls:
        contributor = (
            ("execution", str(call.flow_execution_id))
            if call.flow_execution_id
            else ("session", str(call.runtime_session_id))
        )
        ref = call.work_ref if isinstance(call.work_ref, dict) else {}
        found = work_key(ref)
        closes = [k for k in (ref.get("closes") or []) if isinstance(k, str)]
        if found is None:
            if ref.get("kind") == "pull_request" and isinstance(ref.get("repo"), str):
                pending[(contributor, ref["repo"])].update(closes)
            continue
        item: ItemKey = found
        links[contributor].add(item)
        if url := work_url(ref):
            urls.setdefault(item, url)
        if item[0] == "pull_request":
            for issue_key in closes:
                uf.union(("issue", issue_key), item)
    for (contributor, repo), closes in pending.items():
        named = [
            item
            for item in links.get(contributor, set())
            if item[0] == "pull_request" and item[1].startswith(f"{repo}#")
        ]
        # Only an unambiguous follow-up says which pull request was created:
        # with several candidates, joining all of them would merge unrelated
        # issue groups.
        if len(named) == 1:
            for issue_key in closes:
                uf.union(("issue", issue_key), named[0])

    execution_ids = {uuid.UUID(c[1]) for c in links if c[0] == "execution"}
    session_ids = {uuid.UUID(c[1]) for c in links if c[0] == "session"}
    slices = crud.contributor_usage(
        db,
        account_id=account_id,
        execution_ids=execution_ids,
        session_ids=session_ids,
        since=start,
        until=end,
        limit=USAGE_SLICE_LIMIT,
    )
    truncated |= len(slices) >= USAGE_SLICE_LIMIT

    items: dict[ItemKey, _Item] = defaultdict(_Item)
    agent_keys: dict[tuple[str, str], str] = {}
    principals: set[tuple[str, str]] = set()
    flow_ids: set[uuid.UUID] = set()
    user_ids: set[uuid.UUID] = set()
    for row in slices:
        if row["flow_execution_id"] is not None:
            contributor = ("execution", str(row["flow_execution_id"]))
        else:
            contributor = ("session", str(row["runtime_session_id"]))
        targets = links.get(contributor)
        if not targets:
            continue
        share = 1.0 / len(targets)
        cost = _number(row["estimated_cost"]) * share
        tokens = _number(row["total_tokens"]) * share
        requests = _number(row["request_count"]) * share
        principal = (row["principal_type"] or "", row["principal_id"] or "")
        agent = "|".join(principal) if principal[1] else ""
        if principal[0] == FLOW_RUN_PRINCIPAL:
            # A flow run's principal is the run itself; the Flows breakdown
            # already names it, so runs share one agent row.
            agent = FLOW_RUN_AGENT
        if agent and agent != FLOW_RUN_AGENT:
            principals.add(principal)
            agent_keys.setdefault(agent, row["principal_name"] or principal[1])
        if row["flow_id"] is not None:
            flow_ids.add(row["flow_id"])
        if row["user_id"] is not None:
            user_ids.add(row["user_id"])
        for target in targets:
            item = items[target]
            item.totals.add(cost, tokens, requests)
            item.partial |= bool(row["partial"])
            if contributor[0] == "execution":
                item.executions.add(contributor[1])
            else:
                item.sessions.add(contributor[1])
            item.agents[agent or "none"].add(cost, tokens, requests)
            if row["flow_id"] is not None:
                item.flows[str(row["flow_id"])].add(cost, tokens, requests)
            item.models[str(row["model"])].add(cost, tokens, requests)
            item.users[str(row["user_id"]) if row["user_id"] else "none"].add(
                cost, tokens, requests
            )

    labels = _labels(
        db,
        account_id=account_id,
        user=user,
        agent_keys=agent_keys,
        principals=principals,
        flow_ids=flow_ids,
        user_ids=user_ids,
    )

    states = _pull_request_details(
        db, account_id=account_id, keys=set(items), urls=urls, titles=titles, uf=uf
    )
    costed = set(items)
    # Issues that a costed pull request closes join its group even with no
    # spend of their own, so the group leads with the issue it delivers.
    roots = {uf.find(key) for key in costed}
    every = costed | {key for key in list(uf.parent) if uf.find(key) in roots}
    _fill_titles(db, account_id=account_id, keys=every, urls=urls, titles=titles)
    groups: dict[ItemKey, list[ItemKey]] = defaultdict(list)
    for key in every:
        groups[uf.find(key)].append(key)

    built = [
        _group(root, members, items, labels, urls, titles, states)
        for root, members in groups.items()
    ]
    built.sort(key=lambda g: (g.estimated_cost, g.total_tokens), reverse=True)
    return WorkItemCostReport(
        period_start=start,
        period_end=end,
        groups=built[offset : offset + limit],
        total_groups=len(built),
        estimated_cost=round(sum(g.estimated_cost for g in built), 6),
        total_tokens=sum(g.total_tokens for g in built),
        truncated=truncated,
    )


def _group(
    root: ItemKey,
    members: list[ItemKey],
    items: dict[ItemKey, _Item],
    labels: dict[str, dict[str, str]],
    urls: dict[ItemKey, str],
    titles: dict[ItemKey, str],
    states: dict[ItemKey, str],
) -> WorkItemGroup:
    merged = _Item()
    for key in members:
        item = items[key]
        merged.totals.add(item.totals.cost, item.totals.tokens, item.totals.requests)
        merged.partial |= item.partial
        merged.executions |= item.executions
        merged.sessions |= item.sessions
        for target, source in (
            (merged.agents, item.agents),
            (merged.flows, item.flows),
            (merged.models, item.models),
            (merged.users, item.users),
        ):
            for name, totals in source.items():
                target[name].add(totals.cost, totals.tokens, totals.requests)
    # The lead is the issue when the group has one, else its costliest item.
    lead = root
    if lead[0] != "issue":
        issues = [k for k in members if k[0] == "issue"]
        pool = issues or members
        lead = max(pool, key=lambda k: items[k].totals.cost)
    ordered = sorted(
        members, key=lambda k: (k[0] != "issue", -items[k].totals.cost, k[1])
    )
    return WorkItemGroup(
        key=lead[1],
        kind=lead[0],
        title=titles.get(lead),
        url=urls.get(lead),
        estimated_cost=round(merged.totals.cost, 6),
        partial=merged.partial,
        total_tokens=int(round(merged.totals.tokens)),
        request_count=round(merged.totals.requests, 2),
        execution_count=len(merged.executions),
        session_count=len(merged.sessions),
        items=[
            WorkItemRef(
                kind=key[0],
                key=key[1],
                title=titles.get(key),
                url=urls.get(key),
                state=states.get(key),
                estimated_cost=round(items[key].totals.cost, 6),
                total_tokens=int(round(items[key].totals.tokens)),
                request_count=round(items[key].totals.requests, 2),
            )
            for key in ordered
        ],
        agents=_slices(merged.agents, labels["agents"]),
        flows=_slices(merged.flows, labels["flows"]),
        models=_slices(merged.models, {}),
        users=_slices(merged.users, labels["users"]),
    )


def _labels(
    db: Session,
    *,
    account_id: uuid.UUID,
    user: models.User,
    agent_keys: dict[str, str],
    principals: set[tuple[str, str]],
    flow_ids: set[uuid.UUID],
    user_ids: set[uuid.UUID],
) -> dict[str, dict[str, str]]:
    """Display names; agents the caller may not view keep a generic label."""
    from preloop.plugins.account_hooks import VISIBLE_MANAGED_AGENT, filter_viewable

    agents = {"none": "No agent", FLOW_RUN_AGENT: "Flow runs"}
    loaded = fleet_crud.agents_for_principals(
        db, account_id=str(account_id), principals=principals
    )
    visible = {
        str(agent.id)
        for agent in filter_viewable(db, user, VISIBLE_MANAGED_AGENT, loaded)
    }
    index = agent_index(loaded)
    by_id = {str(agent.id): agent for agent in loaded}
    for key, fallback in agent_keys.items():
        principal_type, _, principal_id = key.partition("|")
        found = resolve_principal(index, principal_type, principal_id, by_id=by_id)
        if found is not None:
            agents[key] = found.display_name if str(found.id) in visible else "Agent"
        else:
            agents[key] = fallback
    users = {"none": "No user"}
    users.update(crud.user_names(db, account_id=account_id, user_ids=user_ids))
    return {
        "agents": agents,
        "flows": crud.flow_names(db, account_id=account_id, flow_ids=flow_ids),
        "users": users,
    }


def _fill_titles(
    db: Session,
    *,
    account_id: uuid.UUID,
    keys: set[ItemKey],
    urls: dict[ItemKey, str],
    titles: dict[ItemKey, str],
) -> None:
    missing = {k[1] for k in keys if k[0] == "issue" and k not in titles}
    for row in crud.issue_titles(db, account_id=account_id, issue_keys=missing):
        key: ItemKey = ("issue", str(row.issue_key))
        if row.title:
            titles[key] = str(row.title)
        if row.issue_url:
            urls.setdefault(key, str(row.issue_url))


def _pull_request_details(
    db: Session,
    *,
    account_id: uuid.UUID,
    keys: set[ItemKey],
    urls: dict[ItemKey, str],
    titles: dict[ItemKey, str],
    uf: _UnionFind,
) -> dict[ItemKey, str]:
    """States of known pull requests; fills titles and joins attributed issues."""
    by_url = {urls[k]: k for k in keys if k[0] == "pull_request" and k in urls}
    states: dict[ItemKey, str] = {}
    for row in crud.pull_request_details(
        db, account_id=account_id, pr_keys=set(by_url)
    ):
        key = by_url.get(str(row.pr_key))
        if key is None:
            continue
        if row.state:
            states[key] = str(row.state)
        if row.title:
            titles.setdefault(key, str(row.title))
        if row.issue_key:
            uf.union(("issue", str(row.issue_key)), key)
    return states
