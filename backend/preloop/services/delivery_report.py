"""The Delivery Report: what landed, whether it did what was asked (#2019).

The Delivery Report preset (``presets/022-delivery-report.yaml``) is a
scheduled flow that reads the account's trackers and forges and writes one
structured report per run as its ``result.json``. The stored result is the
report artifact: the next run receives it as ``previous/result.json``
through the schedule payload's ``previous_result_execution_id: "last"``
(see :mod:`preloop.utils.workspace_baseline`), and this module reads it
again on the terminal path to compute the delta.

The run works from tracker and forge data alone. Nothing here requires a
Preloop-managed agent to have produced the change.

Three things live here, all pure functions so they can be tested against
recorded tool responses:

* :class:`DeliveryReportPolicy`, the report policy as a config object with
  the design's defaults (cadence, budget, open-item windows, tracker write
  rules). There is no policy UI yet; the preset carries the defaults.
* :func:`assemble_report`, the deterministic report built from
  ``get_pull_request`` and ``get_issue`` responses. It is also what a run
  sends when the budget leaves no room for assessments.
* :func:`finalize_delivery_report`, the platform pass over the agent's
  report: it enforces the honesty rules (a verdict needs evidence; an issue
  without criteria is ``no_criteria``, never ``covered``), recomputes the
  summary strip and the "needs you" list, carries the previous report's
  open questions into the delta, and stamps what the run spent from gateway
  usage. The agent cannot author the spend or the delta.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, Field, field_validator

#: ``agent_config`` key that marks a flow as a Delivery Report, so the
#: orchestrator knows to finalize its result.
REPORT_KIND_KEY = "report_kind"
DELIVERY_REPORT_KIND = "delivery_report"
#: Schema id written on every report, so a reader can tell versions apart.
REPORT_SCHEMA = "preloop.delivery_report/v1"

PURPOSE_COVERED = "covered"
PURPOSE_PARTIAL = "partial"
PURPOSE_UNVERIFIED = "unverified"
PURPOSE_NO_CRITERIA = "no_criteria"
PURPOSE_VERDICTS = (
    PURPOSE_COVERED,
    PURPOSE_PARTIAL,
    PURPOSE_UNVERIFIED,
    PURPOSE_NO_CRITERIA,
)

RISK_HOT_PATH_WITHOUT_TESTS = "hot_path_without_tests"
RISK_PUBLIC_API_WITHOUT_VERSION_NOTE = "public_api_without_version_note"
RISK_UI_WITHOUT_SCREENSHOT = "ui_without_screenshot"
RISK_MIGRATION_WITHOUT_DOWNGRADE = "migration_without_downgrade"
RISK_FLAGS = (
    RISK_HOT_PATH_WITHOUT_TESTS,
    RISK_PUBLIC_API_WITHOUT_VERSION_NOTE,
    RISK_UI_WITHOUT_SCREENSHOT,
    RISK_MIGRATION_WITHOUT_DOWNGRADE,
)

#: Report sections in rendering order (the design's console layout).
SECTIONS = (
    "summary",
    "needs_you",
    "shipped",
    "closed_issues",
    "open_past_window",
    "delta",
    "spend",
)

WriteRule = Literal["allow", "approval", "deny"]
DropStep = Literal["risk_detail", "assessments_beyond_n"]
DEFAULT_DROP_ORDER: tuple[DropStep, ...] = ("risk_detail", "assessments_beyond_n")

_CLOSED_STATES = frozenset({"closed", "done", "resolved", "completed"})
_CLOSING_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+"
    r"((?:[\w.-]+/[\w.-]+)?#\d+|[A-Z][A-Z0-9]+-\d+)",
    re.IGNORECASE,
)
_TRIAGE_START = "<!-- preloop-issue-triage:start -->"
_TRIAGE_END = "<!-- preloop-issue-triage:end -->"
_ACCEPTANCE_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s*|\*\*)?acceptance(?:\s+criteria)?\b[.:]?(?:\*\*)?[.:]?\s*(.*)$",
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s+\S|\*\*[^*]+\*\*\s*$|\*\*[^*]+[.:]\*\*)")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*\S)\s*$")

_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|_test\.|\.spec\.|\.test\."
)
_MIGRATION_PATH_RE = re.compile(r"(^|/)(alembic|migrations?)/(versions/)?[^/]+\.py$")
_UI_PATH_RE = re.compile(r"\.(tsx?|jsx?|css|scss|html|vue|svelte)$")
_PUBLIC_API_PATH_RE = re.compile(r"(^|/)(openapi\.ya?ml|api/endpoints/[^/]+\.py)$")
_VERSION_NOTE_PATH_RE = re.compile(r"(^|/)(CHANGELOG[^/]*|VERSION|pyproject\.toml)$")
_SCREENSHOT_RE = re.compile(r"!\[|\.(png|jpe?g|gif|webp)\b|<img\b", re.IGNORECASE)
_DOC_PATH_RE = re.compile(r"\.(md|rst|txt)$|(^|/)docs/")


class TrackerWritePolicy(BaseModel):
    """What the run may do to the tracker. Writes ship in #2021.

    Closing is never done by the report, so ``close`` only accepts ``deny``.
    """

    create_follow_up: WriteRule = "allow"
    reopen: WriteRule = "approval"
    close: Literal["deny"] = "deny"
    follow_up_label: str = "preloop:proposed"


class DeliveryReportPolicy(BaseModel):
    """The report policy, per account and overridable per repository.

    Defaults are the design's. The per-run ceiling is a placeholder until
    the dogfood runs measure real cost; it is applied as the flow's
    ``agent_config.limits.max_usd`` so the gateway enforces it like any
    other execution limit.
    """

    cadence: Literal["weekly", "daily", "manual"] = "weekly"
    weekday: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "mon"
    at: str = "07:00"
    timezone: str = "UTC"
    #: Project identifiers in scope; empty means every linked project.
    projects: List[str] = Field(default_factory=list)
    per_run_usd: float = Field(default=5.0, gt=0)
    monthly_usd: float = Field(default=25.0, gt=0)
    #: What to drop first when the budget runs short. The shipped list is
    #: never dropped.
    drop_order: List[DropStep] = Field(default_factory=lambda: list(DEFAULT_DROP_ORDER))
    max_assessed_changes: int = Field(default=25, ge=0)
    pr_ready_days: int = Field(default=7, ge=1)
    issue_inactive_days: int = Field(default=14, ge=1)
    criteria_source: Literal["triage_assessment", "description"] = "triage_assessment"
    tracker_writes: TrackerWritePolicy = Field(default_factory=TrackerWritePolicy)

    @field_validator("at")
    @classmethod
    def _valid_time(cls, value: str) -> str:
        """Require HH:MM, the shape the schedule forms take."""
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("at must be HH:MM")
        return value

    def schedule_config(self) -> Optional[Dict[str, Any]]:
        """The flow ``schedule_config`` for this cadence; None for manual.

        The payload asks for the flow's own previous result, which is how
        the run reads the previous report.
        """
        payload = {"previous_result_execution_id": "last"}
        if self.cadence == "manual":
            return None
        if self.cadence == "daily":
            return {
                "type": "daily",
                "at": self.at,
                "timezone": self.timezone,
                "payload": payload,
            }
        return {
            "type": "weekly",
            "days": [self.weekday],
            "at": self.at,
            "timezone": self.timezone,
            "payload": payload,
        }

    def execution_limits(self) -> Dict[str, Any]:
        """The ``agent_config.limits`` block that enforces the run ceiling."""
        return {"max_usd": self.per_run_usd}


DEFAULT_POLICY = DeliveryReportPolicy()


# ---------------------------------------------------------------------------
# Reading tool responses
# ---------------------------------------------------------------------------


def _parse_time(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp from a tool response; None when absent."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _in_window(value: Any, start: Optional[datetime], end: datetime) -> bool:
    moment = _parse_time(value)
    if moment is None:
        return False
    return (start is None or moment >= start) and moment <= end


def _strs(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    return [str(v).strip() for v in values if isinstance(v, str) and v.strip()]


def linked_issue_refs(text: Optional[str]) -> List[str]:
    """Issue references a PR body closes with a closing keyword."""
    if not text:
        return []
    refs: List[str] = []
    for match in _CLOSING_RE.finditer(text):
        ref = match.group(1)
        if ref not in refs:
            refs.append(ref)
    return refs


def _issue_matches_ref(issue: Mapping[str, Any], ref: str) -> bool:
    key = str(issue.get("key") or "")
    number = ref.rsplit("#", 1)[-1] if "#" in ref else None
    if key == ref:
        return True
    if number is not None:
        return key.endswith(f"#{number}") or key == number
    return False


def extract_criteria(description: Optional[str], *, source: str) -> List[str]:
    """Acceptance criteria from an issue body, or an empty list.

    The triage assessment section is read first when the policy names it;
    otherwise (or when it has none) an "Acceptance" heading in the body is
    used. Bullets under the heading are one criterion each; a heading with
    inline prose is one criterion. Nothing found means no criteria: the
    report never invents them.
    """
    if not description:
        return []
    texts: List[str] = []
    if source == "triage_assessment" and _TRIAGE_START in description:
        start = description.index(_TRIAGE_START) + len(_TRIAGE_START)
        end = description.find(_TRIAGE_END, start)
        texts.append(description[start : end if end != -1 else None])
    texts.append(description)
    for text in texts:
        criteria = _criteria_under_heading(text)
        if criteria:
            return criteria
    return []


def _criteria_under_heading(text: str) -> List[str]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _ACCEPTANCE_HEADING_RE.match(line)
        if not match:
            continue
        criteria: List[str] = []
        inline = match.group(1).strip()
        for follow in lines[index + 1 :]:
            if _HEADING_RE.match(follow) or _ACCEPTANCE_HEADING_RE.match(follow):
                break
            bullet = _BULLET_RE.match(follow)
            if bullet:
                criteria.append(bullet.group(1))
            elif not follow.strip() and criteria:
                break
            elif follow.strip() and not criteria:
                inline = f"{inline} {follow.strip()}".strip()
        if criteria:
            return criteria
        if inline:
            return [inline]
    return []


def _changed_files(pr: Mapping[str, Any]) -> List[Dict[str, Any]]:
    changes = pr.get("changes")
    if not isinstance(changes, Mapping):
        return []
    files = changes.get("changed_files")
    if not isinstance(files, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for f in files:
        if not isinstance(f, Mapping):
            continue
        # GitHub: filename/patch. GitLab: new_path/diff. Bitbucket: path.
        name = f.get("filename") or f.get("new_path") or f.get("path")
        if not name:
            continue
        normalized.append(
            {"filename": str(name), "patch": f.get("patch") or f.get("diff") or ""}
        )
    return normalized


def detect_risk_flags(pr: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Risk flags readable from the diff surface alone, each with evidence.

    These are the design's four regression classes, judged mechanically: a
    flag here always names the files that raised it. "Hot path" is read as
    any non-test, non-doc source change; the agent narrows it with context.
    """
    files = _changed_files(pr)
    names = [str(f["filename"]) for f in files]
    tests = [n for n in names if _TEST_PATH_RE.search(n)]
    code = [
        n
        for n in names
        if n not in tests
        and not _DOC_PATH_RE.search(n)
        and not _VERSION_NOTE_PATH_RE.search(n)
    ]
    flags: List[Dict[str, Any]] = []
    if code and not tests:
        flags.append(
            {
                "flag": RISK_HOT_PATH_WITHOUT_TESTS,
                "evidence": [f"{n} changed; no test file in the diff" for n in code],
            }
        )
    api = [n for n in names if _PUBLIC_API_PATH_RE.search(n)]
    if api and not any(_VERSION_NOTE_PATH_RE.search(n) for n in names):
        flags.append(
            {
                "flag": RISK_PUBLIC_API_WITHOUT_VERSION_NOTE,
                "evidence": [f"{n} changed; no changelog or version file" for n in api],
            }
        )
    ui = [n for n in code if _UI_PATH_RE.search(n)]
    if ui and not _SCREENSHOT_RE.search(str(pr.get("description") or "")):
        flags.append(
            {
                "flag": RISK_UI_WITHOUT_SCREENSHOT,
                "evidence": [f"{n} changed; no screenshot in the PR body" for n in ui],
            }
        )
    for f in files:
        name = str(f["filename"])
        if not _MIGRATION_PATH_RE.search(name):
            continue
        patch = str(f.get("patch") or "")
        if not patch:
            # No per-file diff (Bitbucket): cannot tell, so no flag.
            continue
        downgrade = re.search(r"def downgrade\(\)[^:]*:\s*(.*)", patch, re.DOTALL)
        body = downgrade.group(1) if downgrade else ""
        body_lines = [
            ln.strip()[1:].strip()
            for ln in body.splitlines()
            if ln.strip().startswith("+")
        ]
        meaningful = [ln for ln in body_lines if ln and not ln.startswith(("#", '"'))]
        if not downgrade or meaningful[:1] == ["pass"] or not meaningful:
            flags.append(
                {
                    "flag": RISK_MIGRATION_WITHOUT_DOWNGRADE,
                    "evidence": [f"{name}: downgrade() is missing or empty"],
                }
            )
    return flags


