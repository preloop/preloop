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
from datetime import datetime, timedelta, timezone
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
RISK_SENSITIVE_PATH = "sensitive_path"
RISK_FILE_LIST_INCOMPLETE = "file_list_incomplete"
RISK_FLAGS = (
    RISK_HOT_PATH_WITHOUT_TESTS,
    RISK_PUBLIC_API_WITHOUT_VERSION_NOTE,
    RISK_UI_WITHOUT_SCREENSHOT,
    RISK_MIGRATION_WITHOUT_DOWNGRADE,
    RISK_SENSITIVE_PATH,
    RISK_FILE_LIST_INCOMPLETE,
)

#: Where an assessment came from. ``audit`` is a completed merge audit
#: (preset 013) at the closing revision; ``report`` is this run's model
#: pass; ``cache`` is the previous report's assessment of an unchanged
#: closed issue; ``metadata`` is a dependabot or docs-only change reported
#: without a model call; ``none`` is not assessed.
SOURCE_AUDIT = "audit"
SOURCE_REPORT = "report"
SOURCE_CACHE = "cache"
SOURCE_METADATA = "metadata"
SOURCE_NONE = "none"
ASSESSMENT_SOURCES = (
    SOURCE_AUDIT,
    SOURCE_REPORT,
    SOURCE_CACHE,
    SOURCE_METADATA,
    SOURCE_NONE,
)

#: Workspace path of the completed merge audits the run receives, next to
#: the previous report.
MERGE_AUDITS_WORKSPACE_PATH = "previous/merge_audits.json"
MERGE_AUDITS_SCHEMA = "preloop.merge_audits/v1"

#: Forge file lists stop at 100 entries per page; at or above this the
#: list must have been read with pagination to be trusted.
FILE_LIST_PAGE_LIMIT = 100

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
ReportMode = Literal["deterministic", "assessed"]
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
_STYLE_PATH_RE = re.compile(r"\.(css|scss|sass|less)$")
_VIEW_PATH_RE = re.compile(
    r"(^|/)(views?|pages|templates)/[^/]+$|\.(html|vue|svelte)$", re.IGNORECASE
)
_FRONTEND_PATH_RE = re.compile(r"(^|/)(frontend|web|ui|client)/")
_MERGE_REVISION_RE = re.compile(r"^\+?\s*down_revision\b[^=\n]*=\s*[\(\[]", re.M)
_DEPENDABOT_RE = re.compile(r"^(dependabot|renovate)(\[bot\])?$", re.IGNORECASE)

#: Path classes that always deserve a reader's eye, whatever the diff says.
#: Each is (class, pattern); the first match names the class.
PATH_CLASSES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    (
        "auth_session",
        re.compile(
            r"(^|/|_)(auth[nz]?|authentication|authorization|sessions?|login|"
            r"oauth2?|sso|jwt|permissions?)"
            r"([/_.]|$)",
            re.IGNORECASE,
        ),
    ),
    ("migration", _MIGRATION_PATH_RE),
    (
        "billing_payments",
        re.compile(
            r"(^|/|_)(billing|payments?|stripe|invoices?|subscriptions?|pricing)"
            r"([/_.]|$)",
            re.IGNORECASE,
        ),
    ),
    ("public_api", re.compile(r"(^|/)(openapi\.(ya?ml|json)|api/endpoints/[^/]+)$")),
    (
        "ci_release",
        re.compile(
            r"(^|/)(\.github/workflows/|\.gitlab-ci\.yml$|\.gitlab/ci/|"
            r"release[^/]*\.(ya?ml|sh|py)$|Dockerfile[^/]*$)"
        ),
    ),
    (
        "secrets",
        re.compile(
            r"(^|/|_)(secrets?|credentials?|vault|kms|keys?|encryption|crypto)"
            r"([/_.]|$)",
            re.IGNORECASE,
        ),
    ),
)


class TrackerWritePolicy(BaseModel):
    """What the run may do to the tracker. Writes ship in #2021.

    Tracker writes start off. A follow-up issue or a reopen request is a
    proposal in the report, and the reader's click on Open issue or Reopen
    is the approval. Creating follow-ups automatically under the tool
    policy is a per-account setting (``create_follow_up: "allow"``) that an
    account enables only after the report's proposals have proven precise
    on that account. Closing is never done by the report, so ``close`` only
    accepts ``deny``.
    """

    #: ``deny`` keeps creation off: the follow-up stays a proposal in the
    #: report until the account enables automatic creation.
    create_follow_up: WriteRule = "deny"
    reopen: WriteRule = "approval"
    close: Literal["deny"] = "deny"
    follow_up_label: str = "preloop:proposed"


