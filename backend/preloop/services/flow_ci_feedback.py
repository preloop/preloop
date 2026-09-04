"""Retrigger an implementation flow when CI fails on the PR it opened.

Issue-implementation flows record the PR they opened on
``FlowExecution.result`` (see :mod:`preloop.services.flow_pr_binding`). A
failing GitHub check run / check suite / workflow run, or a failing GitLab
pipeline / job, on that PR head is the same kind of feedback as a review
comment: it should resume the agent that wrote the branch instead of
starting a cold run.

This module is deliberately separate from ``flow_pr_binding`` so the comment
correlation path and the CI path can evolve (and merge) independently. It
reuses ``flow_pr_binding`` helpers for URL normalization and lookup.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from preloop.models.crud import crud_flow_execution
from preloop.models.models.flow_execution import FlowExecution
from preloop.services.flow_pr_binding import find_bound_execution, normalize_pr_url

logger = logging.getLogger(__name__)

# Normalized event types (see preloop.sync.event_normalizer) that can carry a
# CI failure for a PR head.
GITHUB_CI_EVENT_TYPES = frozenset({"check_run", "check_suite", "workflow_run"})
GITLAB_CI_EVENT_TYPES = frozenset({"pipeline", "job"})
CI_FAILURE_EVENT_TYPES = GITHUB_CI_EVENT_TYPES | GITLAB_CI_EVENT_TYPES

# GitHub conclusions that mean "the branch is broken". ``cancelled`` and
# ``skipped`` are excluded: they are usually a human or a concurrency group,
# not a defect the agent can fix.
GITHUB_FAILED_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "startup_failure", "action_required"}
)
GITLAB_FAILED_STATUSES = frozenset({"failed"})

# Reserved key on the trigger event payload, mirroring ``_resume``. The
# orchestrator copies trigger event data into the execution context, so this
# surfaces as ``execution_context["trigger_event_data"]["_ci_failure"]`` and
# as ``{{execution.ci_failure}}`` in prompts.
CI_FAILURE_KEY = "_ci_failure"

# Maximum number of CI-failure resumes started for one PR.
# TODO: unify with the flow-level comment resume cap once the comment path
# grows a configurable setting (see flow_pr_binding / flow settings).
DEFAULT_CI_FAILURE_CAP = 5

_LOOKBACK = 50


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _payload(event_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(event_data, dict):
        return {}
    payload = event_data.get("payload")
    if isinstance(payload, dict):
        return payload
    return event_data


def _mr_url_from_payload(payload: Dict[str, Any]) -> Optional[str]:
    """PR/MR HTML URL carried directly by a GitLab CI payload, if any."""

    mr = payload.get("merge_request")
    if isinstance(mr, dict):
        for key in ("web_url", "url"):
            normalized = normalize_pr_url(mr.get(key))
            if normalized:
                return normalized
    return None


def _github_project_url(payload: Dict[str, Any]) -> Optional[str]:
    repo = payload.get("repository")
    if isinstance(repo, dict):
        return _text(repo.get("html_url"))
    return None


def _github_repo_full_name(payload: Dict[str, Any]) -> Optional[str]:
    repo = payload.get("repository")
    if isinstance(repo, dict):
        return _text(repo.get("full_name"))
    return None


def _github_pr_url(payload: Dict[str, Any], obj: Dict[str, Any]) -> Optional[str]:
    """HTML PR URL from ``pull_requests[]``, or constructed from repo + number."""

    for source in (obj, payload):
        prs = source.get("pull_requests") if isinstance(source, dict) else None
        if not isinstance(prs, list):
            continue
        for pr in prs:
            if not isinstance(pr, dict):
                continue
            html = normalize_pr_url(_text(pr.get("html_url")))
            if html:
                return html
            number = pr.get("number")
            repo = _github_repo_full_name(payload)
            if number is not None and repo:
                return f"https://github.com/{repo}/pull/{number}"
    return None


def _gitlab_project_url(payload: Dict[str, Any]) -> Optional[str]:
    project = payload.get("project")
    if isinstance(project, dict):
        return _text(project.get("web_url"))
    return None


def _github_failure(
    event_type: str, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    if event_type == "check_run":
        obj = payload.get("check_run")
        if not isinstance(obj, dict):
            return None
        suite = (
            obj.get("check_suite") if isinstance(obj.get("check_suite"), dict) else {}
        )
        branch = _text(suite.get("head_branch"))
        url = _text(obj.get("html_url")) or _text(obj.get("details_url"))
        name = _text(obj.get("name")) or "check run"
        head_sha = _text(obj.get("head_sha")) or _text(suite.get("head_sha"))
    elif event_type == "check_suite":
        obj = payload.get("check_suite")
        if not isinstance(obj, dict):
            return None
        branch = _text(obj.get("head_branch"))
        head_sha = _text(obj.get("head_sha"))
        url = _text(obj.get("html_url"))
        if not url:
            project = _github_project_url(payload)
            if project and head_sha:
                url = f"{project}/commit/{head_sha}/checks"
        app = obj.get("app") if isinstance(obj.get("app"), dict) else {}
        name = _text(app.get("name")) or "check suite"
    else:  # workflow_run
        obj = payload.get("workflow_run")
        if not isinstance(obj, dict):
            return None
        branch = _text(obj.get("head_branch"))
        head_sha = _text(obj.get("head_sha"))
        url = _text(obj.get("html_url"))
        name = _text(obj.get("name")) or "workflow run"

    status = (_text(obj.get("status")) or "").lower()
    conclusion = (_text(obj.get("conclusion")) or "").lower()
    if status and status != "completed":
        return None
    if conclusion not in GITHUB_FAILED_CONCLUSIONS:
        return None

    return {
        "provider": "github",
        "name": name,
        "url": url,
        "conclusion": conclusion,
        "head_sha": head_sha,
        "branch": branch,
        "pr_url": _github_pr_url(payload, obj),
        "repo": _github_repo_full_name(payload),
    }


def _gitlab_failure(
    event_type: str, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    if event_type == "pipeline":
        attrs = payload.get("object_attributes")
        if not isinstance(attrs, dict):
            return None
        status = (_text(attrs.get("status")) or "").lower()
        if status not in GITLAB_FAILED_STATUSES:
            return None
        branch = _text(attrs.get("ref"))
        head_sha = _text(attrs.get("sha"))
        name = _text(attrs.get("name")) or "pipeline"
        url = _text(attrs.get("url"))
        if not url:
            project = _gitlab_project_url(payload)
            pipeline_id = _text(attrs.get("id"))
            if project and pipeline_id:
                url = f"{project}/-/pipelines/{pipeline_id}"
        conclusion = status
    else:  # job (GitLab "Job Hook", object_kind=build)
        status = (_text(payload.get("build_status")) or "").lower()
        if status not in GITLAB_FAILED_STATUSES:
            return None
        if payload.get("build_allow_failure") is True:
            return None
        branch = _text(payload.get("ref"))
        head_sha = _text(payload.get("sha"))
        name = _text(payload.get("build_name")) or "job"
        url = None
        project = _gitlab_project_url(payload)
        build_id = _text(payload.get("build_id"))
        if project and build_id:
            url = f"{project}/-/jobs/{build_id}"
        conclusion = status

    return {
        "provider": "gitlab",
        "name": name,
        "url": url,
        "conclusion": conclusion,
        "head_sha": head_sha,
        "branch": branch,
        "pr_url": _mr_url_from_payload(payload),
    }


def extract_ci_failure(
    event_type: str, event_data: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Return CI failure details for this event, or None when it is not a failure.

    The returned dict carries ``provider``, ``name``, ``url``, ``conclusion``,
    ``head_sha`` plus the internal ``branch`` / ``pr_url`` correlation hints.
    """

    payload = _payload(event_data)
    if not payload:
        return None
    try:
        if event_type in GITHUB_CI_EVENT_TYPES:
            return _github_failure(event_type, payload)
        if event_type in GITLAB_CI_EVENT_TYPES:
            return _gitlab_failure(event_type, payload)
    except Exception:
        logger.warning(
            "Could not read CI failure details from a %s event",
            event_type,
            exc_info=True,
        )
    return None