def _area(pr: Mapping[str, Any]) -> str:
    """The top-level directory most of the diff touched."""
    counts: Dict[str, int] = {}
    for f in _changed_files(pr):
        top = str(f["filename"]).split("/", 1)[0]
        counts[top] = counts.get(top, 0) + 1
    if not counts:
        return "unknown"
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


_PR_URL_RE = re.compile(
    r"/([^/]+/[^/]+?)(?:/-)?/(?:pull|pulls|merge_requests|pull-requests)/(\d+)"
)


def _pr_ref(pr: Mapping[str, Any]) -> str:
    """``owner/repo#number``, from the PR URL (the response has no project)."""
    match = _PR_URL_RE.search(str(pr.get("url") or ""))
    if match:
        return f"{match.group(1)}#{match.group(2)}"
    return f"{pr.get('project') or ''}#{pr.get('number')}"


# ---------------------------------------------------------------------------
# Honesty rules
# ---------------------------------------------------------------------------


def _evidence(value: Any) -> List[str]:
    return _strs(value)


def normalize_purpose(purpose: Any) -> Dict[str, Any]:
    """Apply the honesty rules to one purpose assessment.

    * No criteria: ``no_criteria``, whatever the claim.
    * ``covered`` needs every criterion met with evidence.
    * ``partial`` needs at least one criterion met with evidence.
    * Anything else, or a claim the evidence does not carry, is
      ``unverified``. A verdict is never better than its evidence.
    """
    # Untrusted agent JSON: anything but a mapping is no assessment at all.
    purpose = dict(purpose) if isinstance(purpose, Mapping) else {}
    raw_criteria = purpose.get("criteria")
    criteria: List[Dict[str, Any]] = []
    for item in raw_criteria if isinstance(raw_criteria, list) else []:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, Mapping) or not str(item.get("text") or "").strip():
            continue
        evidence = _evidence(item.get("evidence"))
        met = item.get("met")
        # A criterion is only met when the evidence is cited.
        if met is True and not evidence:
            met = None
        if met not in (True, False):
            met = None
        criteria.append(
            {"text": str(item["text"]).strip(), "met": met, "evidence": evidence}
        )

    claimed = purpose.get("verdict")
    if not criteria:
        verdict = PURPOSE_NO_CRITERIA
    else:
        met_count = sum(1 for c in criteria if c["met"] is True)
        if met_count == len(criteria):
            verdict = PURPOSE_COVERED
        elif met_count:
            verdict = PURPOSE_PARTIAL
        else:
            verdict = PURPOSE_UNVERIFIED
        # The agent may be more cautious than the evidence, never less.
        rank = {PURPOSE_UNVERIFIED: 0, PURPOSE_PARTIAL: 1, PURPOSE_COVERED: 2}
        if (
            isinstance(claimed, str)
            and claimed in rank
            and rank[claimed] < rank[verdict]
        ):
            verdict = claimed
    normalized: Dict[str, Any] = {"verdict": verdict, "criteria": criteria}
    reason = purpose.get("reason")
    if isinstance(reason, str) and reason.strip():
        normalized["reason"] = reason.strip()
    elif verdict == PURPOSE_NO_CRITERIA:
        normalized["reason"] = "no criteria stated"
    if isinstance(claimed, str) and claimed != verdict:
        normalized["claimed_verdict"] = claimed
    return normalized