class DeliveryReportPolicy(BaseModel):
    """The report policy, per account and overridable per repository.

    Two kinds of report share this policy. The deterministic report (what
    shipped, closed and aged, the delta and the deterministic risk flags)
    makes no model call and costs nothing. The assessed report adds a model
    pass, only on changes that have neither a completed merge audit nor a
    change brief, and only under the per-run and monthly budgets. The
    budget defaults come from a measured run on a heavy week. The per-run
    ceiling is applied as the flow's ``agent_config.limits.max_usd`` so the
    gateway enforces it like any other execution limit.
    """

    cadence: Literal["weekly", "daily", "manual"] = "weekly"
    weekday: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "mon"
    at: str = "07:00"
    timezone: str = "UTC"
    #: Project identifiers in scope; empty means every linked project.
    projects: List[str] = Field(default_factory=list)
    #: ``deterministic`` never calls a model; ``assessed`` adds the model
    #: pass within the budgets below.
    report_mode: ReportMode = "assessed"
    per_run_usd: float = Field(default=5.0, gt=0)
    monthly_usd: float = Field(default=25.0, gt=0)
    #: What to drop first when the budget runs short. The shipped list is
    #: never dropped.
    drop_order: List[DropStep] = Field(default_factory=lambda: list(DEFAULT_DROP_ORDER))
    max_assessed_changes: int = Field(default=25, ge=0)
    pr_ready_days: int = Field(default=7, ge=1)
    issue_inactive_days: int = Field(default=14, ge=1)
    #: Changed lines in rendered frontend files below which a UI change
    #: without a screenshot is not flagged (style and view files always are).
    ui_screenshot_min_lines: int = Field(default=40, ge=0)
    criteria_source: Literal["triage_assessment", "description"] = "triage_assessment"
    tracker_writes: TrackerWritePolicy = Field(default_factory=TrackerWritePolicy)

    def assessments_allowed(self, spent_this_month_usd: Optional[float]) -> bool:
        """Whether this run may pay for a model pass.

        False in deterministic mode, or when the month's spend has reached
        the monthly budget. Unknown spend does not block: the per-run
        ceiling still caps the run.
        """
        if self.report_mode != "assessed":
            return False
        return spent_this_month_usd is None or spent_this_month_usd < self.monthly_usd

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
        lines = 0
        for key in ("additions", "deletions"):
            value = f.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                lines += value
        patch = str(f.get("patch") or f.get("diff") or "")
        if not lines and patch:
            lines = sum(
                1
                for ln in patch.splitlines()
                if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
            )
        normalized.append({"filename": str(name), "patch": patch, "lines": lines})
    return normalized


def _file_count(pr: Mapping[str, Any]) -> Optional[int]:
    """The PR's own changed-file count, when the response states one."""
    changes = pr.get("changes")
    for source in (changes if isinstance(changes, Mapping) else {}, pr):
        for key in ("files_changed", "changed_files_count", "changed_files"):
            value = source.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def is_merge_revision(patch: str) -> bool:
    """Whether a migration diff adds an Alembic merge revision.

    A merge revision joins two heads: its ``down_revision`` is a tuple, and
    its ``downgrade()`` is a no-op by design, so it never raises the
    missing-downgrade flag.
    """
    return bool(_MERGE_REVISION_RE.search(patch))


def path_class(name: str) -> Optional[str]:
    """The sensitive path class a file belongs to, or None."""
    for label, pattern in PATH_CLASSES:
        if pattern.search(name):
            return label
    return None


def detect_risk_flags(
    pr: Mapping[str, Any], *, policy: Optional[DeliveryReportPolicy] = None
) -> List[Dict[str, Any]]:
    """Risk flags readable from the diff surface alone, each with evidence.

    Deterministic and free, so they run before any model pass and on every
    report. A flag here always names the files that raised it:

    * ``sensitive_path``: auth and session code, migrations, billing and
      payments, public API surface and OpenAPI, CI and release workflows,
      secrets handling. Evidence lines name the class.
    * ``hot_path_without_tests``: any non-test, non-doc source change with
      no test file in the diff; the model narrows it with context.
    * ``public_api_without_version_note``: API surface changed with no
      changelog or version file.
    * ``ui_without_screenshot``: only when a style or view file changes, or
      the changed frontend lines exceed the policy threshold, and the PR
      body has no image. A logic-only frontend touch does not raise it.
    * ``migration_without_downgrade``: a migration whose ``downgrade()`` is
      missing or empty, except an Alembic merge revision.
    * ``file_list_incomplete``: the forge reported more files than the
      response lists, so every other flag may be missing something.
    """
    policy = policy or DEFAULT_POLICY
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
    sensitive = [f"{label}: {n}" for n in names if (label := path_class(n))]
    if sensitive:
        flags.append({"flag": RISK_SENSITIVE_PATH, "evidence": sensitive})
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
    ui_files = [
        f for f in files if f["filename"] in code and _UI_PATH_RE.search(f["filename"])
    ]
    rendered = [
        str(f["filename"])
        for f in ui_files
        if _STYLE_PATH_RE.search(f["filename"]) or _VIEW_PATH_RE.search(f["filename"])
    ]
    frontend_lines = sum(
        int(f["lines"]) for f in ui_files if _FRONTEND_PATH_RE.search(f["filename"])
    )
    ui_changed = bool(rendered) or frontend_lines > policy.ui_screenshot_min_lines
    if ui_changed and not _SCREENSHOT_RE.search(str(pr.get("description") or "")):
        evidence = [f"{n} changed; no screenshot in the PR body" for n in rendered]
        if not evidence:
            evidence = [
                f"{frontend_lines} frontend lines changed (threshold "
                f"{policy.ui_screenshot_min_lines}); no screenshot in the PR body"
            ]
        flags.append({"flag": RISK_UI_WITHOUT_SCREENSHOT, "evidence": evidence})
    for f in files:
        name = str(f["filename"])
        if not _MIGRATION_PATH_RE.search(name):
            continue
        patch = str(f.get("patch") or "")
        if not patch:
            # No per-file diff (Bitbucket): cannot tell, so no flag.
            continue
        if is_merge_revision(patch):
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
    stated = _file_count(pr)
    if stated is not None and stated > len(files):
        flags.append(
            {
                "flag": RISK_FILE_LIST_INCOMPLETE,
                "evidence": [
                    f"the PR changes {stated} files; {len(files)} were listed. "
                    "Read the paginated files endpoint before trusting the flags."
                ],
            }
        )
    return flags


