"""File the approved portfolio follow ups as tracker issues (issue #687).

A portfolio review reads many untrusted projects, so the flow that produces
the ranked follow ups holds no write tool: ``allowed_mcp_servers`` is empty
and the only tool on its allowlist is the built in question channel. The
deliverable, though, is "an inventory, an assessment and ranked follow up
issues filed in the customer's tracker", and an approval nobody acts on is
not a filed issue.

Filing therefore happens where the report publication of issue #648 happens:
on the platform side, after the agent process has exited, from the control
plane that already holds the account's tracker credential. The agent's tool
surface is unchanged, which is the whole argument:

- the rows that get filed are the ones a human approved at the gate, read
  out of the result envelope the run wrote;
- an expired gate or an empty approval files nothing and says so;
- a follow up that already has an issue from an earlier run is not filed
  again: the stable follow up id is the key, and the ledger is the filings
  earlier executions of the same flow recorded;
- one tracker error fails one row. The remaining rows are still filed and
  the failed row is reported as not filed with a reason from a closed
  vocabulary, never a provider error string;
- the filed issue identifier is written back onto its follow up row, so the
  report and the next run both know the follow up has a home.

The module is deliberately free of database and HTTP imports: it takes a
tracker client and a result envelope, and the orchestrator supplies both.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Result key the control plane owns. An agent result.json cannot author it
# (see preloop.services.flow_artifacts.RESERVED_RESULT_FIELDS): a flow with no
# write tools cannot claim issues it had no way to create.
FOLLOW_UP_FILING_RESULT_KEY = "follow_up_filing"

# Only a portfolio envelope carries the rows this module knows how to read.
PORTFOLIO_SCHEMA_PREFIX = "preloop.review.portfolio/"

# Printed in every filed issue body. It is what a human (and a later
# duplicate check) uses to tie an issue back to the follow up it came from.
FOLLOW_UP_ID_HEADING = "Follow up id"

DEFAULT_LABELS: tuple[str, ...] = ("preloop", "portfolio-review", "follow-up")
DEFAULT_MAX_ISSUES = 25

# How far back the idempotency ledger looks over a flow's own executions.
LEDGER_EXECUTION_LIMIT = 50

MAX_TITLE_LENGTH = 200
MAX_NOTE_LENGTH = 2000
MAX_FIELD_LENGTH = 500

OUTCOME_FILED = "filed"
OUTCOME_PARTIAL = "partial"
OUTCOME_NOTHING_FILED = "nothing_filed"
OUTCOME_FAILED = "failed"
FOLLOW_UP_FILING_OUTCOMES = frozenset(
    {OUTCOME_FILED, OUTCOME_PARTIAL, OUTCOME_NOTHING_FILED, OUTCOME_FAILED}
)

ROW_FILED = "filed"
ROW_ALREADY_FILED = "already_filed"
ROW_NOT_FILED = "not_filed"
ROW_FAILED = "failed"
FOLLOW_UP_ROW_OUTCOMES = frozenset(
    {ROW_FILED, ROW_ALREADY_FILED, ROW_NOT_FILED, ROW_FAILED}
)

# Closed vocabulary. A reason is a diagnosis an operator can act on, never a
# tracker error string, so nothing unbounded (or secret bearing) reaches the
# stored result. The detail of a failure goes to the execution log.
FOLLOW_UP_FILING_REASONS = frozenset(
    {
        "",  # filed: nothing to explain
        "already_filed",
        "credentials_unavailable",
        "duplicate_follow_up",
        "filing_disabled",
        "gate_expired",
        "invalid_row",
        "limit_reached",
        "no_follow_ups",
        "not_a_portfolio_result",
        "nothing_approved",
        "project_ambiguous",
        "project_missing",
        "tracker_error",
        "tracker_unavailable",
    }
)

SEVERITIES = ("high", "medium", "low")


class FollowUpFilingError(ValueError):
    """A configuration or target this module refuses to file against.

    Carries a ``reason`` from the closed vocabulary so the caller can degrade
    the run with a diagnosis instead of an exception message.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class FollowUpFilingPlan:
    """What the flow configured: whether to file, where, and how much."""

    enabled: bool
    labels: tuple[str, ...] = DEFAULT_LABELS
    max_issues: int = DEFAULT_MAX_ISSUES
    project_id: Optional[str] = None


