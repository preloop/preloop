"""Merge bounded, persisted work evidence without guessing identities or actors."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import fleet_work as work_crud
from preloop.schemas.fleet_map import FleetWorkItem, FleetWorkRelation, WorkAction
from preloop.services.fleet_work_refs import work_key, work_url
from preloop.utils.secret_scrubbing import scrub_secrets

WORK_LIMIT = 200
RELATION_LIMIT = 500
_PROVENANCE = {
    "builtin": "tool_call",
    "cli": "cli",
    "github_mcp": "mcp",
    "gitlab_mcp": "mcp",
}
RUN_LIMIT = 5000
Kind = Literal["issue", "pull_request"]
_TOOL_ACTIONS: dict[str, WorkAction] = {
    "create_issue": "issue_opened",
    "update_issue": "issue_updated",
    "create_pull_request": "opened",
    "create_merge_request": "opened",
    "merge_pull_request": "merged",
    "merge_merge_request": "merged",
    "approve_merge_request": "reviewed",
    "create_pull_request_review": "reviewed",
    "pull_request_review_write": "reviewed",
    "submit_pending_pull_request_review": "reviewed",
    "add_comment": "commented",
    "add_comment_to_pending_review": "commented",
    "create_merge_request_note": "commented",
}
_OBJECT_PATH = re.compile(
    r"^/(?P<repo>.+?)/(?:-/)?(?P<type>issues|pull|pulls|merge_requests|pull-requests)/(?P<n>[0-9]+)/?$"
)
_KEY = re.compile(r"^(?P<repo>[\w.-]+(?:/[\w.-]+)+)(?P<mark>[#!])(?P<n>[0-9]+)$")
_JIRA = re.compile(r"^[A-Z][A-Z0-9_]*-[0-9]+$")
_ACTION_PRIORITY: dict[WorkAction, int] = {
    "issue_updated": 0,
    "pull_request_updated": 0,
    "issue_labeled": 1,
    "issue_unlabeled": 1,
    "commented": 2,
    "issue_opened": 3,
    "opened": 3,
    "reviewed": 4,
    "merged": 5,
}


@dataclass
class FleetWorkResult:
    """Work history and the PR evidence reused by the existing merge counts."""

    work: list[FleetWorkItem]
    truncated: bool
    flow_activity: dict[str, datetime]
    merged_pull_requests: list[Any]
    pull_requests_truncated: bool
    relations: list[FleetWorkRelation] | None = None
    relations_truncated: bool = False


def work_id(kind: str, key: str) -> str:
    """Graph id of a work item."""
    return f"work:{kind}:{key}"


def _aware(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _url(value: Any) -> str | None:
    """Keep HTTPS object URLs without credentials, queries or fragments."""
    if not isinstance(value, str) or len(value) > 2000:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            return None
        return urlunsplit(
            ("https", parts.netloc.rsplit("@", 1)[-1], parts.path, "", "")
        )
    except ValueError:
        return None


def _identity(
    value: Any, kind: Kind, repo: Any = None
) -> tuple[str, str, str | None, str | None] | None:
    """Resolve only explicit object references; integer summaries are lengths."""
    if not isinstance(value, str) or len(value) > 2000:
        return None
    value = value.strip()
    url = _url(value)
    if url:
        parts = urlsplit(url)
        match = _OBJECT_PATH.fullmatch(parts.path)
        if match:
            actual = "issue" if match["type"] == "issues" else "pull_request"
            if actual != kind:
                return None
            mark = "!" if match["type"] == "merge_requests" else "#"
            value = f"{match['repo']}{mark}{match['n']}"
        elif kind == "issue" and parts.path.startswith("/browse/"):
            value = parts.path.removeprefix("/browse/")
        else:
            return None
    if value.startswith(("#", "!")) and isinstance(repo, str):
        value = repo.strip("/") + value
    match = _KEY.fullmatch(value)
    if match:
        if kind == "issue" and match["mark"] == "!":
            return None
        repository = match["repo"].lower()
        reference = f"{match['mark']}{int(match['n'])}"
        return repository + reference, reference, repository, url
    if kind == "issue" and _JIRA.fullmatch(value.upper()):
        return value.upper(), value.upper(), None, url
    return None


def build_work_items(
    db: Session,
    *,
    account_id: str,
    since: datetime,
    until: datetime,
    calls: list[Any],
    flow_of_execution: dict[str, str],
    agents_by_principal: dict[tuple[str, str], models.ManagedAgent],
    see_flows: bool,
    calls_truncated: bool,
    pull_request_limit: int,
) -> FleetWorkResult:
    """Read each persisted source once and merge by kind and canonical key.

    Titles come from stored tracker rows or compact run subjects; labels only
    from tracker snapshots. Current PR state and
    latest review verdict remain unknown unless there is explicit forge evidence.
    Actor links must fall inside the same window, with visibility applied before
    their IDs are returned. Agent owners are not inferred to have acted.
    """
    items: dict[tuple[Kind, str], FleetWorkItem] = {}
    actors: dict[tuple[Kind, str], dict[str, datetime]] = {}
    relations: dict[str, FleetWorkRelation] = {}

    def relate(
        kind: str,
        source: str,
        target: tuple[Kind, str],
        at: datetime,
        provenance: str,
        action: WorkAction | None = None,
    ) -> None:
        """Record one relation; both work ends are checked after the merge."""
        target_id = work_id(*target)
        identifier = f"{kind}:{source}:{target_id}"
        at = _aware(at)
        relation = relations.get(identifier)
        if relation is None:
            relation = relations[identifier] = FleetWorkRelation(
                id=identifier,
                kind=kind,  # type: ignore[arg-type]
                source=source,
                target=target_id,
                at=at,
                provenance=provenance,  # type: ignore[arg-type]
            )
        relation.at = max(relation.at, at)
        if kind == "touched":
            relation.count += 1
            if action and action not in relation.actions:
                relation.actions.append(action)

    def slot_of(value: Any, kind: Kind, repo: Any = None) -> tuple[Kind, str] | None:
        identity = _identity(value, kind, repo)
        return (kind, identity[0]) if identity else None

    def add(
        value: Any,
        kind: Kind,
        at: datetime,
        action: WorkAction,
        *,
        repo: Any = None,
        title: Any = None,
        url: Any = None,
        state: Any = None,
        labels: Any = None,
        related: Any = None,
        node_id: str | None = None,
        snapshot: bool = False,
    ) -> None:
        identity = _identity(value, kind, repo)
        if identity is None:
            return
        key, reference, repository, target_url = identity
        slot = (kind, key)
        at = _aware(at)
        item = items.get(slot)
        if item is None:
            item = FleetWorkItem(
                key=key,
                kind=kind,
                reference=reference,
                repo=repository,
                url=target_url,
                last_at=at,
                last_action=action,
            )
            items[slot] = item
        if at > item.last_at or (
            at == item.last_at
            and _ACTION_PRIORITY[action] > _ACTION_PRIORITY[item.last_action]
        ):
            item.last_at, item.last_action = at, action
        if (
            (snapshot or item.title is None)
            and isinstance(title, str)
            and title.strip()
        ):
            item.title = (scrub_secrets(title) or "")[:160]
        if item.url is None:
            item.url = target_url or _url(url)
        if item.state is None and state in {"open", "closed", "merged"}:
            item.state = state
        if not item.labels and isinstance(labels, list):
            item.labels = [
                (scrub_secrets(label) or "")[:40]
                for label in labels[:6]
                if isinstance(label, str) and label.strip()
            ][:6]
        if related and (issue := _identity(related, "issue")):
            item.related_issue = issue[0]
        if node_id:
            by_node = actors.setdefault(slot, {})
            by_node[node_id] = max(by_node.get(node_id, at), at)

    links = (
        work_crud.list_run_links(
            db, account_id=account_id, since=since, until=until, limit=RUN_LIMIT + 1
        )
        if see_flows
        else []
    )
    subjects = (
        work_crud.list_execution_subjects(
            db, account_id=account_id, since=since, until=until, limit=RUN_LIMIT + 1
        )
        if see_flows
        else []
    )
    truncated = calls_truncated or len(links) > RUN_LIMIT or len(subjects) > RUN_LIMIT
    for row in links[:RUN_LIMIT]:
        node_id: str | None = f"flow:{row.flow_id}"
        issue_slot = slot_of(row.issue_key or row.url, "issue")
        pr_slot = slot_of(row.pr_key, "pull_request")
        if issue_slot and pr_slot:
            relate(
                "resolves" if row.link == "closing_reference" else "mentions",
                work_id(*pr_slot),
                issue_slot,
                row.at,
                "tracker" if row.link == "closing_reference" else "flow_run",
            )
        add(
            row.issue_key or row.url,
            "issue",
            row.at,
            "issue_updated",
            title=row.title,
            url=row.url,
            node_id=node_id,
        )
        add(
            row.pr_key,
            "pull_request",
            row.at,
            "pull_request_updated",
            related=row.issue_key,
            node_id=node_id,
        )
    for row in subjects[:RUN_LIMIT]:
        node_id = f"flow:{row.flow_id}" if row.assigned else None
        # A trigger identifies a run's subject, not the actor that performed
        # the webhook action. The run itself only proves work on that subject.
        subject_kind: Kind | None = None
        if isinstance(row.event, str):
            if row.event.startswith("issue_"):
                subject_kind = "issue"
            elif row.event.startswith(("pull_request_", "merge_request_")):
                subject_kind = "pull_request"
        if subject_kind:
            # The trigger subject is what started the run, assigned or not.
            subject = slot_of(row.url or row.reference, subject_kind, row.repo)
            if subject:
                relate("triggered", f"flow:{row.flow_id}", subject, row.at, "flow_run")
        produced = slot_of(row.pr_url, "pull_request")
        if produced and node_id:
            relate("produced", node_id, produced, row.at, "flow_run")
        if isinstance(row.event, str):
            if row.event.startswith("issue_"):
                add(
                    row.url or row.reference,
                    "issue",
                    row.at,
                    "issue_updated",
                    repo=row.repo,
                    title=row.title,
                    node_id=node_id,
                )
            elif row.event.startswith(("pull_request_", "merge_request_")):
                add(
                    row.url or row.reference,
                    "pull_request",
                    row.at,
                    "pull_request_updated",
                    repo=row.repo,
                    title=row.title,
                    node_id=node_id,
                )
        add(row.pr_url, "pull_request", row.at, "pull_request_updated", node_id=node_id)
    # A create call has no number yet. Its parsed closing/plain references
    # attach to the first pull request the same caller names afterwards in the
    # same repository (typically its own follow-up on the PR it opened).
    unnumbered: list[tuple[tuple[str, str, str], str, datetime, dict[str, Any]]] = []
    named: list[tuple[tuple[str, str, str], str, datetime, str]] = []
    for call in calls:
        metadata = call.metadata_ if isinstance(call.metadata_, dict) else {}
        work_ref = metadata.get("work_ref")
        caller = (
            call.runtime_principal_type or "",
            call.runtime_principal_id or "",
            str(call.flow_execution_id or ""),
        )
        if (
            isinstance(work_ref, dict)
            and work_key(work_ref) is None
            and work_ref.get("kind") == "pull_request"
            and work_ref.get("action") == "opened"
            and isinstance(work_ref.get("repo"), str)
            and (work_ref.get("closes") or work_ref.get("mentions"))
        ):
            unnumbered.append(
                (caller, work_ref["repo"], _aware(call.timestamp), work_ref)
            )
            continue
        if isinstance(work_ref, dict) and (named_key := work_key(work_ref)):
            if named_key[0] == "pull_request" and isinstance(work_ref.get("repo"), str):
                named.append(
                    (caller, work_ref["repo"], _aware(call.timestamp), named_key[1])
                )
        # A classified call is authoritative once its object is known; an
        # unresolved one (e.g. a tracker key) falls through to the summary.
        if isinstance(work_ref, dict) and work_key(work_ref) is not None:
            flow_id = flow_of_execution.get(str(call.flow_execution_id or ""))
            actor: str | None = f"flow:{flow_id}" if flow_id else None
            if not call.flow_execution_id:
                agent = agents_by_principal.get(
                    (call.runtime_principal_type or "", call.runtime_principal_id or "")
                )
                if agent:
                    actor = f"agent:{agent.id}"
            found = work_key(work_ref)
            ref_action = work_ref.get("action")
            if found and ref_action in _ACTION_PRIORITY:
                ref_kind, key = found
                add(
                    key,
                    ref_kind,
                    call.timestamp,
                    ref_action,
                    url=work_url(work_ref),
                    node_id=actor,
                )
                slot = slot_of(key, ref_kind)
                if slot and actor:
                    relate(
                        "touched",
                        actor,
                        slot,
                        call.timestamp,
                        _PROVENANCE.get(str(work_ref.get("via")), "tool_call"),
                        ref_action,
                    )
                if slot and ref_kind == "pull_request":
                    for relation_kind, name in (
                        ("resolves", "closes"),
                        ("mentions", "mentions"),
                    ):
                        for issue_key in (work_ref.get(name) or [])[:5]:
                            issue = slot_of(issue_key, "issue")
                            if issue:
                                relate(
                                    relation_kind,
                                    work_id(*slot),
                                    issue,
                                    call.timestamp,
                                    _PROVENANCE.get(
                                        str(work_ref.get("via")), "tool_call"
                                    ),
                                )
            continue
        summary = metadata.get("arguments_summary")
        if not isinstance(summary, dict):
            continue
        kind: Kind = (
            "issue"
            if call.tool_name in {"create_issue", "update_issue"}
            else "pull_request"
        )
        action = _TOOL_ACTIONS.get(call.tool_name, "pull_request_updated")
        if call.tool_name == "update_pull_request" and "review_action" in summary:
            action = "reviewed"
        flow_id = flow_of_execution.get(str(call.flow_execution_id or ""))
        node_id = f"flow:{flow_id}" if flow_id else None
        if not call.flow_execution_id:
            agent = agents_by_principal.get(
                (call.runtime_principal_type or "", call.runtime_principal_id or "")
            )
            if agent:
                node_id = f"agent:{agent.id}"
        target = next(
            (
                value
                for name in (
                    "url",
                    "issue_url",
                    "pr_url",
                    "target",
                    "issue",
                    "pull_request",
                    "reference",
                    "key",
                    "issue_id",
                )
                if _identity(value := summary.get(name), kind, summary.get("repo"))
            ),
            None,
        )
        if target is not None:
            if (
                call.tool_name == "add_comment"
                and metadata.get("code_target") != "pull_request"
            ):
                # A short GitHub #N is shared by issues and PRs. Only a PR URL
                # or GitLab !N is sufficient without the wrapper's target flag.
                identity = _identity(target, kind, summary.get("repo"))
                if identity is None or (
                    identity[3] is None and not identity[1].startswith("!")
                ):
                    continue
            add(
                target,
                kind,
                call.timestamp,
                action,
                repo=summary.get("repo"),
                node_id=node_id,
            )

    for caller, repo, at, work_ref in unnumbered:
        later = sorted(
            (when, key)
            for who, where, when, key in named
            if who == caller and where == repo and when >= at
        )
        if not later:
            continue
        pr_slot = slot_of(later[0][1], "pull_request")
        if pr_slot is None:
            continue
        for relation_kind, name in (("resolves", "closes"), ("mentions", "mentions")):
            for issue_key in (work_ref.get(name) or [])[:5]:
                if issue := slot_of(issue_key, "issue"):
                    # The referenced issue joins the work list: the PR names it.
                    add(issue_key, "issue", at, "issue_updated")
                    relate(
                        relation_kind,
                        work_id(*pr_slot),
                        issue,
                        at,
                        _PROVENANCE.get(str(work_ref.get("via")), "tool_call"),
                    )

    # Reuse the bounded references as an in-query join, so even an old tracker
    # snapshot enriches a recent tool/run target without a lookup per object.
    def targets(kind: Kind) -> work_crud.Targets:
        return {
            item.key: (item.last_at, item.url)
            for item in items.values()
            if item.kind == kind
        }

    issues = work_crud.list_issues(
        db,
        account_id=account_id,
        since=since,
        until=until,
        limit=WORK_LIMIT + 1,
        targets=targets("issue"),
    )
    for row in issues:
        add(
            row.key or row.url,
            "issue",
            row.at,
            "issue_opened" if row.created_at == row.at else "issue_updated",
            title=row.title,
            url=row.url,
            state=row.state,
            labels=row.labels,
            snapshot=True,
        )
    pull_requests = work_crud.list_pull_requests(
        db,
        account_id=account_id,
        since=since,
        until=until,
        limit=pull_request_limit + 1,
        targets=targets("pull_request"),
    )
    active_prs = [row for row in pull_requests if row.active]
    merged_prs = sorted(
        (row for row in pull_requests if row.for_merges),
        key=lambda row: (-_aware(row.merged_at).timestamp(), row.pr_key),
    )
    pull_requests_truncated = (
        len(active_prs) > pull_request_limit or len(merged_prs) > pull_request_limit
    )
    for row in active_prs[:pull_request_limit]:
        action = (
            "opened"
            if row.opened_at and _aware(row.opened_at) == row.at
            else "pull_request_updated"
        )
        if row.merged_at and _aware(row.merged_at) == row.at:
            action = "merged"
        elif row.approved_at and _aware(row.approved_at) == row.at:
            action = "reviewed"
        add(
            row.pr_key,
            "pull_request",
            row.at,
            action,
            state="merged"
            if row.merged_at and _aware(row.merged_at) <= until
            else None,
            related=row.issue_key if not row.ambiguous else None,
        )
        pr_slot = slot_of(row.pr_key, "pull_request")
        issue_slot = slot_of(row.issue_key, "issue") if not row.ambiguous else None
        if pr_slot and issue_slot:
            # The issue-cost rollup pairs a PR with the issue it closes.
            relate("resolves", work_id(*pr_slot), issue_slot, row.at, "tracker")
    truncated |= pull_requests_truncated or len(issues) > WORK_LIMIT
    for slot, item in items.items():
        item.id = work_id(slot[0], slot[1])
        item.href = item.url
        item.node_ids = sorted(
            actors.get(slot, {}),
            key=lambda node: (-actors[slot][node].timestamp(), node),
        )[:5]
    ordered = sorted(
        items.values(),
        key=lambda item: (-item.last_at.timestamp(), item.kind, item.key),
    )
    visible = ordered[:WORK_LIMIT]
    active_flows: dict[str, datetime] = {}
    for item in visible:
        for node in item.node_ids:
            if node.startswith("flow:"):
                flow_id = node.removeprefix("flow:")
                at = actors[(item.kind, item.key)][node]
                active_flows[flow_id] = max(active_flows.get(flow_id, at), at)
    # A relation needs both work ends among the returned items; a node end
    # (agent/flow) is checked by the caller's visibility.
    shown = {item.id for item in visible}
    kept = sorted(
        (
            relation
            for relation in relations.values()
            if relation.target in shown
            and (not relation.source.startswith("work:") or relation.source in shown)
        ),
        key=lambda relation: (-relation.at.timestamp(), relation.id),
    )
    return FleetWorkResult(
        relations=kept[:RELATION_LIMIT],
        relations_truncated=len(kept) > RELATION_LIMIT,
        work=visible,
        truncated=truncated or len(ordered) > WORK_LIMIT,
        flow_activity=active_flows,
        merged_pull_requests=merged_prs[:pull_request_limit],
        pull_requests_truncated=pull_requests_truncated,
    )