def metadata_only_reason(pr: Mapping[str, Any]) -> Optional[str]:
    """``dependabot`` or ``docs_only`` when the PR needs no model call.

    Dependency bumps are reported with their CI result; a change that only
    touches docs or the README is reported in one line. Both come from
    metadata alone.
    """
    if _DEPENDABOT_RE.match(str(pr.get("author") or "").strip()):
        return "dependabot"
    names = [str(f["filename"]) for f in _changed_files(pr)]
    if names and all(_DOC_PATH_RE.search(n) for n in names):
        return "docs_only"
    return None


# ---------------------------------------------------------------------------
# Change briefs and review summaries in PR bodies
# ---------------------------------------------------------------------------

BRIEF_START = "<!-- PRELOOP_BRIEF -->"
BRIEF_END = "<!-- /PRELOOP_BRIEF -->"
SUMMARY_START = "<!-- PRELOOP_SUMMARY -->"
SUMMARY_END = "<!-- /PRELOOP_SUMMARY -->"

#: Brief section headings and the keys they parse into.
BRIEF_SECTIONS = {
    "what changed": "what_changed",
    "why": "why",
    "assumptions": "assumptions",
    "not verified": "not_verified",
    "rollback": "rollback",
    "decisions": "decisions",
}
_BRIEF_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*#*\s*$")
_SUMMARY_LABEL_RE = re.compile(r"^\*\*([^*]+)\*\*\s*:?\s*(.*)$")
_NONE_RE = re.compile(r"^(none|n/?a|-)\.?$", re.IGNORECASE)


def _block(text: Optional[str], start: str, end: str) -> Optional[str]:
    """Text between a start marker and its end marker; None when absent.

    A start marker without an end runs to the end of the body, so a
    truncated body still yields what it has.
    """
    if not text or start not in text:
        return None
    begin = text.rindex(start) + len(start)
    stop = text.find(end, begin)
    return text[begin : stop if stop != -1 else None]


def _bullets(lines: Sequence[str]) -> List[str]:
    """Bullet texts from lines; a "none" bullet means an empty list."""
    out: List[str] = []
    for line in lines:
        match = _BULLET_RE.match(line)
        text = match.group(1).strip() if match else ""
        if text and not _NONE_RE.match(text):
            out.append(text)
    return out


def parse_change_brief(body: Optional[str]) -> Optional[Dict[str, List[str]]]:
    """The ``PRELOOP_BRIEF`` block of a PR body, one list per section.

    Returns None when the body has no brief. A missing section is an empty
    list, an unknown heading is ignored, and a "none" bullet counts as no
    bullet. The brief is the author's claim, not an assessment: its "Not
    verified" bullets become evidence gaps in the report.
    """
    block = _block(body, BRIEF_START, BRIEF_END)
    if block is None:
        return None
    sections: Dict[str, List[str]] = {key: [] for key in BRIEF_SECTIONS.values()}
    current: Optional[str] = None
    pending: List[str] = []

    def flush() -> None:
        if current is not None:
            sections[current] = _bullets(pending)

    for line in block.splitlines():
        heading = _BRIEF_HEADING_RE.match(line)
        if heading:
            flush()
            current = BRIEF_SECTIONS.get(heading.group(1).strip().lower().rstrip(":"))
            pending = []
        elif current is not None:
            pending.append(line)
    flush()
    return sections


def parse_review_summary(body: Optional[str]) -> Optional[Dict[str, Any]]:
    """The reviewer's ``PRELOOP_SUMMARY`` block of a PR body.

    Returns ``{"risk_level", "not_verified", "must_know"}`` or None when the
    body has no summary. Lines may carry the ``> `` quote prefix of the
    note callout. A missing label is an empty list (or None for the risk
    level).
    """
    block = _block(body, SUMMARY_START, SUMMARY_END)
    if block is None:
        return None
    labelled: Dict[str, List[str]] = {}
    current: Optional[str] = None
    risk_level: Optional[str] = None
    for raw in block.splitlines():
        line = re.sub(r"^\s*>\s?", "", raw).strip()
        label = _SUMMARY_LABEL_RE.match(line)
        if label:
            name = label.group(1).strip().strip("[]").rstrip(":").lower()
            if name in ("low", "medium", "high", "critical") and risk_level is None:
                risk_level = name
                current = None
                continue
            current = name
            labelled.setdefault(current, [])
            rest = label.group(2).strip()
            if rest:
                labelled[current].append(f"- {rest}")
        elif current is not None and line:
            labelled[current].append(line)
    return {
        "risk_level": risk_level,
        "not_verified": _bullets(labelled.get("not verified", [])),
        "must_know": _bullets(labelled.get("must know", []))[:3],
    }