def flow_requires_ci_failure_resume(flow: Any) -> bool:
    """True when a GitHub CI event on this flow must correlate to a PR it opened.

    Only GitHub ``check_run`` / ``check_suite`` / ``workflow_run`` events
    take this path. GitLab ``pipeline`` / ``job`` flows (including those
    that also listen to issue intake) keep firing on every event, so
    existing GitLab implementations are unchanged.
    """

    types = getattr(flow, "trigger_event_types", None)
    if not isinstance(types, (list, tuple, set)):
        return False
    type_set = set(types)
    if not (type_set & GITHUB_CI_EVENT_TYPES):
        return False
    return bool(type_set & {"issue_labeled", "issue_opened"})


def ci_failure_cap(_flow: Any) -> int:
    """Per-PR cap on CI-failure resumes.

    No flow-level setting exists yet; every flow uses
    ``DEFAULT_CI_FAILURE_CAP``. ``flow`` is accepted so the call site can
    pass the flow once a real setting lands.
    """

    return DEFAULT_CI_FAILURE_CAP


def _execution_pr_urls(execution: Any) -> set:
    """Every PR URL an execution is associated with, normalized."""

    urls = set()
    result = getattr(execution, "result", None)
    if isinstance(result, dict):
        normalized = normalize_pr_url(result.get("pr_url"))
        if normalized:
            urls.add(normalized)
    details = getattr(execution, "trigger_event_details", None)
    if isinstance(details, dict):
        for key in ("_resume", CI_FAILURE_KEY):
            entry = details.get(key)
            if isinstance(entry, dict):
                normalized = normalize_pr_url(entry.get("pr_url"))
                if normalized:
                    urls.add(normalized)
    return urls


