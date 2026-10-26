"""Account-scoped, bounded work-item sources for Fleet Map history."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Row,
    String,
    case,
    cast,
    column,
    func,
    or_,
    null,
    select,
    union,
    union_all,
    values,
)
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from preloop.models import models

ASSIGNED_LINKS = (
    "lifecycle",
    "trigger_issue",
    "resume",
    "delegated",
    "pull_request",
    "closing_reference",
)
Targets = dict[str, tuple[datetime, str | None]]


def _targets(targets: Targets) -> Any:
    """Bounded references already identified by the other source windows."""
    return (
        values(
            column("key", String),
            column("at", DateTime(timezone=True)),
            column("url", String),
            name="work_targets",
        )
        .data([(key.lower(), at, url) for key, (at, url) in targets.items()])
        .alias("work_targets")
    )


def _window_time(*columns: Any, since: datetime, until: datetime) -> ColumnElement[Any]:
    """Latest observed timestamp inside the closed window, ignoring future stamps."""
    return func.greatest(
        *(case((column.between(since, until), column)) for column in columns)
    )


def list_pull_requests(
    db: Session,
    *,
    account_id: str,
    since: datetime,
    until: datetime,
    limit: int,
    targets: Targets | None = None,
) -> list[Row[Any]]:
    """Read independently capped active and merged PR windows in one query.

    At most twice ``limit`` distinct PRs are returned, with membership flags.
    Active metadata must never crowd a merge out of the existing merge counts.
    No actor is implied by a milestone.
    """
    p, r, x = (
        models.IssueCostPullRequest,
        models.IssueCostRollup,
        models.IssueCostExecution,
    )
    e, f = models.FlowExecution, models.Flow
    at = _window_time(p.opened_at, p.approved_at, p.merged_at, since=since, until=until)
    target_rows = _targets(targets) if targets else None
    if target_rows is not None:
        at = func.greatest(at, target_rows.c.at)
    active_statement = select(p.id, at.label("at")).select_from(p)
    if target_rows is not None:
        canonical_key = func.regexp_replace(
            func.lower(p.pr_key),
            r"^https://[^/]+/(.+?)/(?:-/)?(pull|pulls|merge_requests|pull-requests)/([0-9]+)/?$",
            case((p.pr_key.contains("/merge_requests/"), r"\1!\3"), else_=r"\1#\3"),
        )
        active_statement = active_statement.outerjoin(
            target_rows,
            or_(p.pr_key == target_rows.c.url, canonical_key == target_rows.c.key),
        )
    active = (
        active_statement.where(p.account_id == account_id, at.is_not(None))
        .order_by(at.desc(), p.pr_key)
        .limit(limit)
        .cte("active_prs")
    )
    merged = (
        select(p.id)
        .where(p.account_id == account_id, p.merged_at.between(since, until))
        .order_by(p.merged_at.desc(), p.pr_key)
        .limit(limit)
        .cte("merged_prs")
    )
    keys = union(select(active.c.id), select(merged.c.id)).subquery()
    # Historical merge counts credit contributing flows, while work.node_ids
    # below only credits links active in this window. Bound fan-out as well as PRs.
    contributors = (
        select(x.flow_id)
        .join(e, (e.id == x.execution_id) & (e.flow_id == x.flow_id))
        .join(f, (f.id == e.flow_id) & (f.account_id == account_id))
        .where(
            x.account_id == account_id,
            x.pr_key == p.pr_key,
            x.link.in_(ASSIGNED_LINKS),
            p.ambiguous.is_(False),
            p.merged_at.between(since, until),
        )
        .distinct()
        .order_by(x.flow_id)
        .limit(6)
        .correlate(p)
        .subquery()
    )
    merge_flows = select(func.json_agg(contributors.c.flow_id)).scalar_subquery()
    statement = (
        select(
            p.pr_key,
            p.opened_at,
            p.approved_at,
            p.merged_at,
            p.state,
            p.ambiguous,
            r.issue_key,
            active.c.at,
            active.c.id.is_not(None).label("active"),
            merged.c.id.is_not(None).label("for_merges"),
            merge_flows.label("merge_flows"),
        )
        .select_from(p)
        .join(keys, keys.c.id == p.id)
    )
    statement = statement.outerjoin(active, active.c.id == p.id).outerjoin(
        merged, merged.c.id == p.id
    )
    return list(
        db.execute(
            statement.outerjoin(r, (r.id == p.rollup_id) & (r.account_id == account_id))
            .where(p.account_id == account_id)
            .order_by(active.c.at.desc().nullslast(), p.pr_key)
        ).all()
    )


def list_issues(
    db: Session,
    *,
    account_id: str,
    since: datetime,
    until: datetime,
    limit: int,
    targets: Targets | None = None,
) -> list[Row[Any]]:
    """Read current issue metadata and base/lifecycle recency without relationships.

    Base timestamps are UTC without a timezone. Lifecycle state is workflow state,
    not forge issue state. Neither a local insert nor a lifecycle revision identifies
    an issue's creator.
    """
    i, t, l = models.Issue, models.Tracker, models.IssueLifecycle
    revision_at = _window_time(
        func.timezone("UTC", l.created_at),
        func.timezone("UTC", l.updated_at),
        since=since,
        until=until,
    )
    revisions = (
        select(l.issue_id, func.max(revision_at).label("at"))
        .where(l.account_id == account_id, revision_at.is_not(None))
        .group_by(l.issue_id)
        .subquery()
    )
    # ``updated_at`` is local bookkeeping (every sync or embedding write bumps
    # it), not activity on the issue, so it never puts an issue in a window.
    at = _window_time(
        func.timezone("UTC", i.created_at),
        func.timezone("UTC", i.last_updated_external),
        revisions.c.at,
        since=since,
        until=until,
    )
    target_rows = _targets(targets) if targets else None
    if target_rows is not None:
        at = func.greatest(at, target_rows.c.at)
    statement = (
        select(
            i.key,
            i.external_url.label("url"),
            func.left(i.title, 160).label("title"),
            i.status.label("state"),
            i.meta_data["labels"].label("labels"),
            func.timezone("UTC", i.created_at).label("created_at"),
            at.label("at"),
        )
        .select_from(i)
        .join(t, t.id == i.tracker_id)
        .outerjoin(revisions, revisions.c.issue_id == i.id)
    )
    if target_rows is not None:
        statement = statement.outerjoin(
            target_rows,
            or_(
                func.lower(i.key) == target_rows.c.key,
                i.external_url == target_rows.c.url,
            ),
        )
    return list(
        db.execute(
            statement.where(t.account_id == account_id, at.is_not(None))
            .order_by(at.desc(), i.id)
            .limit(limit)
        ).all()
    )


def list_run_links(
    db: Session, *, account_id: str, since: datetime, until: datetime, limit: int
) -> list[Row[Any]]:
    """Read explicit cost and lifecycle run links in one bounded query.

    Unassigned and ambiguous facts never create actors or issue associations.
    Joining the real run and owning flow guards even inconsistent foreign keys.
    Lifecycle rows link issues directly to executions; their assessment data is
    deliberately excluded.
    """
    x, r, p = (
        models.IssueCostExecution,
        models.IssueCostRollup,
        models.IssueCostPullRequest,
    )
    e, f = models.FlowExecution, models.Flow
    l, i, t = models.IssueLifecycle, models.Issue, models.Tracker
    at = _window_time(x.start_time, x.end_time, since=since, until=until)
    costs = (
        select(
            x.pr_key.label("pr_key"),
            r.issue_key.label("issue_key"),
            func.left(r.title, 160).label("title"),
            r.issue_url.label("url"),
            x.flow_id.label("flow_id"),
            at.label("at"),
            x.link.label("link"),
        )
        .select_from(x)
        .join(e, (e.id == x.execution_id) & (e.flow_id == x.flow_id))
        .join(f, (f.id == e.flow_id) & (f.account_id == account_id))
        .outerjoin(r, (r.id == x.rollup_id) & (r.account_id == account_id))
        .outerjoin(p, (p.pr_key == x.pr_key) & (p.account_id == account_id))
        .where(
            x.account_id == account_id,
            x.link.in_(ASSIGNED_LINKS),
            or_(p.id.is_(None), p.ambiguous.is_(False)),
            at.is_not(None),
        )
    )
    lifecycle_at = _window_time(
        func.timezone("UTC", l.created_at),
        func.timezone("UTC", l.updated_at),
        since=since,
        until=until,
    )
    lifecycle = (
        select(
            cast(null(), p.pr_key.type).label("pr_key"),
            i.key.label("issue_key"),
            func.left(i.title, 160).label("title"),
            i.external_url.label("url"),
            e.flow_id.label("flow_id"),
            lifecycle_at.label("at"),
            cast(null(), x.link.type).label("link"),
        )
        .select_from(l)
        .join(i, i.id == l.issue_id)
        .join(t, (t.id == i.tracker_id) & (t.account_id == account_id))
        .join(e, e.id == l.execution_id)
        .join(f, (f.id == e.flow_id) & (f.account_id == account_id))
        .where(l.account_id == account_id, lifecycle_at.is_not(None))
    )
    links = union_all(costs, lifecycle).subquery()
    return list(
        db.execute(
            select(links)
            .order_by(
                links.c.at.desc(), links.c.flow_id, links.c.pr_key, links.c.issue_key
            )
            .limit(limit)
        ).all()
    )


def list_execution_subjects(
    db: Session, *, account_id: str, since: datetime, until: datetime, limit: int
) -> list[Row[Any]]:
    """Project compact stored trigger subjects and publication URLs in one read.

    This includes running executions before cost facts exist. No raw webhook,
    output, prompt, feedback, or result body is selected. A conflicting cost fact
    suppresses actor attribution, even when a subject or publication is present.
    """
    e, f, x, p = (
        models.FlowExecution,
        models.Flow,
        models.IssueCostExecution,
        models.IssueCostPullRequest,
    )
    subject = e.trigger_event_details["_subject"]
    at = _window_time(
        func.timezone("UTC", e.start_time),
        func.timezone("UTC", e.end_time),
        since=since,
        until=until,
    )
    assigned = or_(
        x.id.is_(None),
        x.link.in_(ASSIGNED_LINKS) & or_(p.id.is_(None), p.ambiguous.is_(False)),
    )
    return list(
        db.execute(
            select(
                e.flow_id,
                at.label("at"),
                assigned.label("assigned"),
                e.trigger_event_details["type"].as_string().label("event"),
                subject["reference"].as_string().label("reference"),
                subject["repo"].as_string().label("repo"),
                subject["url"].as_string().label("url"),
                func.left(subject["title"].as_string(), 160).label("title"),
                e.result["pr_url"].as_string().label("pr_url"),
            )
            .select_from(e)
            .join(f, (f.id == e.flow_id) & (f.account_id == account_id))
            .outerjoin(x, (x.execution_id == e.id) & (x.account_id == account_id))
            .outerjoin(p, (p.pr_key == x.pr_key) & (p.account_id == account_id))
            .where(
                at.is_not(None),
                or_(subject.is_not(None), e.result["pr_url"].as_string().is_not(None)),
            )
            .order_by(at.desc(), e.id)
            .limit(limit)
        ).all()
    )


def flows_for_pull_request(db: Session, *, account_id: Any, pr_key: str) -> list[str]:
    """Read at most five explicitly linked flows for a forge merge broadcast."""
    x, p, e, f = (
        models.IssueCostExecution,
        models.IssueCostPullRequest,
        models.FlowExecution,
        models.Flow,
    )
    return [
        str(flow_id)
        for flow_id in db.scalars(
            select(x.flow_id)
            .distinct()
            .join(e, (e.id == x.execution_id) & (e.flow_id == x.flow_id))
            .join(f, (f.id == e.flow_id) & (f.account_id == account_id))
            .outerjoin(p, (p.pr_key == x.pr_key) & (p.account_id == account_id))
            .where(
                x.account_id == account_id,
                x.pr_key == pr_key,
                x.link.in_(ASSIGNED_LINKS),
                or_(p.id.is_(None), p.ambiguous.is_(False)),
            )
            .order_by(x.flow_id)
            .limit(5)
        )
    ]