def evidence_gaps(pr: Mapping[str, Any]) -> List[str]:
    """The "Not verified" bullets of a PR's brief and review summary."""
    body = str(pr.get("description") or "")
    gaps: List[str] = []
    brief = parse_change_brief(body)
    summary = parse_review_summary(body)
    for item in [
        *((brief or {}).get("not_verified") or []),
        *((summary or {}).get("not_verified") or []),
    ]:
        if item not in gaps:
            gaps.append(item)
    return gaps


def needs_model_risk_pass(
    pr: Mapping[str, Any], flags: Optional[Sequence[Mapping[str, Any]]] = None
) -> bool:
    """Whether the report should pay for a model risk pass on this PR.

    Metadata-only PRs never need one. A PR with a change brief or a review
    summary skips it unless a deterministic flag fired; a PR with neither
    always gets it.
    """
    if metadata_only_reason(pr):
        return False
    if flags is None:
        flags = detect_risk_flags(pr)
    body = str(pr.get("description") or "")
    described = parse_change_brief(body) is not None or (
        parse_review_summary(body) is not None
    )
    return not described or bool(flags)


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
    for key in ("source", "revision", "audit_execution_id"):
        value = purpose.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
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
# Completed merge audits, cached assessments and issue comments
# ---------------------------------------------------------------------------

_AUDIT_MET = {"complete": True, "gap": False, "unknown": None}
_ISSUE_URL_RE = re.compile(r"/([^/]+/[^/]+?)(?:/-)?/issues/(\d+)")


def _issue_ref_from_url(url: Any) -> Optional[str]:
    match = _ISSUE_URL_RE.search(str(url or ""))
    return f"{match.group(1)}#{match.group(2)}" if match else None


def merge_audit_record(execution: Any) -> Optional[Dict[str, Any]]:
    """One completed merge audit (preset 013) as a report-ready record.

    Reads the execution's lifecycle envelope (the issue, the acceptance
    contract and the closing merge SHA the controller pinned) and its
    stored result. Returns None unless the run checked out exactly that
    merge SHA, so a record is always pinned to the issue's closing
    revision. Deployment audits are not merge audits and are skipped.
    """
    details = getattr(execution, "trigger_event_details", None)
    payload = details.get("payload") if isinstance(details, Mapping) else None
    envelope = payload.get("lifecycle") if isinstance(payload, Mapping) else None
    result = getattr(execution, "result", None)
    if not isinstance(envelope, Mapping) or not isinstance(result, Mapping):
        return None
    if envelope.get("audit_kind", "merge_audit") != "merge_audit":
        return None
    merge_sha = str(envelope.get("merge_sha") or "")
    if not merge_sha or result.get("checked_out_sha") != merge_sha:
        return None
    if result.get("status") != "success":
        return None
    contract = envelope.get("contract")
    contract_criteria = (
        contract.get("criteria") if isinstance(contract, Mapping) else None
    )
    outcomes: Dict[str, str] = {}
    for item in contract_criteria if isinstance(contract_criteria, list) else []:
        if isinstance(item, Mapping) and item.get("id"):
            outcomes[str(item["id"])] = str(item.get("outcome") or item["id"])
    criteria: List[Dict[str, Any]] = []
    raw_criteria = result.get("criteria")
    for item in raw_criteria if isinstance(raw_criteria, list) else []:
        if not isinstance(item, Mapping):
            continue
        cid = str(item.get("criterion_id") or "")
        evidence = [
            *_strs(item.get("code")),
            *_strs(item.get("tests")),
            *_strs(item.get("observations")),
        ]
        criteria.append(
            {
                "text": outcomes.get(cid, cid),
                "met": _AUDIT_MET.get(str(item.get("verdict")), None),
                "evidence": evidence,
            }
        )
    url = envelope.get("issue_url")
    started = getattr(execution, "end_time", None) or getattr(
        execution, "start_time", None
    )
    return {
        "issue_ref": _issue_ref_from_url(url),
        "issue_url": url,
        "issue_title": envelope.get("issue_title"),
        "merge_sha": merge_sha,
        "execution_id": str(getattr(execution, "id", "") or "") or None,
        "assessed_at": started.isoformat() if isinstance(started, datetime) else None,
        "purpose": {"criteria": criteria},
    }


def merge_audits_document(
    executions: Iterable[Any], *, max_bytes: Optional[int] = None
) -> Dict[str, Any]:
    """The ``previous/merge_audits.json`` document the run receives.

    Newest audit per issue wins (``executions`` arrive newest first). When
    ``max_bytes`` is set and the document does not fit, the oldest records
    are dropped and ``truncated`` says so; the platform still applies every
    audit when it finalizes the report.
    """
    import json

    records: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for execution in executions:
        record = merge_audit_record(execution)
        if record is None:
            continue
        key = str(record.get("issue_ref") or record.get("issue_url"))
        if key in seen:
            continue
        seen.add(key)
        records.append(record)
    document: Dict[str, Any] = {
        "schema": MERGE_AUDITS_SCHEMA,
        "audits": records,
        "truncated": False,
    }
    if max_bytes is not None:
        while records and len(json.dumps(document, default=str)) > max_bytes:
            records.pop()
            document["truncated"] = True
    return document