@dataclass(frozen=True)
class FollowUpRow:
    """One approved follow up, as the result envelope recorded it."""

    index: int
    id: str
    project: str
    title: str
    severity: str
    evidence: Optional[str]
    rank: Optional[int]
    note: Optional[str]
    approved_by: Optional[str]
    approved_at: Optional[str]
    child_execution_id: Optional[str]


@dataclass(frozen=True)
class FilingContext:
    """Where the follow ups came from, quoted into every issue body."""

    execution_id: Optional[str] = None
    flow_name: Optional[str] = None
    repository: Optional[str] = None
    commit: Optional[str] = None
    report_path: Optional[str] = None


@dataclass(frozen=True)
class IssueRequest:
    """The tracker-independent shape of one issue this module files."""

    title: str
    description: str
    labels: tuple[str, ...]
    priority: Optional[str]


@dataclass(frozen=True)
class RowFiling:
    """What happened to one follow up row."""

    index: int
    follow_up_id: str
    outcome: str
    reason: str = ""
    issue: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class FilingOutcome:
    """The receipt for one run's filing pass."""

    rows: tuple[RowFiling, ...] = ()
    reason: str = ""
    tracker: Optional[str] = None
    project: Optional[str] = None
    considered: int = 0
    _blocked: bool = field(default=False, repr=False)

    @property
    def filed(self) -> int:
        return sum(1 for row in self.rows if row.outcome == ROW_FILED)

    @property
    def already_filed(self) -> int:
        return sum(1 for row in self.rows if row.outcome == ROW_ALREADY_FILED)

    @property
    def failed(self) -> int:
        return sum(1 for row in self.rows if row.outcome == ROW_FAILED)

    @property
    def not_filed(self) -> int:
        return sum(1 for row in self.rows if row.outcome == ROW_NOT_FILED)

    @property
    def outcome(self) -> str:
        if self.filed and self.failed:
            return OUTCOME_PARTIAL
        if self.filed:
            return OUTCOME_FILED
        if self.failed:
            return OUTCOME_FAILED
        return OUTCOME_NOTHING_FILED

    def receipt(self) -> dict[str, Any]:
        """The record written to ``result['follow_up_filing']``."""
        reason = self.reason if self.reason in FOLLOW_UP_FILING_REASONS else ""
        if not reason and self.outcome == OUTCOME_FAILED:
            reason = "tracker_error"
        return {
            "outcome": self.outcome,
            "reason": reason,
            "considered": self.considered,
            "filed": self.filed,
            "already_filed": self.already_filed,
            "failed": self.failed,
            "not_filed": self.not_filed,
            "tracker": self.tracker,
            "project": self.project,
            "rows": [
                {
                    "id": row.follow_up_id,
                    "outcome": row.outcome,
                    "reason": row.reason,
                    "issue": dict(row.issue) if row.issue else None,
                }
                for row in self.rows
            ],
        }


def blocked_outcome(reason: str, **kwargs: Any) -> FilingOutcome:
    """A receipt for a pass that filed nothing, with the reason it did not."""
    if reason not in FOLLOW_UP_FILING_REASONS:
        raise ValueError(f"unknown follow up filing reason: {reason!r}")
    return FilingOutcome(reason=reason, _blocked=True, **kwargs)