def normalize_risk_flags(flags: Any) -> List[Dict[str, Any]]:
    """Keep risk flags that carry a line of evidence; drop the rest."""
    kept: List[Dict[str, Any]] = []
    for flag in flags if isinstance(flags, list) else []:
        if not isinstance(flag, Mapping):
            continue
        name = str(flag.get("flag") or "").strip()
        evidence = _evidence(flag.get("evidence"))
        if name and evidence:
            kept.append({"flag": name, "evidence": evidence})
    return kept


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def assemble_report(
    *,
    window: Mapping[str, Any],
    pull_requests: Sequence[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
    assessments: Optional[Mapping[str, Mapping[str, Any]]] = None,
    previous: Optional[Mapping[str, Any]] = None,
    policy: DeliveryReportPolicy = DEFAULT_POLICY,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build the report from ``get_pull_request`` and ``get_issue`` responses.

    Args:
        window: ``{"from": iso | None, "to": iso}``, the schedule window.
        pull_requests: PR responses read this run (merged and open).
        issues: Issue responses read this run (touched, closed and open).
        assessments: Agent assessments keyed by PR ref (``project#number``):
            ``{"purpose": {...}, "risk_flags": [...], "summary": str}``.
            Missing entries get the deterministic assessment only.
        previous: The previous report, for the delta.
        policy: The report policy.
        now: Clock for ages; defaults to the window end.

    Returns:
        The report document with every section of :data:`SECTIONS`.
    """
    end = _parse_time(window.get("to")) or now or datetime.now(timezone.utc)
    start = _parse_time(window.get("from"))
    clock = now or end
    assessments = assessments or {}
    issue_by_ref: Dict[str, Mapping[str, Any]] = {}

    shipped: List[Dict[str, Any]] = []
    linked_by_issue: Dict[str, List[str]] = {}
    for pr in pull_requests:
        if not pr.get("merged_at") or not _in_window(pr.get("merged_at"), start, end):
            continue
        ref = _pr_ref(pr)
        links = linked_issue_refs(pr.get("description"))
        criteria: List[str] = []
        for link in links:
            linked_by_issue.setdefault(link, []).append(ref)
            issue = next((i for i in issues if _issue_matches_ref(i, link)), None)
            if issue is not None:
                issue_by_ref[link] = issue
                criteria.extend(
                    extract_criteria(
                        issue.get("description"), source=policy.criteria_source
                    )
                )
        agent = assessments.get(ref) or {}
        purpose = dict(agent.get("purpose") or {})
        if "criteria" not in purpose:
            purpose["criteria"] = criteria
        risk = detect_risk_flags(pr) + list(agent.get("risk_flags") or [])
        shipped.append(
            {
                "ref": ref,
                "title": pr.get("title"),
                "url": pr.get("url"),
                "project": _pr_ref(pr).split("#", 1)[0] or None,
                "area": _area(pr),
                "author": pr.get("author"),
                "merged_at": pr.get("merged_at"),
                "issues": links,
                "summary": agent.get("summary"),
                "purpose": purpose,
                "risk_flags": risk,
                "cost_usd": agent.get("cost_usd"),
            }
        )

    closed: List[Dict[str, Any]] = []
    for issue in issues:
        status = str(issue.get("status") or "").lower()
        if status not in _CLOSED_STATES:
            continue
        if not _in_window(issue.get("updated_at"), start, end):
            continue
        key = str(issue.get("key") or "")
        prs = next(
            (v for k, v in linked_by_issue.items() if _issue_matches_ref(issue, k)), []
        )
        criteria_texts = extract_criteria(
            issue.get("description"), source=policy.criteria_source
        )
        met: Dict[str, Dict[str, Any]] = {}
        for change in shipped:
            if change["ref"] not in prs:
                continue
            for c in normalize_purpose(change["purpose"])["criteria"]:
                if c["met"] is True:
                    met.setdefault(c["text"], c)
        closed.append(
            {
                "ref": key,
                "title": issue.get("title"),
                "url": issue.get("url"),
                "closed_by": prs,
                "purpose": {
                    "criteria": [
                        met.get(text, {"text": text, "met": None, "evidence": []})
                        for text in criteria_texts
                    ]
                },
            }
        )

    open_items: List[Dict[str, Any]] = []
    for pr in pull_requests:
        if pr.get("merged_at") or str(pr.get("state") or "").lower() != "open":
            continue
        opened = _parse_time(pr.get("created_at"))
        if opened is None:
            continue
        age = (clock - opened).days
        if age >= policy.pr_ready_days:
            open_items.append(
                {
                    "kind": "pull_request",
                    "ref": _pr_ref(pr),
                    "title": pr.get("title"),
                    "url": pr.get("url"),
                    "age_days": age,
                    "draft": bool(pr.get("is_draft")),
                }
            )
    for issue in issues:
        if str(issue.get("status") or "").lower() in _CLOSED_STATES:
            continue
        touched = _parse_time(issue.get("updated_at"))
        if touched is None:
            continue
        idle = (clock - touched).days
        if idle >= policy.issue_inactive_days:
            open_items.append(
                {
                    "kind": "issue",
                    "ref": issue.get("key"),
                    "title": issue.get("title"),
                    "url": issue.get("url"),
                    "age_days": idle,
                }
            )

    report: Dict[str, Any] = {
        "status": "success",
        "schema": REPORT_SCHEMA,
        "window": {"from": window.get("from"), "to": window.get("to")},
        "shipped": shipped,
        "closed_issues": closed,
        "open_past_window": open_items,
        "follow_ups": [],
        "open_questions": [],
    }
    return finalize_delivery_report(report, previous=previous)


def _needs_you(report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for issue in report.get("closed_issues") or []:
        purpose = issue.get("purpose") or {}
        if purpose.get("verdict") in (PURPOSE_PARTIAL, PURPOSE_UNVERIFIED):
            unmet = [
                c["text"] for c in purpose.get("criteria") or [] if c["met"] is not True
            ]
            items.append(
                {
                    "kind": "closed_unverified",
                    "ref": issue.get("ref"),
                    "title": issue.get("title"),
                    "url": issue.get("url"),
                    "unmet_criteria": unmet,
                }
            )
    for change in report.get("shipped") or []:
        if change.get("risk_flags"):
            items.append(
                {
                    "kind": "risk",
                    "ref": change.get("ref"),
                    "title": change.get("title"),
                    "url": change.get("url"),
                    "flags": [f["flag"] for f in change["risk_flags"]],
                }
            )
    for item in report.get("open_past_window") or []:
        items.append(
            {
                "kind": "aged",
                "ref": item.get("ref"),
                "title": item.get("title"),
                "url": item.get("url"),
                "age_days": item.get("age_days"),
            }
        )
    return items


def _questions(raw: Any) -> List[Dict[str, Any]]:
    """Open questions as ``{"question", "ref"}`` rows; junk entries dropped."""
    rows: List[Dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            item = {"question": item}
        if not isinstance(item, Mapping):
            continue
        text = item.get("question")
        if not isinstance(text, str) or not text.strip():
            continue
        ref = item.get("ref")
        rows.append(
            {"question": text.strip(), "ref": ref if isinstance(ref, str) else None}
        )
    return rows


def build_delta(
    current: Mapping[str, Any], previous: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    """What changed since the previous report.

    Every open question the previous report asked appears here, marked
    ``answered`` when this report's shipped or closed items reference what
    it asked about, otherwise ``still_open``. A question is never silently
    dropped between reports.
    """
    if not isinstance(previous, Mapping) or previous.get("schema") != REPORT_SCHEMA:
        return {"previous_report": None, "open_questions": []}
    resolved_refs = {str(c.get("ref")) for c in current.get("shipped") or []}
    resolved_refs |= {
        str(i.get("ref"))
        for i in current.get("closed_issues") or []
        if (i.get("purpose") or {}).get("verdict") == PURPOSE_COVERED
    }
    questions: List[Dict[str, Any]] = []
    for question in _questions(previous.get("open_questions")):
        ref = question.get("ref")
        answered = bool(ref) and str(ref) in resolved_refs
        questions.append(
            {
                "question": question.get("question"),
                "ref": ref,
                "status": "answered" if answered else "still_open",
            }
        )
    previous_open = {str(i.get("ref")) for i in previous.get("open_past_window") or []}
    current_open = {str(i.get("ref")) for i in current.get("open_past_window") or []}
    previous_summary = previous.get("summary") or {}
    return {
        "previous_report": {
            "window": previous.get("window"),
            "shipped": previous_summary.get("shipped"),
            "spend_usd": (previous.get("spend") or {}).get("cost_usd"),
        },
        "open_questions": questions,
        "newly_aged": sorted(current_open - previous_open),
        "no_longer_aged": sorted(previous_open - current_open),
    }


def record_spend(
    report: Dict[str, Any],
    *,
    cost_usd: Optional[float],
    total_tokens: Optional[int],
    limit_usd: Optional[float],
    skipped: Iterable[str] = (),
) -> Dict[str, Any]:
    """Stamp what the run spent. Platform-owned: overwrites any agent claim.

    ``cost_usd`` None means the gateway could not price the run, which is
    reported as unknown, never as zero.
    """
    raw_spend = report.get("spend")
    agent_spend: Mapping[str, Any] = raw_spend if isinstance(raw_spend, Mapping) else {}
    skipped_items = list(
        dict.fromkeys([*_strs(agent_spend.get("skipped_for_budget")), *skipped])
    )
    report["spend"] = {
        "cost_usd": None if cost_usd is None else round(float(cost_usd), 4),
        "total_tokens": total_tokens,
        "limit_usd": limit_usd,
        "skipped_for_budget": skipped_items,
        "source": "gateway",
    }
    return report


def finalize_delivery_report(
    report: Any,
    *,
    previous: Optional[Mapping[str, Any]] = None,
    cost_usd: Optional[float] = None,
    total_tokens: Optional[int] = None,
    limit_usd: Optional[float] = None,
) -> Dict[str, Any]:
    """The platform pass over a report: honesty rules, summary, delta, spend.

    Safe on a malformed or error result: a non-dict becomes an error
    report; an ``error`` report keeps its reason and still records spend.
    """
    if not isinstance(report, Mapping):
        report = {"status": "error", "reason": "result was not a report object"}
    out: Dict[str, Any] = dict(report)
    out["schema"] = REPORT_SCHEMA
    for key in ("shipped", "closed_issues", "open_past_window", "follow_ups"):
        if not isinstance(out.get(key), list):
            out[key] = []
    out["open_questions"] = _questions(out.get("open_questions"))

    shipped = []
    for change in out["shipped"]:
        if not isinstance(change, Mapping):
            continue
        change = dict(change)
        change["purpose"] = normalize_purpose(change.get("purpose"))
        change["risk_flags"] = normalize_risk_flags(change.get("risk_flags"))
        shipped.append(change)
    out["shipped"] = shipped
    closed = []
    for issue in out["closed_issues"]:
        if not isinstance(issue, Mapping):
            continue
        issue = dict(issue)
        issue["purpose"] = normalize_purpose(issue.get("purpose"))
        closed.append(issue)
    out["closed_issues"] = closed

    out["needs_you"] = _needs_you(out)
    out["summary"] = {
        "shipped": len(shipped),
        "closed": len(closed),
        "closed_unverified": sum(
            1 for i in out["needs_you"] if i["kind"] == "closed_unverified"
        ),
        "risk_flagged": sum(1 for c in shipped if c["risk_flags"]),
        "open_past_window": len(out["open_past_window"]),
        "follow_ups_proposed": len(out["follow_ups"]),
        "verdicts": {
            v: sum(1 for c in shipped if c["purpose"]["verdict"] == v)
            for v in PURPOSE_VERDICTS
        },
    }
    out["delta"] = build_delta(out, previous)
    # Persist every question still open, this period's and the previous
    # report's, so the next report can carry them even if this run failed
    # or the agent forgot to repeat one.
    asked = {q["question"] for q in out["open_questions"]}
    for carried in out["delta"]["open_questions"]:
        if carried["status"] == "still_open" and carried["question"] not in asked:
            out["open_questions"].append(
                {"question": carried["question"], "ref": carried.get("ref")}
            )
            asked.add(carried["question"])
    record_spend(out, cost_usd=cost_usd, total_tokens=total_tokens, limit_usd=limit_usd)
    out["report_markdown"] = render_markdown(out)
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_VERDICT_LABELS = {
    PURPOSE_COVERED: "covered",
    PURPOSE_PARTIAL: "partial",
    PURPOSE_UNVERIFIED: "could not verify",
    PURPOSE_NO_CRITERIA: "no criteria stated",
}


def _link(item: Mapping[str, Any]) -> str:
    ref = item.get("ref") or ""
    title = item.get("title") or ""
    url = item.get("url")
    label = f"{ref} {title}".strip()
    return f"[{label}]({url})" if url else label


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render the report as markdown, one heading per section."""
    window = report.get("window") or {}
    summary = report.get("summary") or {}
    lines = [
        f"# Delivery report {window.get('from') or 'start'} to {window.get('to') or 'now'}",
        "",
        "## Summary",
        "",
        f"Shipped {summary.get('shipped', 0)}, closed {summary.get('closed', 0)} "
        f"({summary.get('closed_unverified', 0)} unverified), "
        f"{summary.get('risk_flagged', 0)} with risk flags, "
        f"{summary.get('open_past_window', 0)} open past the window.",
    ]
    if report.get("status") == "error":
        lines += ["", f"The run did not complete: {report.get('reason') or 'unknown'}."]

    lines += ["", "## Needs you", ""]
    needs = report.get("needs_you") or []
    for item in needs:
        if item["kind"] == "closed_unverified":
            unmet = "; ".join(item.get("unmet_criteria") or []) or "criteria unchecked"
            lines.append(f"- Closed but unverified: {_link(item)}. Unmet: {unmet}")
        elif item["kind"] == "risk":
            lines.append(f"- Risk: {_link(item)} ({', '.join(item['flags'])})")
        else:
            lines.append(f"- Aged {item.get('age_days')} days: {_link(item)}")
    if not needs:
        lines.append("Nothing needs you this period.")

    lines += ["", "## Shipped", ""]
    for change in report.get("shipped") or []:
        purpose = change["purpose"]
        lines.append(
            f"- {_link(change)} ({change.get('area') or 'unknown'}): purpose "
            f"{_VERDICT_LABELS[purpose['verdict']]}"
        )
        for c in purpose["criteria"]:
            state = {True: "met", False: "not met", None: "could not verify"}[c["met"]]
            cite = f" ({'; '.join(c['evidence'])})" if c["evidence"] else ""
            lines.append(f"  - {c['text']}: {state}{cite}")
        for flag in change["risk_flags"]:
            lines.append(f"  - Risk {flag['flag']}: {'; '.join(flag['evidence'])}")
    if not report.get("shipped"):
        lines.append("Nothing merged in the window.")

    lines += ["", "## Closed issues", ""]
    for issue in report.get("closed_issues") or []:
        verdict = _VERDICT_LABELS[issue["purpose"]["verdict"]]
        lines.append(f"- {_link(issue)}: {verdict}")
    if not report.get("closed_issues"):
        lines.append("No issues closed in the window.")

    lines += ["", "## Open past the window", ""]
    for item in report.get("open_past_window") or []:
        lines.append(f"- {item.get('kind')} {_link(item)}: {item.get('age_days')} days")
    if not report.get("open_past_window"):
        lines.append("Nothing aged past the policy window.")

    lines += ["", "## Since the previous report", ""]
    delta = report.get("delta") or {}
    if not delta.get("previous_report"):
        lines.append("No previous report to compare with.")
    for q in delta.get("open_questions") or []:
        lines.append(f"- {q['status'].replace('_', ' ')}: {q.get('question')}")

    lines += ["", "## Open questions", ""]
    for q in report.get("open_questions") or []:
        ref = f" ({q['ref']})" if q.get("ref") else ""
        lines.append(f"- {q['question']}{ref}")
    if not report.get("open_questions"):
        lines.append("No open questions.")

    spend = report.get("spend") or {}
    cost = spend.get("cost_usd")
    lines += [
        "",
        "## What this run spent",
        "",
        f"{'unknown' if cost is None else f'${cost:.2f}'}"
        + (
            f" of a ${spend['limit_usd']:.2f} ceiling"
            if spend.get("limit_usd") is not None
            else ""
        )
        + (
            f". Skipped for budget: {', '.join(spend['skipped_for_budget'])}"
            if spend.get("skipped_for_budget")
            else ""
        )
        + ".",
    ]
    return "\n".join(lines) + "\n"


def is_delivery_report_flow(flow: Any) -> bool:
    """Whether a flow's result should be finalized as a Delivery Report."""
    config = getattr(flow, "agent_config", None)
    return isinstance(config, Mapping) and (
        config.get(REPORT_KIND_KEY) == DELIVERY_REPORT_KIND
    )


def finalize_for_execution(
    db: Any, *, flow: Any, execution: Any, result: Any
) -> Dict[str, Any]:
    """Finalize a Delivery Report run's result on the terminal path.

    Reads the flow's previous stored report (same account, this execution
    excluded) and the gateway usage of this execution through the CRUD
    layer, then applies :func:`finalize_delivery_report`. Either read
    failing degrades to "no previous report" or "spend unknown"; it never
    fails the run.
    """
    import logging

    from preloop.models.crud import crud_flow_execution
    from preloop.services.flow_execution_limits import (
        get_execution_usage,
        parse_execution_limits,
    )

    log = logging.getLogger(__name__)
    previous: Optional[Mapping[str, Any]] = None
    account_id = getattr(flow, "account_id", None)
    if account_id:
        try:
            row = crud_flow_execution.latest_with_result(
                db,
                flow_id=flow.id,
                account_id=str(account_id),
                exclude_execution_id=getattr(execution, "id", None),
                # A failed run's error result is not a report to diff
                # against; its questions are carried on the result anyway.
                succeeded_only=True,
            )
            if row is not None and isinstance(row.result, Mapping):
                previous = row.result
        except Exception:  # noqa: BLE001 - a missing delta never fails a run
            log.warning("Could not read the previous delivery report", exc_info=True)
    cost: Optional[float] = None
    tokens: Optional[int] = None
    try:
        usage = get_execution_usage(db, execution.id)
        cost, tokens = usage.cost_usd, usage.total_tokens
    except Exception:  # noqa: BLE001 - unknown spend is reported as unknown
        log.warning("Could not read delivery report spend", exc_info=True)
    limits = parse_execution_limits(getattr(flow, "agent_config", None))
    return finalize_delivery_report(
        result,
        previous=previous,
        cost_usd=cost,
        total_tokens=tokens,
        limit_usd=limits.max_usd,
    )
