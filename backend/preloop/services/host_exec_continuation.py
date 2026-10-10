"""Feedback continuation for Copilot host profiles (#1069).

A feedback event on a pull request a Copilot host run published creates a
continuation execution through the existing feedback thread machinery
(``_resume`` on the trigger). This module validates that continuation and
builds the runner-facing resume request:

* The validated Copilot session of the originating execution is persisted
  on ``cli_session`` (with profile and model alias) when its publishing run
  succeeds; the control plane, not the trigger copy, is the authority.
* The continuation resumes only on the originating runner and profile with
  the same model alias, and only when that runner advertises
  ``host_continuation``. The resume argument is control-plane owned.
* Any missing piece fails the run with ``resume_unavailable``; a host
  continuation never silently starts a fresh implementation.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional
from uuid import UUID

from sqlalchemy.orm import Session

HOST_CONTINUATION_CAPABILITY = "host_continuation"
HOST_RESUME_LEASE_KEY = "host_exec_resume"
RESUME_UNAVAILABLE = "resume_unavailable"
RESUME_MISMATCH = "resume_identity_mismatch"
_COPILOT_HARNESS = "copilot_cli"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class HostContinuationError(ValueError):
    """A host continuation cannot run; the message starts with the code."""


def _unavailable(reason: str) -> HostContinuationError:
    return HostContinuationError(f"{RESUME_UNAVAILABLE}: {reason}")


def _session_id(value: Any) -> Optional[str]:
    if isinstance(value, str) and _UUID_RE.fullmatch(value.strip().lower()):
        return value.strip().lower()
    return None


def host_continuation_session(
    *, result: Any, pending_job: Mapping[str, Any]
) -> Optional[Dict[str, Any]]:
    """The ``cli_session`` record for a succeeded publishing Copilot run.

    Args:
        result: Validated completion result (``session_id`` from the
            Copilot ``result`` event, captured by the runner).
        pending_job: The persisted lease.

    Returns:
        ``{"agent_type", "harness", "session_id", "host_exec_profile",
        "model_identifier"}``, or None when nothing should be recorded.
    """
    from preloop.services.host_exec import host_exec_profile_name
    from preloop.services.host_exec_publication import HOST_PUBLICATION_LEASE_KEY

    if not pending_job.get(HOST_PUBLICATION_LEASE_KEY):
        return None
    if not isinstance(result, Mapping) or result.get("harness") != _COPILOT_HARNESS:
        return None
    session = _session_id(result.get("session_id"))
    profile = host_exec_profile_name(pending_job)
    if session is None or profile is None:
        return None
    return {
        "agent_type": "copilot",
        "harness": _COPILOT_HARNESS,
        "session_id": session,
        "host_exec_profile": profile,
        "model_identifier": pending_job.get("model_identifier") or None,
    }


def record_host_continuation_session(
    db: Session, execution: Any, *, status: str, result: Any, pending_job: Any
) -> None:
    """Persist the validated Copilot session of a succeeded publishing run."""
    from preloop.models.crud import crud_flow_execution

    if status != "SUCCEEDED" or not isinstance(pending_job, Mapping):
        return
    session = host_continuation_session(result=result, pending_job=pending_job)
    if session is not None:
        crud_flow_execution.set_cli_session(db, db_obj=execution, cli_session=session)


def _runner_advertises(runner: Any, profile: str, capability: str) -> bool:
    capabilities = getattr(runner, "capabilities", None) or {}
    if not isinstance(capabilities, Mapping):
        return False
    for item in capabilities.get("host_exec_profiles") or []:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip().lower() == profile.lower():
            caps = item.get("capabilities") or []
            return isinstance(caps, list) and capability in caps
    return False


def resolve_host_continuation(
    db: Session,
    *,
    flow: Any,
    resume: Any,
    profile: str,
    model_identifier: Optional[str],
) -> Dict[str, str]:
    """Validate a feedback continuation and return the runner resume request.

    Args:
        db: Database session.
        flow: The flow being continued.
        resume: ``trigger_event_data["_resume"]``.
        profile: The flow's current host profile.
        model_identifier: The flow's current host model alias.

    Returns:
        ``{"session_id", "execution_id", "runner_id", "source_branch"}``.

    Raises:
        HostContinuationError: ``resume_unavailable: ...`` for every case
            that must not run (and must not restart from scratch).
    """
    from preloop.models.crud import crud_flow_execution, crud_flow_runner

    if not isinstance(resume, Mapping):
        raise _unavailable("the continuation names no prior execution")
    try:
        prior_id = UUID(str(resume.get("execution_id")))
    except (TypeError, ValueError):
        raise _unavailable("the continuation names no prior execution") from None
    prior = crud_flow_execution.get(
        db, id=prior_id, account_id=str(getattr(flow, "account_id", ""))
    )
    if prior is None or prior.flow_id != getattr(flow, "id", None):
        raise _unavailable("the prior execution does not belong to this flow")
    session = prior.cli_session if isinstance(prior.cli_session, dict) else {}
    session_id = _session_id(session.get("session_id"))
    if session.get("harness") != _COPILOT_HARNESS or session_id is None:
        raise _unavailable("the prior run recorded no Copilot session")
    if str(session.get("host_exec_profile") or "").lower() != profile.lower():
        raise _unavailable(
            "the flow's host profile changed since the prior run; start a new run"
        )
    if (session.get("model_identifier") or None) != (model_identifier or None):
        raise _unavailable(
            "the flow's Copilot model changed since the prior run; start a new run"
        )
    result = prior.result if isinstance(prior.result, dict) else {}
    receipt = result.get("host_publication")
    branch = resume.get("source_branch")
    if (
        not result.get("pr_url")
        or not isinstance(receipt, dict)
        or receipt.get("status") != "pushed"
        or not isinstance(branch, str)
        or receipt.get("branch") != branch
    ):
        raise _unavailable("the prior run's pull request is not confirmed")
    runner_id = getattr(prior, "runner_id", None)
    runner = crud_flow_runner.get(db, id=runner_id) if runner_id else None
    if runner is None or not _runner_advertises(
        runner, profile, HOST_CONTINUATION_CAPABILITY
    ):
        raise _unavailable(
            "the originating runner is gone or does not advertise "
            "host_continuation for this profile"
        )
    return {
        "session_id": session_id,
        "execution_id": str(prior.id),
        "runner_id": str(runner.id),
        "source_branch": branch,
    }


def check_resumed_session(
    status: str,
    error: Optional[str],
    result: Optional[Dict[str, Any]],
    pending_job: Mapping[str, Any],
) -> tuple[str, Optional[str]]:
    """Fail a continuation whose result names another Copilot session."""
    resume = pending_job.get(HOST_RESUME_LEASE_KEY)
    if status != "SUCCEEDED" or not isinstance(resume, Mapping):
        return status, error
    expected = _session_id(resume.get("session_id"))
    got = _session_id((result or {}).get("session_id"))
    if expected is None or got != expected:
        return (
            "FAILED",
            f"{RESUME_MISMATCH}: Copilot reported a different session than the "
            "one resumed",
        )
    return status, error
