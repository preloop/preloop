"""Bind a flow execution to the pull request it opened.

Issue-implementation flows persist the opened PR URL on
``FlowExecution.result`` so a later ``comment_created`` on that PR can start
a new execution of the same flow with ``_resume`` metadata. The resume
metadata also carries the prior execution's native CLI session (see
``preloop.agents.cli_session``) so the new run can invoke the CLI resume flag
instead of starting a cold agent.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from preloop.models.crud import crud_flow_execution
from preloop.models.models.flow_execution import FlowExecution
from preloop.utils.pr_metadata import read_result_metadata

logger = logging.getLogger(__name__)

_RESUME_LOOKBACK = 50
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})

# Single line the container wrapper prints after creating (or finding) the PR:
#   PRELOOP_PR_OPENED {"url": "...", "branch": "...", "provider": "github"}
PR_OPENED_MARKER = "PRELOOP_PR_OPENED"
MAX_PUBLISHED_PRS = 32

# One line per non-trivial choice the agent made, printed by the agent itself:
#   PRELOOP_DECISION {"summary": "...", "reason": "...", "alternatives": ["..."]}
# These are the agent's own claims, kept for reports to cite, never trusted
# for control flow.
DECISION_MARKER = "PRELOOP_DECISION"
MAX_DECISIONS = 50
MAX_DECISION_TEXT = 1000
MAX_DECISION_ALTERNATIVES = 10
# Template filler such as "...", "<why>" or "___": an echoed prompt, issue or
# PR body that shows the marker's shape must not record a decision.
_DECISION_PLACEHOLDER_RE = re.compile(r"^(?:[\s.\u2026_\-]*|<[^<>]*>)$")

# Change brief the implementer writes into its PR body, between these markers:
#   <!-- PRELOOP_BRIEF --> ### What changed ... <!-- /PRELOOP_BRIEF -->
# Like decisions it is the author's claim, stored for reports to cite.
BRIEF_START = "<!-- PRELOOP_BRIEF -->"
BRIEF_END = "<!-- /PRELOOP_BRIEF -->"
BRIEF_SECTIONS = {
    "what changed": "what_changed",
    "why": "why",
    "assumptions": "assumptions",
    "not verified": "not_verified",
    "rollback": "rollback",
    "decisions": "decisions",
}
MAX_BRIEF_SCAN_BYTES = 60000
MAX_BRIEF_BULLETS = 50
MAX_BRIEF_TEXT = 1000
_BRIEF_NONE = frozenset({"none", "none.", "n/a", "-"})

# Comment marker written by the PR reviewer flow, e.g.
#   <!-- preloop-review:flow-id:pr-reviewer:severity:HIGH -->
REVIEW_MARKER_RE = re.compile(
    r"<!--\s*preloop-review:flow-id:([^\s:>]+)(?::severity:([^\s>]+))?\s*-->"
)

# Cap on how many times one PR may restart its implementer flow. Flows can
# override it (see :func:`max_resumes_per_pr`).
DEFAULT_MAX_RESUMES_PER_PR = 5

_RUNNING_STATUSES = ("PENDING", "INITIALIZING", "STARTING", "RUNNING")


def _is_github_host(host: str) -> bool:
    return host in _GITHUB_HOSTS


def _is_tracker_api_url(url: str) -> bool:
    """True for GitLab/GitHub REST URLs that must not be compared to HTML URLs."""

    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if "/api/v4/" in path:
        return True
    if host in {"api.github.com", "api.bitbucket.org"}:
        return True
    return False


def normalize_pr_url(url: Optional[str]) -> str:
    """Canonicalize a GitHub PR or GitLab MR HTML URL for matching."""

    if not url or not isinstance(url, str):
        return ""
    raw = url.strip()
    if _is_tracker_api_url(raw):
        return ""
    parsed = urlparse(raw)
    if not parsed.netloc or not parsed.path:
        return ""
    path = parsed.path.rstrip("/")
    host = (parsed.hostname or "").lower()
    if _is_github_host(host):
        parts = path.split("/")
        # GitHub issue comments on PRs often use /issues/N in html_url.
        if len(parts) >= 5 and parts[3] == "issues":
            parts[3] = "pull"
            path = "/".join(parts)
    scheme = parsed.scheme or "https"
    return f"{scheme}://{host}{path}"


def _first_html_pr_url(*candidates: Optional[str]) -> Optional[str]:
    for raw in candidates:
        normalized = normalize_pr_url(raw)
        if normalized:
            return normalized
    return None


def extract_pr_url_from_comment_event(event_data: Dict[str, Any]) -> Optional[str]:
    """Return the PR/MR HTML URL for a comment event, or None for issue comments."""

    payload = event_data.get("payload") or event_data
    if not isinstance(payload, dict):
        return None

    issue = payload.get("issue")
    if isinstance(issue, dict):
        pr_stub = issue.get("pull_request")
        if isinstance(pr_stub, dict):
            return _first_html_pr_url(
                pr_stub.get("html_url"),
                issue.get("html_url"),
                pr_stub.get("url"),
            )

    pr = payload.get("pull_request")
    if isinstance(pr, dict):
        found = _first_html_pr_url(pr.get("html_url"), pr.get("url"))
        if found:
            return found

    mr = payload.get("merge_request")
    if isinstance(mr, dict):
        # create_merge_request persists mr.web_url. Webhook notes expose both
        # an API ``url`` and a browser ``web_url``; prefer the HTML form.
        found = _first_html_pr_url(mr.get("web_url"), mr.get("url"))
        if found:
            return found

    bitbucket_pr = payload.get("pullrequest")
    if isinstance(bitbucket_pr, dict):
        links = bitbucket_pr.get("links") or {}
        found = _first_html_pr_url((links.get("html") or {}).get("href"))
        if found:
            return found

    return None


def parse_pr_opened_marker(line: str) -> Optional[Dict[str, str]]:
    """Parse the wrapper's ``PRELOOP_PR_OPENED {...}`` log line.

    Returns ``{"url": ..., "branch": ..., "provider": ...}`` or None when the
    line is not the marker or carries no usable URL.
    """

    if not line or not isinstance(line, str):
        return None
    idx = line.find(PR_OPENED_MARKER)
    if idx < 0:
        return None
    payload = line[idx + len(PR_OPENED_MARKER) :].strip()
    start = payload.find("{")
    end = payload.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(payload[start : end + 1])
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    url = parsed.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    out: Dict[str, str] = {"url": url.strip()}
    branch = parsed.get("branch")
    if isinstance(branch, str) and branch.strip():
        out["branch"] = branch.strip()
    provider = parsed.get("provider")
    if isinstance(provider, str) and provider.strip():
        out["provider"] = provider.strip()
    return out


def _decision_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or _DECISION_PLACEHOLDER_RE.match(text):
        return None
    return text[:MAX_DECISION_TEXT]


def parse_decision_marker(line: str) -> Optional[Dict[str, Any]]:
    """Parse an agent's ``PRELOOP_DECISION {...}`` log line.

    Args:
        line: One log line.

    Returns:
        ``{"summary": str, "reason": str, "alternatives": [str, ...]}``, or
        None when the line is not the marker. A marker line that is not a JSON
        object with a summary and a reason (neither empty nor a placeholder)
        is logged and ignored.
    """

    if not line or not isinstance(line, str):
        return None
    idx = line.find(DECISION_MARKER)
    if idx < 0:
        return None
    payload = line[idx + len(DECISION_MARKER) :].strip()
    start = payload.find("{")
    parsed: Any = None
    if start >= 0:
        try:
            parsed, _ = json.JSONDecoder().raw_decode(payload[start:])
        except ValueError:
            parsed = None
    if not isinstance(parsed, dict):
        logger.warning(
            "Ignoring malformed %s line (%d chars)", DECISION_MARKER, len(line)
        )
        return None
    summary = _decision_text(parsed.get("summary"))
    reason = _decision_text(parsed.get("reason"))
    if summary is None or reason is None:
        logger.warning(
            "Ignoring %s line without a summary and a reason", DECISION_MARKER
        )
        return None
    raw_alternatives = parsed.get("alternatives")
    alternatives: List[str] = []
    if isinstance(raw_alternatives, list):
        for item in raw_alternatives:
            text = _decision_text(item)
            if text is not None:
                alternatives.append(text)
            if len(alternatives) >= MAX_DECISION_ALTERNATIVES:
                break
    return {"summary": summary, "reason": reason, "alternatives": alternatives}


def parse_decisions(transcript: Optional[str]) -> List[Dict[str, Any]]:
    """Return every well-formed decision marker in ``transcript``, in order."""

    if not transcript:
        return []
    decisions: List[Dict[str, Any]] = []
    for line in transcript.splitlines():
        if DECISION_MARKER not in line:
            continue
        decision = parse_decision_marker(line)
        if decision is not None:
            decisions.append(decision)
    return decisions


def record_decision(
    db: Session,
    execution_id: Any,
    decision: Dict[str, Any],
    *,
    raise_errors: bool = False,
) -> bool:
    """Append one parsed decision to ``result["decisions"]`` once.

    Returns:
        False when the write failed, so a caller can retry it later.
    """

    try:
        if execution_id:
            crud_flow_execution.add_decision(
                db, execution_id=execution_id, decision=decision
            )
        return True
    except Exception:
        logger.warning(
            "Failed to record decision on execution %s", execution_id, exc_info=True
        )
        db.rollback()
        if raise_errors:
            raise
        return False


def parse_brief(body: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse the ``PRELOOP_BRIEF`` block out of a pull request body.

    Args:
        body: Pull request description; only its first 60,000 bytes are read.

    Returns:
        None when there is no complete block. Otherwise one key per section
        (``what_changed``, ``why``, ``assumptions``, ``not_verified``,
        ``decisions`` as lists of bullets; ``rollback`` as one line or None),
        ``missing`` listing sections the block does not have, and
        ``truncated`` when a bullet or a section was cut to the caps.
    """

    if not body or not isinstance(body, str):
        return None
    text = body.encode("utf-8")[:MAX_BRIEF_SCAN_BYTES].decode("utf-8", "ignore")
    start = text.find(BRIEF_START)
    if start < 0:
        return None
    end = text.find(BRIEF_END, start + len(BRIEF_START))
    if end < 0:
        return None
    sections: Dict[str, List[str]] = {}
    current: Optional[List[str]] = None
    truncated = False
    for raw in text[start + len(BRIEF_START) : end].splitlines():
        line = raw.strip()
        if line.startswith("#"):
            key = BRIEF_SECTIONS.get(line.lstrip("#").strip().lower())
            current = sections.setdefault(key, []) if key else None
            continue
        if current is None or not line:
            continue
        if line[:2] in ("- ", "* "):
            line = line[2:].strip()
        if not line or line.lower() in _BRIEF_NONE:
            continue
        if _DECISION_PLACEHOLDER_RE.match(line):
            continue
        if len(current) >= MAX_BRIEF_BULLETS:
            truncated = True
            continue
        if len(line) > MAX_BRIEF_TEXT:
            line = line[:MAX_BRIEF_TEXT]
            truncated = True
        current.append(line)
    brief: Dict[str, Any] = {}
    for key in BRIEF_SECTIONS.values():
        if key == "rollback":
            lines = sections.get(key) or []
            brief[key] = lines[0] if lines else None
        else:
            brief[key] = sections.get(key, [])
    brief["missing"] = [k for k in BRIEF_SECTIONS.values() if k not in sections]
    brief["truncated"] = truncated
    return brief