def _clean(value: Any, *, limit: int = MAX_FIELD_LENGTH) -> Optional[str]:
    """A trimmed single value, or None when there is nothing usable."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


def resolve_follow_up_filing(
    git_clone_config: Optional[Mapping[str, Any]],
) -> Optional[FollowUpFilingPlan]:
    """Read the ``follow_up_filing`` block, or None when the flow has none.

    A disabled block is None too: nothing downstream should have to
    distinguish "never configured" from "turned off".
    """
    if not isinstance(git_clone_config, Mapping):
        return None
    block = git_clone_config.get("follow_up_filing")
    if not isinstance(block, Mapping) or not block.get("enabled"):
        return None

    raw_labels = block.get("labels")
    labels: tuple[str, ...]
    if isinstance(raw_labels, Sequence) and not isinstance(raw_labels, (str, bytes)):
        cleaned = [_clean(label, limit=50) for label in raw_labels]
        labels = tuple(dict.fromkeys(label for label in cleaned if label))
    else:
        labels = DEFAULT_LABELS

    max_issues = block.get("max_issues")
    if not isinstance(max_issues, int) or isinstance(max_issues, bool):
        max_issues = DEFAULT_MAX_ISSUES
    max_issues = max(1, min(int(max_issues), 100))

    return FollowUpFilingPlan(
        enabled=True,
        labels=labels,
        max_issues=max_issues,
        project_id=_clean(block.get("project_id"), limit=64),
    )


def _question(result: Mapping[str, Any], phase: str) -> Mapping[str, Any]:
    questions = result.get("questions")
    if not isinstance(questions, Sequence):
        return {}
    for entry in questions:
        if isinstance(entry, Mapping) and entry.get("phase") == phase:
            return entry
    return {}


def gate_status(result: Mapping[str, Any]) -> Optional[str]:
    """The status the run recorded for the follow up gate, if it recorded one."""
    status = _question(result, "follow_ups").get("status")
    return status if isinstance(status, str) and status else None


def approved_follow_ups(result: Mapping[str, Any]) -> list[FollowUpRow]:
    """The approved rows, in the order they are to be filed (rank, then order).

    A row missing an id, a project or a title is not filed: there is nothing
    honest to put in an issue, and inventing it would be worse than skipping.
    """
    rows: list[FollowUpRow] = []
    follow_ups = result.get("follow_ups")
    if not isinstance(follow_ups, Sequence):
        return rows
    for index, raw in enumerate(follow_ups):
        if not isinstance(raw, Mapping) or raw.get("status") != "approved":
            continue
        identifier = _clean(raw.get("id"), limit=200)
        project = _clean(raw.get("project"))
        title = _clean(raw.get("title"), limit=MAX_TITLE_LENGTH)
        if not identifier or not project or not title:
            logger.warning("Skipping a follow up row with no id, project or title")
            continue
        severity = raw.get("severity")
        rank = raw.get("rank")
        rows.append(
            FollowUpRow(
                index=index,
                id=identifier,
                project=project,
                title=title,
                severity=severity if severity in SEVERITIES else "low",
                evidence=_clean(raw.get("evidence")),
                rank=rank
                if isinstance(rank, int) and not isinstance(rank, bool)
                else None,
                note=_clean(raw.get("note"), limit=MAX_NOTE_LENGTH),
                approved_by=_clean(raw.get("approved_by")),
                approved_at=_clean(raw.get("approved_at")),
                child_execution_id=_clean(raw.get("child_execution_id"), limit=64),
            )
        )
    rows.sort(key=lambda row: (row.rank is None, row.rank or 0, row.index))
    return rows


def filing_blocked_reason(result: Any, rows: Sequence[FollowUpRow]) -> Optional[str]:
    """Why this result files nothing, or None when there are rows to file.

    "Nothing to file" is a reported outcome, not silence: an expired gate and
    an empty approval are different states and the report says which one.
    """
    if not isinstance(result, Mapping):
        return "not_a_portfolio_result"
    schema = result.get("schema")
    if not isinstance(schema, str) or not schema.startswith(PORTFOLIO_SCHEMA_PREFIX):
        return "not_a_portfolio_result"
    follow_ups = result.get("follow_ups")
    if not isinstance(follow_ups, Sequence) or not follow_ups:
        return "no_follow_ups"
    if rows:
        return None
    if gate_status(result) in {"expired", "declined", "cancelled"}:
        return "gate_expired"
    return "nothing_approved"


def _issue_title(row: FollowUpRow) -> str:
    title = row.title
    prefix = f"{row.project}: "
    if not title.lower().startswith(row.project.lower()):
        title = prefix + title
    return title[:MAX_TITLE_LENGTH]


def build_issue_request(
    row: FollowUpRow,
    context: FilingContext,
    plan: FollowUpFilingPlan,
) -> IssueRequest:
    """One approved row as one unit of work.

    The body is shaped for the automated issue implementation preset (011),
    which reads the title, the description and the issue url and nothing
    else: everything it would need (what to change, where, the evidence
    pointer, what done looks like) is in the description, and none of it is
    a link the implementer has to be able to open.
    """
    lines: list[str] = [
        "Follow up from a Preloop portfolio review, approved by a human at "
        "the filing gate.",
        "",
        "## What to do",
        "",
        row.title,
        "",
        "## Where",
        "",
        f"- Project: `{row.project}`",
    ]
    if context.repository:
        lines.append(f"- Repository: {context.repository}")
    if context.commit:
        lines.append(f"- Commit reviewed: `{context.commit}`")
    if row.evidence:
        lines.append(f"- Evidence: `{row.evidence}`")
    lines.extend(
        [
            "",
            "## Why it is here",
            "",
            f"- Priority: {row.severity}",
            f"- Reviewer note: {row.note or 'none'}",
            f"- Approved by: {row.approved_by or 'unrecorded'}"
            + (f" on {row.approved_at}" if row.approved_at else ""),
            "",
            "## Provenance",
            "",
            f"- {FOLLOW_UP_ID_HEADING}: `{row.id}`",
            f"- Portfolio review execution: {context.execution_id or 'unrecorded'}",
            "- Child execution: "
            + (row.child_execution_id or "none (the review ran inline)"),
        ]
    )
    if context.report_path:
        lines.append(f"- Portfolio report: `{context.report_path}`")
    lines.extend(
        [
            "",
            "## Done when",
            "",
            f"- The finding above no longer holds at {row.evidence or 'the pointer recorded in the report'}.",
            "- Nothing outside this project changed.",
            "",
            "Filed by Preloop. The review that produced it read the "
            "repository only; this issue is the first thing it wrote.",
        ]
    )
    labels = tuple(plan.labels)
    if row.severity in SEVERITIES:
        labels = labels + (f"priority:{row.severity}",)
    return IssueRequest(
        title=_issue_title(row),
        description="\n".join(lines),
        labels=tuple(dict.fromkeys(labels)),
        priority=row.severity,
    )


def _issue_record(created: Any) -> dict[str, Any]:
    """The identifier written back onto the follow up row."""
    if isinstance(created, Mapping):
        read = created.get
    else:

        def read(name: str, default: Any = None) -> Any:
            return getattr(created, name, default)

    record: dict[str, Any] = {}
    for name in ("id", "key", "url"):
        value = read(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            record[name] = text[:MAX_FIELD_LENGTH]
    return record


async def file_follow_ups(
    *,
    client: Any,
    project_key: str,
    rows: Sequence[FollowUpRow],
    context: FilingContext,
    plan: FollowUpFilingPlan,
    already_filed: Optional[Mapping[str, Mapping[str, Any]]] = None,
    tracker: Optional[str] = None,
) -> FilingOutcome:
    """File one issue per approved row, in order, against ``client``.

    Never raises: a tracker that refuses one row (or every row) produces a
    receipt, not an exception, because the run that produced the report has
    already succeeded by the time this is called.
    """
    from preloop.schemas.tracker_models import IssueCreate

    ledger = dict(already_filed or {})
    results: list[RowFiling] = []
    seen: set[str] = set()
    filed_here = 0

    for row in rows:
        if row.id in seen:
            results.append(
                RowFiling(
                    index=row.index,
                    follow_up_id=row.id,
                    outcome=ROW_NOT_FILED,
                    reason="duplicate_follow_up",
                )
            )
            continue
        seen.add(row.id)

        known = ledger.get(row.id)
        if known:
            results.append(
                RowFiling(
                    index=row.index,
                    follow_up_id=row.id,
                    outcome=ROW_ALREADY_FILED,
                    reason="already_filed",
                    issue=dict(known),
                )
            )
            continue

        if filed_here >= plan.max_issues:
            results.append(
                RowFiling(
                    index=row.index,
                    follow_up_id=row.id,
                    outcome=ROW_NOT_FILED,
                    reason="limit_reached",
                )
            )
            continue

        request = build_issue_request(row, context, plan)
        try:
            created = await client.create_issue(
                project_key=project_key,
                issue_data=IssueCreate(
                    title=request.title,
                    description=request.description,
                    priority=request.priority,
                    labels=list(request.labels),
                ),
            )
        except Exception:
            # The tracker message is not repeated into the result: it is
            # unbounded, may quote a credential, and an operator reading the
            # receipt needs the diagnosis, not the provider's prose.
            logger.exception("Filing follow up %s failed", row.id)
            results.append(
                RowFiling(
                    index=row.index,
                    follow_up_id=row.id,
                    outcome=ROW_FAILED,
                    reason="tracker_error",
                )
            )
            continue

        issue = _issue_record(created)
        if not issue:
            logger.warning("Tracker returned no identifier for follow up %s", row.id)
            results.append(
                RowFiling(
                    index=row.index,
                    follow_up_id=row.id,
                    outcome=ROW_FAILED,
                    reason="tracker_error",
                )
            )
            continue

        filed_here += 1
        ledger[row.id] = issue
        results.append(
            RowFiling(
                index=row.index,
                follow_up_id=row.id,
                outcome=ROW_FILED,
                issue=issue,
            )
        )

    return FilingOutcome(
        rows=tuple(results),
        tracker=_clean(tracker, limit=64),
        project=_clean(project_key, limit=200),
        considered=len(rows),
    )


def apply_filing_to_result(result: Any, outcome: FilingOutcome) -> dict[str, Any]:
    """Write the filing back onto the result: rows, rollup and receipt.

    The follow up row is where a reader looks, so the issue identifier lands
    there as well as in the receipt. ``rollup.issues_filed`` counts the follow
    ups that now have an issue, including the ones an earlier run filed:
    "filed" is a property of the follow up, not of this execution.
    """
    if not isinstance(result, dict):
        return {}
    receipt = outcome.receipt()
    result[FOLLOW_UP_FILING_RESULT_KEY] = receipt

    follow_ups = result.get("follow_ups")
    if not isinstance(follow_ups, list):
        return receipt

    # "filed" is the platform's word, on every row. A result that claims an
    # issue for a row this pass did not file is corrected here rather than
    # believed: the flow has no tool that could have created it.
    covered = {row.index for row in outcome.rows}
    for index, entry in enumerate(follow_ups):
        if index in covered or not isinstance(entry, dict):
            continue
        if entry.get("filed") is True or entry.get("filed_issue"):
            logger.warning("Clearing an unfiled follow up row that claimed an issue")
        entry["filed"] = False
        entry["filed_issue"] = None

    for row in outcome.rows:
        if not 0 <= row.index < len(follow_ups):
            continue
        entry = follow_ups[row.index]
        if not isinstance(entry, dict):
            continue
        entry["filing_status"] = row.outcome
        entry["filing_reason"] = row.reason or None
        if row.issue:
            entry["filed"] = True
            entry["filed_issue"] = dict(row.issue)
        else:
            entry["filed"] = False
            entry.setdefault("filed_issue", None)

    filed_total = sum(
        1
        for entry in follow_ups
        if isinstance(entry, dict) and entry.get("filed") is True
    )
    rollup = result.get("rollup")
    if isinstance(rollup, dict):
        rollup["issues_filed"] = filed_total
    return receipt


def collect_filed_follow_ups(
    results: Iterable[Any],
) -> dict[str, dict[str, Any]]:
    """The ledger: follow up id -> the issue an earlier run filed for it.

    Reads both the receipt and the follow up rows, because either can be the
    surviving record: a result restored from an older run may carry the rows
    without the receipt this module writes today. ``results`` is read newest
    first, and the first identifier seen for an id wins.
    """
    ledger: dict[str, dict[str, Any]] = {}
    for result in results:
        if not isinstance(result, Mapping):
            continue
        receipt = result.get(FOLLOW_UP_FILING_RESULT_KEY)
        if isinstance(receipt, Mapping):
            for row in receipt.get("rows") or []:
                if not isinstance(row, Mapping):
                    continue
                if row.get("outcome") not in {ROW_FILED, ROW_ALREADY_FILED}:
                    continue
                identifier = _clean(row.get("id"), limit=200)
                issue = row.get("issue")
                if (
                    identifier
                    and isinstance(issue, Mapping)
                    and identifier not in ledger
                ):
                    ledger[identifier] = _issue_record(issue)
        follow_ups = result.get("follow_ups")
        if not isinstance(follow_ups, Sequence):
            continue
        for entry in follow_ups:
            if not isinstance(entry, Mapping) or entry.get("filed") is not True:
                continue
            identifier = _clean(entry.get("id"), limit=200)
            issue = entry.get("filed_issue")
            if identifier and isinstance(issue, Mapping) and identifier not in ledger:
                ledger[identifier] = _issue_record(issue)
    return {key: value for key, value in ledger.items() if value}


def load_filed_follow_ups(
    db: Any,
    *,
    flow_id: Any,
    exclude_execution_id: Any = None,
    limit: int = LEDGER_EXECUTION_LIMIT,
) -> dict[str, dict[str, Any]]:
    """The ledger, read from this flow's own earlier executions.

    A portfolio is re-reviewed on a schedule, and the same unresolved follow
    up comes back with the same stable id every time. What stops a second
    issue is this: the identifiers earlier runs of the same flow recorded.
    """
    from preloop.models.crud import crud_flow_execution

    try:
        executions = crud_flow_execution.get_by_flow(db, flow_id=flow_id, limit=limit)
    except Exception:
        logger.exception("Could not read earlier executions for the follow up ledger")
        return {}

    results = []
    for execution in executions:
        if exclude_execution_id and str(getattr(execution, "id", "")) == str(
            exclude_execution_id
        ):
            continue
        result = getattr(execution, "result", None)
        if isinstance(result, Mapping):
            results.append(result)
    return collect_filed_follow_ups(results)