def _audit_for(
    item: Mapping[str, Any], audits: Sequence[Mapping[str, Any]]
) -> Optional[Mapping[str, Any]]:
    """The audit for a closed issue at its closing revision, if any."""
    ref = str(item.get("ref") or "")
    url = item.get("url")
    closing = str(item.get("closing_sha") or "")
    for audit in audits:
        if not (
            (ref and audit.get("issue_ref") == ref)
            or (url and audit.get("issue_url") == url)
        ):
            continue
        if closing and audit.get("merge_sha") != closing:
            # Audited at another revision: the closing merge changed since.
            continue
        return audit
    return None


def _audited_purpose(audit: Mapping[str, Any]) -> Dict[str, Any]:
    purpose = dict(audit.get("purpose") or {})
    purpose["source"] = SOURCE_AUDIT
    purpose["revision"] = audit.get("merge_sha")
    purpose["audit_execution_id"] = audit.get("execution_id")
    return purpose


def assessment_cache_key(issue_updated_at: Any, closing_head_sha: Any) -> Optional[str]:
    """Cache key of a closed issue's assessment; None when either part is unknown.

    An issue whose text and closing PR have not changed since the previous
    report keeps that report's assessment and is not assessed again.
    """
    updated = _parse_time(issue_updated_at)
    sha = str(closing_head_sha or "").strip()
    if updated is None or not sha:
        return None
    return f"{updated.isoformat()}|{sha}"