def brief_from_result(result: Any) -> Optional[Dict[str, Any]]:
    """Parse the brief from the PR body an agent result carries, if any.

    The body is chosen exactly as the publisher chooses it
    (:func:`preloop.utils.pr_metadata.read_result_metadata`), so the stored
    brief always describes the published body.
    """

    if not isinstance(result, dict):
        return None
    try:
        raw = json.dumps(result).encode("utf-8")
    except (TypeError, ValueError):
        return None
    _title, body, _warnings = read_result_metadata(raw)
    return parse_brief(body)


def parse_review_marker(body: Optional[str]) -> Optional[str]:
    """Return the flow id carried by a ``preloop-review`` comment marker."""

    if not body or not isinstance(body, str):
        return None
    match = REVIEW_MARKER_RE.search(body)
    if not match:
        return None
    return match.group(1)


def extract_comment_body(event_data: Dict[str, Any]) -> str:
    """Best-effort comment body for GitHub, GitLab and Bitbucket comments."""

    payload = event_data.get("payload") or event_data
    if not isinstance(payload, dict):
        return ""
    comment = payload.get("comment")
    if isinstance(comment, dict):
        for key in ("body", "note"):
            value = comment.get(key)
            if isinstance(value, str) and value:
                return value
        # Bitbucket Cloud: comment.content.raw
        content = comment.get("content")
        if isinstance(content, dict):
            raw = content.get("raw")
            if isinstance(raw, str) and raw:
                return raw
    elif isinstance(comment, str) and comment:
        return comment
    obj_attrs = payload.get("object_attributes")
    if isinstance(obj_attrs, dict):
        for key in ("note", "description", "body"):
            value = obj_attrs.get(key)
            if isinstance(value, str) and value:
                return value
    for key in ("body", "note"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def extract_comment_url(event_data: Dict[str, Any]) -> Optional[str]:
    """Best-effort browser URL of the comment that triggered the event."""

    payload = event_data.get("payload") or event_data
    if not isinstance(payload, dict):
        return None
    comment = payload.get("comment")
    if isinstance(comment, dict):
        for key in ("html_url", "url", "web_url"):
            value = comment.get(key)
            if isinstance(value, str) and value:
                return value
    obj_attrs = payload.get("object_attributes")
    if isinstance(obj_attrs, dict):
        for key in ("url", "web_url"):
            value = obj_attrs.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _flow_slug(name: Any) -> str:
    if not isinstance(name, str):
        return ""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def marker_flow_id_matches(flow: Any, marker_flow_id: Optional[str]) -> bool:
    """True when a review marker names the flow that is about to receive it.

    The marker carries whatever identifier the reviewer flow was told to write
    (its UUID, or a preset slug like ``pr-reviewer``), so both forms count.
    """

    if not marker_flow_id:
        return False
    needle = marker_flow_id.strip().lower()
    if not needle:
        return False
    candidates = {str(getattr(flow, "id", "") or "").lower()}
    slug = _flow_slug(getattr(flow, "name", None))
    if slug:
        candidates.add(slug)
    candidates.discard("")
    return needle in candidates


def max_resumes_per_pr(flow: Any) -> int:
    """Flow-level cap on resumes started from one PR (default 5)."""

    raw = getattr(flow, "max_resumes_per_pr", None)
    # Only a real number-shaped value counts; a future column, a test double
    # or a stray attribute must not silently become the cap.
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raw = None
    if raw is None:
        config = getattr(flow, "agent_config", None)
        if isinstance(config, dict):
            raw = config.get("max_resumes_per_pr")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_RESUMES_PER_PR
    return value if value >= 0 else DEFAULT_MAX_RESUMES_PER_PR


def resume_count(execution: Any) -> int:
    """Resumes already started from the PR this execution opened."""

    result = getattr(execution, "result", None)
    if not isinstance(result, dict):
        return 0
    try:
        return max(int(result.get("resume_count") or 0), 0)
    except (TypeError, ValueError):
        return 0


def _write_result(db: Session, execution: Any, updates: Dict[str, Any]) -> bool:
    """Merge ``updates`` onto ``execution.result`` and commit. Never raises."""

    try:
        current = dict(execution.result) if isinstance(execution.result, dict) else {}
        current.update(updates)
        execution.result = current
        flag_modified(execution, "result")
        db.commit()
        return True
    except Exception:
        logger.warning(
            "Failed to update result on execution %s",
            getattr(execution, "id", None),
            exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            logger.debug(
                "Could not roll back after a failed result write", exc_info=True
            )
        return False


def note_resume_started(db: Session, execution: Any) -> int:
    """Increment and persist the resume counter on the bound execution."""

    count = resume_count(execution) + 1
    _write_result(db, execution, {"resume_count": count})
    return count


def find_running_executions_for_pr(
    db: Session, flow: Any, pr_url: str
) -> List[FlowExecution]:
    """Running executions of ``flow`` that belong to ``pr_url``.

    Matches both the execution that opened the PR (``result.pr_url``) and any
    resume started from it (``trigger_event_details._resume.pr_url``).
    """

    needle = normalize_pr_url(pr_url)
    if not needle:
        return []
    try:
        executions = crud_flow_execution.get_running_by_flow(
            db, flow_id=flow.id, running_statuses=list(_RUNNING_STATUSES)
        )
    except Exception:
        logger.warning(
            "Could not list running executions for PR follow-up", exc_info=True
        )
        return []
    matches: List[FlowExecution] = []
    for execution in executions:
        result = execution.result if isinstance(execution.result, dict) else {}
        if normalize_pr_url(result.get("pr_url")) == needle:
            matches.append(execution)
            continue
        details = execution.trigger_event_details or {}
        resume = details.get("_resume") if isinstance(details, dict) else None
        if (
            isinstance(resume, dict)
            and normalize_pr_url(resume.get("pr_url")) == needle
        ):
            matches.append(execution)
    return matches


def queue_pending_followup(
    db: Session, execution: Any, comment_url: Optional[str]
) -> bool:
    """Flag one pending follow-up on a running execution (coalescing).

    Later comments during the same run keep the first flag: one run produces
    exactly one follow-up.
    """

    result = execution.result if isinstance(execution.result, dict) else {}
    if result.get("pending_followup"):
        logger.info(
            "Follow-up already queued on execution %s; coalescing this comment",
            getattr(execution, "id", None),
        )
        return False
    updates: Dict[str, Any] = {"pending_followup": True}
    if comment_url:
        updates["pending_followup_comment_url"] = comment_url
    return _write_result(db, execution, updates)


def take_pending_followup(db: Session, execution: Any) -> Optional[Dict[str, Any]]:
    """Clear the pending flag and return what the follow-up needs, or None.

    Refreshes ``execution`` first: the flag is written from a different DB
    session (the trigger service) while this object may be stale after
    commit-status I/O on the terminal path.
    """

    try:
        db.refresh(execution)
    except Exception:
        logger.debug(
            "Could not refresh execution %s before taking a follow-up",
            getattr(execution, "id", None),
            exc_info=True,
        )

    result = execution.result if isinstance(execution.result, dict) else {}
    if not result.get("pending_followup"):
        return None
    comment_url = result.get("pending_followup_comment_url")
    _write_result(
        db,
        execution,
        {"pending_followup": False, "pending_followup_taken_at_comment": comment_url},
    )
    details = execution.trigger_event_details or {}
    resume = details.get("_resume") if isinstance(details, dict) else None
    pr_url = result.get("pr_url")
    source_branch = result.get("pr_source_branch")
    if isinstance(resume, dict):
        pr_url = pr_url or resume.get("pr_url")
        source_branch = source_branch or resume.get("source_branch")
    return {
        "comment_url": comment_url,
        "pr_url": pr_url,
        "source_branch": source_branch,
    }


def merge_result_preserving_pr_binding(
    existing: Optional[Any], incoming: Optional[Any]
) -> Optional[Any]:
    """Keep ``pr_url`` / ``pr_source_branch``, recorded decisions and the
    parsed brief when the agent result overwrites."""

    if incoming is None:
        return existing
    if not isinstance(incoming, dict):
        return incoming
    # Only the parser writes ``brief``; an agent-authored key never lands.
    incoming = {key: value for key, value in incoming.items() if key != "brief"}
    if not isinstance(existing, dict):
        return incoming
    merged = dict(existing)
    merged.update(incoming)
    for key in ("pr_url", "pr_source_branch"):
        if not merged.get(key) and existing.get(key):
            merged[key] = existing[key]
    for key in ("native_resume", "continuation"):
        if key in existing:
            merged[key] = existing[key]
    if "published_prs" in existing:
        merged["published_prs"] = existing["published_prs"]
    if "published_prs_truncated" in existing:
        merged["published_prs_truncated"] = existing["published_prs_truncated"]
    for key in ("decisions", "decisions_truncated"):
        if key in existing:
            merged[key] = existing[key]
    if "brief" in existing:
        merged["brief"] = existing["brief"]
    return merged


def merge_result_with_brief(existing: Any, incoming: Any) -> Any:
    """Keep the PR binding and parse the brief from the body being published.

    Args:
        existing: Execution result written by the publisher and marker reader.
        incoming: The agent's result document.

    Returns:
        The merged result with a parser-owned ``brief`` when a block exists.
    """
    merged = merge_result_preserving_pr_binding(existing, incoming)
    brief = brief_from_result(incoming)
    if brief is not None and isinstance(merged, dict):
        merged["brief"] = brief
    return merged


def record_published_pr(
    db: Session,
    execution_id: Any,
    publication: Dict[str, str],
    *,
    raise_errors: bool = False,
) -> None:
    """Save a wrapper publication as an inspectable output without rebinding follow-up."""
    try:
        if execution_id and publication.get("url"):
            parsed_url = urlparse(publication["url"])
            repository_path = parsed_url.path
            for separator in (
                "/-/merge_requests/",
                "/merge_requests/",
                "/pull/",
                "/pull-requests/",
            ):
                if separator in repository_path:
                    repository_path = repository_path.split(separator, 1)[0]
                    break
            output = {
                **publication,
                "repository": f"{parsed_url.netloc}{repository_path}",
            }
            crud_flow_execution.add_published_pr(
                db, execution_id=execution_id, publication=output
            )
    except Exception:
        logger.warning(
            "Failed to record published PR on execution %s", execution_id, exc_info=True
        )
        db.rollback()
        if raise_errors:
            raise


def record_cli_session(
    db: Session, execution_id: Any, cli_session: Any, *, raise_errors: bool = False
) -> None:
    """Persist the CLI session; runner acknowledgments require durable success.

    ``cli_session`` is ``{"agent_type": ..., "session_id": ...}`` as parsed
    from the PRELOOP_AGENT_SESSION marker. Overwrites so a retried attempt's
    session replaces the failed attempt's.
    """

    try:
        if not execution_id or not isinstance(cli_session, dict):
            return
        if not cli_session.get("session_id"):
            return
        execution = crud_flow_execution.get(db, id=execution_id)
        if execution is None:
            if raise_errors:
                raise ValueError("Execution is no longer available for handoff capture")
            logger.warning(
                "Cannot record CLI session: execution %s not found", execution_id
            )
            return
        crud_flow_execution.set_cli_session(
            db,
            db_obj=execution,
            cli_session={
                "agent_type": cli_session.get("agent_type"),
                "session_id": cli_session.get("session_id"),
                **{
                    key: cli_session[key]
                    for key in (
                        "thread_id",
                        "harness_version",
                        "expires_at",
                        "artifact_reference",
                    )
                    if key in cli_session
                },
            },
        )
        db.commit()
        logger.info("Recorded CLI session on execution %s", execution_id)
    except Exception:
        logger.warning(
            "Failed to record CLI session on execution %s",
            execution_id,
            exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            logger.debug(
                "Could not roll back after a failed CLI session write",
                exc_info=True,
            )
        if raise_errors:
            raise


def resume_cli_session_of(execution: Any) -> Optional[Dict[str, Any]]:
    """CLI session stored on ``execution``, shaped for the resume metadata."""

    cli = getattr(execution, "cli_session", None)
    if not isinstance(cli, dict) or not cli.get("session_id"):
        return None
    return {
        "agent_type": cli.get("agent_type"),
        "session_id": cli.get("session_id"),
        **{
            key: cli[key]
            for key in (
                "thread_id",
                "harness_version",
                "expires_at",
                "artifact_reference",
            )
            if key in cli
        },
    }


def record_opened_pr(
    db: Session,
    execution_id: Any,
    pr_url: str,
    source_branch: Optional[str] = None,
    *,
    raise_errors: bool = False,
    opened_at: Any = None,
) -> None:
    """Merge the PR URL; best effort except when the runner awaits acknowledgment.

    Args:
        db: Database session.
        execution_id: The execution that opened the pull request.
        pr_url: The pull request URL.
        source_branch: Its head branch, when known.
        raise_errors: Re-raise a failed write (the runner waits for it).
        opened_at: The forge's ``created_at`` for the pull request, when the
            caller read it from the forge. The issue cost rollup then reports
            that time as "PR opened" instead of the bind time.
    """

    try:
        if not execution_id or not pr_url:
            return
        stored_url = normalize_pr_url(pr_url) or pr_url
        execution = crud_flow_execution.bind_publication(
            db,
            execution_id=execution_id,
            pr_url=stored_url,
            source_branch=source_branch,
        )
        if execution is None:
            if raise_errors:
                raise ValueError("Execution is no longer available for handoff capture")
            logger.warning(
                "Cannot record opened PR: execution %s not found", execution_id
            )
            return
        logger.info("Recorded opened PR on execution %s", execution_id)
        from preloop.services.issue_cost_rollup import record_publication_safely

        record_publication_safely(
            db, execution.id, stored_url, forge_opened_at=opened_at
        )
        if source_branch:
            from preloop.services.flow_feedback import register_thread

            register_thread(db, execution, stored_url, source_branch)
    except Exception:
        logger.warning(
            "Failed to record opened PR on execution %s",
            execution_id,
            exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            # Best-effort: the outer warning already recorded the write failure.
            logger.debug(
                "Could not roll back after a failed PR-binding write",
                exc_info=True,
            )
        if raise_errors:
            raise


def find_bound_execution(
    db: Session, flow_id: Any, pr_url: str
) -> Optional[FlowExecution]:
    """Return the most recent execution of this flow that opened ``pr_url``."""

    needle = normalize_pr_url(pr_url)
    if not needle:
        return None
    direct = crud_flow_execution.get_by_result_pr_url(db, flow_id, needle)
    if direct is not None:
        return direct
    executions = crud_flow_execution.get_by_flow(
        db, flow_id=flow_id, skip=0, limit=_RESUME_LOOKBACK
    )
    for execution in executions:
        result = execution.result if isinstance(execution.result, dict) else {}
        stored = normalize_pr_url(result.get("pr_url"))
        if stored and stored == needle:
            return execution
    logger.info(
        "No execution of flow %s recorded PR %s (jsonb miss, lookback=%s)",
        flow_id,
        needle,
        _RESUME_LOOKBACK,
    )
    return None


def flow_requires_pr_comment_resume(flow: Any) -> bool:
    """True when comment_created on this flow must correlate to an opened PR.

    Flows that listen to both issue labeling and comments would otherwise start
    a cold agent on every issue comment. Comment-only flows are unchanged.
    """

    types = getattr(flow, "trigger_event_types", None)
    if not isinstance(types, (list, tuple, set)):
        return False
    type_set = set(types)
    if "comment_created" not in type_set:
        return False
    return bool(type_set & {"issue_labeled", "issue_opened"})


def is_bound_implementation_comment(
    db: Session, flow: Any, event: dict[str, Any]
) -> bool:
    """Scope the issue-label exemption to an already authorized PR handoff.

    Existing issue-only automations do not gain comment triggers. A comment
    must match the original execution's account, tracker and immutable provider
    repository identity before its originating issue's intake label is ignored.
    """
    if event.get("type") != "comment_created" or not flow_requires_pr_comment_resume(
        flow
    ):
        return False
    account_id = getattr(flow, "account_id", None)
    tracker_id = getattr(flow, "trigger_event_source", None)
    if (
        not account_id
        or str(event.get("account_id")) != str(account_id)
        or not tracker_id
        or str(event.get("tracker_id")) != str(tracker_id)
    ):
        return False
    pr_url = extract_pr_url_from_comment_event(event)
    if not pr_url:
        return False
    execution = find_bound_execution(db, flow.id, pr_url)
    if execution is None or execution.flow_id != flow.id:
        return False
    source = execution.trigger_event_details or {}
    provider = source.get("source")
    if (
        provider not in {"github", "gitlab", "bitbucket", "bitbucket_dc"}
        or provider != event.get("source")
        or str(source.get("account_id")) != str(account_id)
        or str(source.get("tracker_id")) != str(tracker_id)
    ):
        return False
    payload = event.get("payload") or {}
    original = source.get("payload") or {}
    repository = payload.get("repository") or payload.get("project") or {}
    original_repository = original.get("repository") or original.get("project") or {}
    if provider == "bitbucket":
        from preloop.utils.bitbucket import repository_identity

        repository_id = repository_identity(repository)
        original_id = repository_identity(original_repository)
    else:
        repository_id = repository.get("id")
        original_id = original_repository.get("id")
    return bool(repository_id) and str(repository_id) == str(original_id)


def record_runner_handoff_markers(
    db: Session, execution: Any, line: str, *, isolated_publication: bool
) -> None:
    """Persist handoff metadata from a log frame accepted from the owning runner.

    Call only after authenticating the runner's account and current execution
    lease. Publication in an isolated run still requires the trusted receipt;
    native artifact references are checked again by the checkpoint resolver.
    """
    from preloop.agents.cli_session import parse_agent_session_marker, valid_session_id
    from preloop.agents.session_runtime import parse_native_artifact_marker

    if DECISION_MARKER in line:
        decision = parse_decision_marker(line)
        if decision is not None:
            # Report-only: a failed write must not fail the log batch's ack.
            record_decision(db, execution.id, decision)
    if not isolated_publication:
        opened = parse_pr_opened_marker(line)
        if opened:
            current = execution.result or {}
            if not current.get("pr_url") or (
                normalize_pr_url(current.get("pr_url"))
                == normalize_pr_url(opened["url"])
                and not current.get("pr_source_branch")
                and opened.get("branch")
            ):
                record_opened_pr(
                    db,
                    execution.id,
                    opened["url"],
                    source_branch=opened.get("branch"),
                    raise_errors=True,
                )
            if not current.get("published_prs_truncated"):
                record_published_pr(db, execution.id, opened, raise_errors=True)
    artifact = parse_native_artifact_marker(line)
    if artifact:
        expected_thread = (execution.trigger_event_details or {}).get(
            "_session_thread_id"
        )
        reference = artifact.get("artifact_reference") or {}
        if (
            expected_thread
            and artifact.get("thread_id") == expected_thread
            and str(reference.get("execution_id")) == str(execution.id)
            and valid_session_id(
                artifact.get("agent_type", ""), artifact.get("session_id", "")
            )
        ):
            record_cli_session(db, execution.id, artifact, raise_errors=True)
        return
    parsed = parse_agent_session_marker(line)
    if (
        parsed
        and valid_session_id(parsed["agent_type"], parsed["session_id"])
        and not execution.cli_session
    ):
        record_cli_session(db, execution.id, parsed, raise_errors=True)


def bind_resume_or_skip(
    db: Session, flow: Any, event_data: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Attach ``_resume`` when this comment belongs to a PR this flow opened.

    Returns the resume dict, or None when the event should not start a run.
    Three guards can drop an otherwise-correlated comment:

    * self-loop: the comment carries this flow's own review marker,
    * cap: the PR already started ``max_resumes_per_pr`` resumes,
    * in flight: a run for this PR is still going, so the comment is queued
      as a single follow-up instead of starting a second run.
    """

    pr_url = extract_pr_url_from_comment_event(event_data)
    if not pr_url:
        return None

    marker_flow_id = parse_review_marker(extract_comment_body(event_data))
    if marker_flow_id and marker_flow_id_matches(flow, marker_flow_id):
        logger.info(
            "Skipping comment on %s: its review marker names the receiving "
            "flow %s (self-loop guard)",
            pr_url,
            marker_flow_id,
        )
        return None

    execution = find_bound_execution(db, flow.id, pr_url)
    if execution is None:
        return None

    # Any still-running execution for this PR (the run that opened it, or a
    # resume already started from it) takes the comment as a follow-up.
    running = find_running_executions_for_pr(db, flow, pr_url)
    if running:
        target = running[0]
        queue_pending_followup(db, target, extract_comment_url(event_data))
        logger.info(
            "Queued one follow-up on running execution %s for %s",
            target.id,
            pr_url,
        )
        return None

    cap = max_resumes_per_pr(flow)
    started = resume_count(execution)
    if started >= cap:
        logger.info(
            "Skipping comment on %s: flow %s already started %s/%s resumes "
            "for this PR (max_resumes_per_pr)",
            pr_url,
            flow.id,
            started,
            cap,
        )
        return None

    result = execution.result if isinstance(execution.result, dict) else {}
    resume = {
        "execution_id": str(execution.id),
        "pr_url": result.get("pr_url") or pr_url,
        "source_branch": result.get("pr_source_branch"),
        "resume_index": note_resume_started(db, execution),
        "comment_url": extract_comment_url(event_data),
    }
    cli_session = resume_cli_session_of(execution)
    if cli_session:
        resume["cli_session"] = cli_session
    if marker_flow_id:
        resume["review_flow_id"] = marker_flow_id
    event_data["_resume"] = resume
    return resume