def find_execution_for_ci_failure(
    db: Session, flow: Any, failure: Dict[str, Any]
) -> Optional[FlowExecution]:
    """Return the execution of ``flow`` that opened the PR this CI run tested.

    Matches on the PR URL when the payload carries one (GitHub
    ``pull_requests[]`` or a GitLab MR pipeline). When the payload has a
    PR URL, a miss is a miss — we do not fall back to branch name, which
    would collide across repositories. Branch fallback is only for GitHub
    check events that are not attached to a pull request, and is then
    scoped to the same ``owner/repo`` as the recorded ``pr_url``.
    """

    pr_url = failure.get("pr_url")
    if pr_url:
        return find_bound_execution(db, flow.id, pr_url)

    branch = failure.get("branch")
    if not branch:
        return None
    repo = (failure.get("repo") or "").lower()
    executions = crud_flow_execution.get_by_flow(
        db, flow_id=flow.id, skip=0, limit=_LOOKBACK
    )
    for execution in executions:
        result = execution.result if isinstance(execution.result, dict) else {}
        recorded = result.get("pr_url")
        if not recorded:
            continue
        if result.get("pr_source_branch") != branch:
            continue
        if repo and repo not in str(recorded).lower():
            continue
        return execution
    logger.info(
        "No execution of flow %s opened a PR from branch %s (lookback=%s)",
        getattr(flow, "id", None),
        branch,
        _LOOKBACK,
    )
    return None


def has_running_execution_for_pr(db: Session, flow: Any, pr_url: str) -> bool:
    """True when this flow already has a run in flight for the same PR."""

    needle = normalize_pr_url(pr_url)
    if not needle:
        return False
    for execution in crud_flow_execution.get_running_by_flow(db, flow_id=flow.id):
        if needle in _execution_pr_urls(execution):
            return True
    return False


def count_ci_resumes_for_pr(db: Session, flow: Any, pr_url: str) -> int:
    """Number of CI-failure resumes this flow already started for ``pr_url``."""

    needle = normalize_pr_url(pr_url)
    if not needle:
        return 0
    count = 0
    executions = crud_flow_execution.get_by_flow(
        db, flow_id=flow.id, skip=0, limit=_LOOKBACK
    )
    for execution in executions:
        details = getattr(execution, "trigger_event_details", None)
        if not isinstance(details, dict):
            continue
        entry = details.get(CI_FAILURE_KEY)
        if not isinstance(entry, dict):
            continue
        if normalize_pr_url(entry.get("pr_url")) == needle:
            count += 1
    return count


def bind_ci_failure_resume_or_skip(
    db: Session, flow: Any, event_type: str, event_data: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Attach ``_resume`` and ``_ci_failure`` for a failing CI run on a bound PR.

    Returns the resume dict, or None when the event must not start a run
    (not a failure, PR not opened by this flow, a run already in flight for
    the PR, or the per-PR cap is reached).
    """

    failure = extract_ci_failure(event_type, event_data)
    if not failure:
        return None

    execution = find_execution_for_ci_failure(db, flow, failure)
    if execution is None:
        return None

    result = execution.result if isinstance(execution.result, dict) else {}
    pr_url = normalize_pr_url(result.get("pr_url")) or failure.get("pr_url")
    if not pr_url:
        return None

    if has_running_execution_for_pr(db, flow, pr_url):
        logger.info(
            "Skipping CI failure on %s for flow %s: an execution is still running",
            pr_url,
            getattr(flow, "id", None),
        )
        return None

    cap = ci_failure_cap(flow)
    already = count_ci_resumes_for_pr(db, flow, pr_url)
    if already >= cap:
        logger.info(
            "Skipping CI failure on %s for flow %s: per-PR cap of %s reached",
            pr_url,
            getattr(flow, "id", None),
            cap,
        )
        return None

    resume = {
        "execution_id": str(execution.id),
        "pr_url": pr_url,
        "source_branch": result.get("pr_source_branch") or failure.get("branch"),
    }
    event_data["_resume"] = resume
    event_data[CI_FAILURE_KEY] = {
        "provider": failure.get("provider"),
        "name": failure.get("name"),
        "url": failure.get("url"),
        "conclusion": failure.get("conclusion"),
        "head_sha": failure.get("head_sha"),
        "pr_url": pr_url,
    }
    return resume