def comments_after_last_criteria_edit(
    issue: Mapping[str, Any], *, source: str = "triage_assessment"
) -> List[Dict[str, Any]]:
    """The comments a criterion check must read, oldest first.

    Comments that restate the criteria (an "Acceptance" heading or the
    triage section) are criterion edits; only the comments after the last
    one count, because earlier measurements describe earlier criteria. An
    issue without criteria needs no comments at all.
    """
    if not extract_criteria(issue.get("description"), source=source):
        return []
    raw = issue.get("comments")
    comments = (
        [c for c in raw if isinstance(c, Mapping)] if isinstance(raw, list) else []
    )
    comments.sort(
        key=lambda c: _parse_time(c.get("created_at"))
        or datetime.min.replace(tzinfo=timezone.utc)
    )
    start = 0
    for index, comment in enumerate(comments):
        body = str(comment.get("body") or "")
        if _TRIAGE_START in body or _criteria_under_heading(body):
            start = index + 1
    return [dict(c) for c in comments[start:]]


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
    merge_audits: Sequence[Mapping[str, Any]] = (),
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
        previous: The previous report, for the delta and the per-issue cache.
        merge_audits: Records from :func:`merge_audit_record`; an audited
            closed issue takes the audit's result.
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
        metadata_only = metadata_only_reason(pr)
        # Metadata-only changes are never sent to the model, so an
        # assessment for one is not read either.
        agent = {} if metadata_only else (assessments.get(ref) or {})
        purpose = dict(agent.get("purpose") or {})
        if "criteria" not in purpose:
            purpose["criteria"] = criteria
        deterministic = detect_risk_flags(pr, policy=policy)
        risk = deterministic + list(agent.get("risk_flags") or [])
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
                "evidence_gaps": evidence_gaps(pr),
                "metadata_only": metadata_only,
                "needs_model_risk_pass": needs_model_risk_pass(pr, deterministic),
                "head_sha": pr.get("head_sha"),
                "merge_sha": pr.get("merge_commit_sha"),
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
        closing = [c for c in shipped if c["ref"] in prs]
        closing_pr = closing[-1] if closing else {}
        closed.append(
            {
                "ref": key,
                "title": issue.get("title"),
                "url": issue.get("url"),
                "updated_at": issue.get("updated_at"),
                "closing_sha": closing_pr.get("merge_sha"),
                "closing_head_sha": closing_pr.get("head_sha"),
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
    return finalize_delivery_report(
        report, previous=previous, merge_audits=merge_audits
    )


def _needs_you(report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for request in report.get("reopen_requests") or []:
        items.append(
            {
                "kind": "reopen_request",
                "ref": request.get("ref"),
                "title": request.get("title"),
                "url": request.get("url"),
                "revision": request.get("revision"),
                "reason": request.get("reason"),
            }
        )
    for issue in report.get("closed_issues") or []:
        purpose = issue.get("purpose") or {}
        if purpose.get("verdict") in (PURPOSE_PARTIAL, PURPOSE_UNVERIFIED):
            unmet = [
                c["text"] for c in purpose.get("criteria") or [] if c["met"] is not True
            ]
            items.append(
                {
                    "kind": "closed_unverified",
                    "verdict": purpose.get("verdict"),
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


def _assessed_source(purpose: Mapping[str, Any], fallback: str) -> str:
    """``report`` when the run judged at least one criterion, else ``fallback``."""
    if any(c.get("met") is not None for c in purpose.get("criteria") or []):
        return SOURCE_REPORT
    return fallback


def _cached_purpose(
    issue: Mapping[str, Any], previous: Optional[Mapping[str, Any]]
) -> Optional[Dict[str, Any]]:
    """The previous report's assessment of an unchanged closed issue."""
    key = issue.get("cache_key")
    if not key or not isinstance(previous, Mapping):
        return None
    if previous.get("schema") != REPORT_SCHEMA:
        return None
    for old in previous.get("closed_issues") or []:
        if not isinstance(old, Mapping) or old.get("ref") != issue.get("ref"):
            continue
        if old.get("cache_key") != key:
            return None
        purpose = old.get("purpose")
        if not isinstance(purpose, Mapping):
            return None
        if purpose.get("source") not in (SOURCE_AUDIT, SOURCE_REPORT, SOURCE_CACHE):
            return None
        cached = dict(purpose)
        cached["source"] = SOURCE_CACHE
        return cached
    return None


def normalize_reopen_requests(raw: Any) -> List[Dict[str, Any]]:
    """Reopen proposals that name the issue, the assessed revision and evidence.

    A reopen request is a proposal in the report, never a tracker write. It
    must name the revision it was assessed at so it can go stale when new
    commits land, and it must cite the evidence (for example the issue's
    last comment measuring a criterion as not met). Anything else is
    dropped.
    """
    kept: List[Dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        ref = str(item.get("ref") or "").strip()
        revision = str(item.get("revision") or "").strip()
        evidence = _evidence(item.get("evidence"))
        if not (ref and revision and evidence):
            continue
        kept.append(
            {
                "ref": ref,
                "title": item.get("title"),
                "url": item.get("url"),
                "revision": revision,
                "reason": str(item.get("reason") or "").strip() or None,
                "evidence": evidence,
            }
        )
    return kept


def compute_summary(report: Mapping[str, Any]) -> Dict[str, Any]:
    """The summary strip, computed from the structured sections only.

    Never read from model prose. The denominators stay separate numbers on
    purpose; there is no combined score:

    * ``linkage``: shipped changes with a linked issue, of all shipped.
    * ``criteria_availability``: closed issues that state criteria, of all
      closed issues.
    * ``evidence_availability``: criteria of closed issues with checkable
      evidence cited, of all those criteria.
    * ``closed_verdicts``: closed issues per verdict. ``unverified`` counts
      evidence gaps, not failures.
    """
    shipped = [c for c in report.get("shipped") or [] if isinstance(c, Mapping)]
    closed = [i for i in report.get("closed_issues") or [] if isinstance(i, Mapping)]
    criteria = [
        c for i in closed for c in (i.get("purpose") or {}).get("criteria") or []
    ]
    skipped = (report.get("spend") or {}).get("skipped_for_budget") or []
    return {
        "shipped": len(shipped),
        "closed": len(closed),
        "closed_unverified": sum(
            1
            for i in closed
            if (i.get("purpose") or {}).get("verdict")
            in (PURPOSE_PARTIAL, PURPOSE_UNVERIFIED)
        ),
        "risk_flagged": sum(1 for c in shipped if c.get("risk_flags")),
        "open_past_window": len(report.get("open_past_window") or []),
        "follow_ups_proposed": len(report.get("follow_ups") or []),
        "reopen_requests": len(report.get("reopen_requests") or []),
        "evidence_gaps": sum(len(c.get("evidence_gaps") or []) for c in shipped),
        "linkage": {
            "linked": sum(1 for c in shipped if c.get("issues")),
            "total": len(shipped),
        },
        "criteria_availability": {
            "with_criteria": sum(
                1 for i in closed if (i.get("purpose") or {}).get("criteria")
            ),
            "total": len(closed),
        },
        "evidence_availability": {
            "with_evidence": sum(1 for c in criteria if c.get("evidence")),
            "total": len(criteria),
        },
        "verdicts": {
            v: sum(1 for c in shipped if (c.get("purpose") or {}).get("verdict") == v)
            for v in PURPOSE_VERDICTS
        },
        "closed_verdicts": {
            v: sum(1 for i in closed if (i.get("purpose") or {}).get("verdict") == v)
            for v in PURPOSE_VERDICTS
        },
        "assessment_sources": {
            source: sum(
                1 for i in closed if (i.get("purpose") or {}).get("source") == source
            )
            for source in ASSESSMENT_SOURCES
        },
        "assessments_skipped_for_budget": bool(skipped),
    }


def finalize_delivery_report(
    report: Any,
    *,
    previous: Optional[Mapping[str, Any]] = None,
    merge_audits: Sequence[Mapping[str, Any]] = (),
    cost_usd: Optional[float] = None,
    total_tokens: Optional[int] = None,
    limit_usd: Optional[float] = None,
) -> Dict[str, Any]:
    """The platform pass over a report: honesty rules, summary, delta, spend.

    A closed issue with a completed merge audit at its closing revision
    takes the audit's result (source ``audit``), whatever the run wrote; an
    unchanged closed issue keeps the previous report's assessment (source
    ``cache``). Safe on a malformed or error result: a non-dict becomes an
    error report; an ``error`` report keeps its reason and still records
    spend.
    """
    if not isinstance(report, Mapping):
        report = {"status": "error", "reason": "result was not a report object"}
    out: Dict[str, Any] = dict(report)
    out["schema"] = REPORT_SCHEMA
    for key in ("shipped", "closed_issues", "open_past_window", "follow_ups"):
        if not isinstance(out.get(key), list):
            out[key] = []
    out["open_questions"] = _questions(out.get("open_questions"))
    out["reopen_requests"] = normalize_reopen_requests(out.get("reopen_requests"))
    audits = [a for a in merge_audits if isinstance(a, Mapping)]

    shipped = []
    for change in out["shipped"]:
        if not isinstance(change, Mapping):
            continue
        change = dict(change)
        project = str(change.get("project") or "")
        # "#12" in a PR body means an issue in the PR's own project.
        linked = [
            f"{project}{ref}" if ref.startswith("#") and project else ref
            for ref in _strs(change.get("issues"))
        ]
        audit = _audit_for({"ref": linked[0]}, audits) if len(linked) == 1 else None
        metadata_only = change.get("metadata_only")
        if audit is not None:
            change["purpose"] = normalize_purpose(_audited_purpose(audit))
        else:
            purpose = normalize_purpose(change.get("purpose"))
            fallback = SOURCE_METADATA if metadata_only else SOURCE_NONE
            purpose["source"] = _assessed_source(purpose, fallback)
            change["purpose"] = purpose
        change["risk_flags"] = normalize_risk_flags(change.get("risk_flags"))
        change["evidence_gaps"] = _strs(change.get("evidence_gaps"))
        shipped.append(change)
    out["shipped"] = shipped
    closed = []
    for issue in out["closed_issues"]:
        if not isinstance(issue, Mapping):
            continue
        issue = dict(issue)
        issue["cache_key"] = issue.get("cache_key") or assessment_cache_key(
            issue.get("updated_at"), issue.get("closing_head_sha")
        )
        audit = _audit_for(issue, audits)
        cached = None if audit is not None else _cached_purpose(issue, previous)
        if audit is not None:
            issue["purpose"] = normalize_purpose(_audited_purpose(audit))
        elif cached is not None:
            issue["purpose"] = normalize_purpose(cached)
        else:
            purpose = normalize_purpose(issue.get("purpose"))
            purpose["source"] = _assessed_source(purpose, SOURCE_NONE)
            issue["purpose"] = purpose
        closed.append(issue)
    out["closed_issues"] = closed

    out["needs_you"] = _needs_you(out)
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
    out["summary"] = compute_summary(out)
    out["report_markdown"] = render_markdown(out)
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_VERDICT_LABELS = {
    PURPOSE_COVERED: "covered",
    PURPOSE_PARTIAL: "partial",
    PURPOSE_UNVERIFIED: "unverified (evidence gap)",
    PURPOSE_NO_CRITERIA: "no criteria stated",
}
_SOURCE_LABELS = {
    SOURCE_AUDIT: "from the merge audit",
    SOURCE_REPORT: "assessed by this report",
    SOURCE_CACHE: "unchanged since the previous report",
    SOURCE_METADATA: "reported from metadata",
    SOURCE_NONE: "not assessed",
}


def _ratio(part: Any, whole: Any) -> str:
    return f"{part or 0} of {whole or 0}"


def _summary_lines(summary: Mapping[str, Any]) -> List[str]:
    """The summary strip: counts, then each denominator on its own line."""
    linkage = summary.get("linkage") or {}
    criteria = summary.get("criteria_availability") or {}
    evidence = summary.get("evidence_availability") or {}
    verdicts = summary.get("closed_verdicts") or {}
    lines = [
        f"Shipped {summary.get('shipped', 0)}, closed {summary.get('closed', 0)}, "
        f"{summary.get('risk_flagged', 0)} with risk flags, "
        f"{summary.get('open_past_window', 0)} open past the window.",
        "",
        f"- Linkage: {_ratio(linkage.get('linked'), linkage.get('total'))} "
        "shipped changes link an issue.",
        f"- Criteria available: "
        f"{_ratio(criteria.get('with_criteria'), criteria.get('total'))} "
        "closed issues state acceptance criteria.",
        f"- Evidence available: "
        f"{_ratio(evidence.get('with_evidence'), evidence.get('total'))} "
        "of their criteria cite checkable evidence.",
        f"- Closed issues: {verdicts.get(PURPOSE_COVERED, 0)} covered, "
        f"{verdicts.get(PURPOSE_PARTIAL, 0)} partial, "
        f"{verdicts.get(PURPOSE_UNVERIFIED, 0)} unverified, "
        f"{verdicts.get(PURPOSE_NO_CRITERIA, 0)} with no criteria. Unverified "
        "means an evidence gap: the available evidence could not decide the "
        "criteria. It is not a failure.",
    ]
    if summary.get("assessments_skipped_for_budget"):
        lines += ["", "Assessments were skipped for budget; see the spend section."]
    return lines


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
        *_summary_lines(summary),
    ]
    if report.get("status") == "error":
        lines += ["", f"The run did not complete: {report.get('reason') or 'unknown'}."]

    lines += ["", "## Needs you", ""]
    needs = report.get("needs_you") or []
    for item in needs:
        if item["kind"] == "reopen_request":
            reason = f": {item['reason']}" if item.get("reason") else ""
            lines.append(
                f"- Proposed reopen (assessed at {item.get('revision')}): "
                f"{_link(item)}{reason}"
            )
        elif item["kind"] == "closed_unverified":
            open_criteria = (
                "; ".join(item.get("unmet_criteria") or []) or "criteria unchecked"
            )
            label = (
                "Closed, partly covered"
                if item.get("verdict") == PURPOSE_PARTIAL
                else "Closed with an evidence gap"
            )
            lines.append(f"- {label}: {_link(item)}. Not shown met: {open_criteria}")
        elif item["kind"] == "risk":
            lines.append(f"- Risk: {_link(item)} ({', '.join(item['flags'])})")
        else:
            lines.append(f"- Aged {item.get('age_days')} days: {_link(item)}")
    if not needs:
        lines.append("Nothing needs you this period.")

    lines += ["", "## Shipped", ""]
    for change in report.get("shipped") or []:
        purpose = change["purpose"]
        if change.get("metadata_only"):
            lines.append(
                f"- {_link(change)} ({change.get('area') or 'unknown'}): "
                f"{str(change['metadata_only']).replace('_', ' ')}, "
                "reported from metadata"
            )
            continue
        source = _SOURCE_LABELS.get(str(purpose.get("source")), "")
        lines.append(
            f"- {_link(change)} ({change.get('area') or 'unknown'}): purpose "
            f"{_VERDICT_LABELS[purpose['verdict']]}" + (f", {source}" if source else "")
        )
        for c in purpose["criteria"]:
            state = {True: "met", False: "not met", None: "evidence gap"}[c["met"]]
            cite = f" ({'; '.join(c['evidence'])})" if c["evidence"] else ""
            lines.append(f"  - {c['text']}: {state}{cite}")
        for flag in change["risk_flags"]:
            lines.append(f"  - Risk {flag['flag']}: {'; '.join(flag['evidence'])}")
        for gap in change.get("evidence_gaps") or []:
            lines.append(f"  - Evidence gap (stated in the PR): {gap}")
    if not report.get("shipped"):
        lines.append("Nothing merged in the window.")

    lines += ["", "## Closed issues", ""]
    for issue in report.get("closed_issues") or []:
        verdict = _VERDICT_LABELS[issue["purpose"]["verdict"]]
        source = _SOURCE_LABELS.get(str(issue["purpose"].get("source")), "")
        lines.append(f"- {_link(issue)}: {verdict}" + (f", {source}" if source else ""))
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


def audit_window_start(window: Any, *, now: Optional[datetime] = None) -> datetime:
    """Earliest audit start that can belong to this window, naive UTC.

    The window's ``from``; a run with no window covers the seven days
    before now, the same default the prompt states.
    """
    start = _parse_time(window.get("from")) if isinstance(window, Mapping) else None
    if start is None:
        start = (now or datetime.now(timezone.utc)) - timedelta(days=7)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def load_merge_audits(
    db: Any, *, account_id: Any, window: Any, max_bytes: Optional[int] = None
) -> Dict[str, Any]:
    """Completed merge audits of this account for the window, as a document.

    Read through the CRUD layer; see :func:`merge_audits_document`.
    """
    from preloop.models.crud import crud_flow_execution

    if not account_id:
        return merge_audits_document([])
    executions = crud_flow_execution.completed_merge_audits(
        db, account_id=str(account_id), since=audit_window_start(window)
    )
    return merge_audits_document(executions, max_bytes=max_bytes)


def with_merge_audits_seed(
    trigger_event_data: Optional[Dict[str, Any]], document: Mapping[str, Any]
) -> Optional[Dict[str, Any]]:
    """A copy of the trigger data that also seeds ``previous/merge_audits.json``.

    The audits travel to the workspace as one more ``workspace_files`` seed,
    the transport the previous report's neighbours already use. The input
    is not modified. A seed the caller already declared at that path wins,
    and a seed that would break the workspace caps is left out (the
    platform still applies every audit when it finalizes the report).
    """
    import base64
    import copy
    import json

    from preloop.utils.workspace_seed import (
        WORKSPACE_FILES_KEY,
        WorkspaceSeedError,
        parse_workspace_files,
        workspace_seed_payload,
    )

    data: Dict[str, Any] = copy.deepcopy(trigger_event_data or {})
    try:
        target = workspace_seed_payload(data)
    except WorkspaceSeedError:
        return trigger_event_data
    if target is None:
        target = data.setdefault("payload", {})
    seeds = target.get(WORKSPACE_FILES_KEY)
    seeds = list(seeds) if isinstance(seeds, list) else []
    if any(
        isinstance(seed, Mapping) and seed.get("path") == MERGE_AUDITS_WORKSPACE_PATH
        for seed in seeds
    ):
        return trigger_event_data
    content = json.dumps(document, default=str).encode("utf-8")
    seeds.append(
        {
            "path": MERGE_AUDITS_WORKSPACE_PATH,
            "content_base64": base64.b64encode(content).decode("ascii"),
        }
    )
    target[WORKSPACE_FILES_KEY] = seeds
    try:
        parse_workspace_files(target)
    except WorkspaceSeedError:
        return trigger_event_data
    return data


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
    audits: List[Dict[str, Any]] = []
    if account_id:
        try:
            window = result.get("window") if isinstance(result, Mapping) else None
            audits = load_merge_audits(db, account_id=account_id, window=window)[
                "audits"
            ]
        except Exception:  # noqa: BLE001 - without audits the run's own pass stands
            log.warning("Could not read merge audits for the report", exc_info=True)
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
        merge_audits=audits,
        cost_usd=cost,
        total_tokens=tokens,
        limit_usd=limits.max_usd,
    )
