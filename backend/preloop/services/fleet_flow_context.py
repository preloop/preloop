"""Bounded ID/reference projection for live Fleet Map flow activity.

Never selects raw payloads, titles, prompts, log lines or model output.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from preloop.utils.secret_scrubbing import scrub_secrets


def _text(value: Any, limit: int) -> str | None:
    """Scrub and bound existing display metadata, rejecting other values."""
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(scrub_secrets(value).replace("\x00", " ").split())[:limit]


def _url(value: Any) -> str | None:
    """Keep an HTTP object URL without credentials, query or fragment."""
    if not isinstance(value, str) or len(value) > 2000:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            return None
        clean = urlunsplit(
            (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", "")
        )
    except ValueError:
        return None
    return _text(clean, 1000)


def build_fleet_flow_context(
    *,
    flow_name: Any,
    trigger_details: Any,
    trigger_event_id: Any = None,
    parent_execution_id: Any = None,
    root_execution_id: Any = None,
    retry_of_execution_id: Any = None,
    resume_execution_id: Any = None,
) -> dict[str, Any]:
    """Project already-loaded run metadata without reading the database.

    The subject is an allowlist from the persisted compact subject only.
    Resume points forward to the continuation, while parent/retry point back.
    """
    details = trigger_details if isinstance(trigger_details, Mapping) else {}
    raw_subject = details.get("_subject")
    subject = raw_subject if isinstance(raw_subject, Mapping) else {}
    raw_resume = details.get("_resume")
    resume = raw_resume if isinstance(raw_resume, Mapping) else {}
    raw_feedback = details.get("_feedback")
    feedback = raw_feedback if isinstance(raw_feedback, Mapping) else {}
    projected = {
        key: text
        for key, limit in (
            ("repo", 200),
            ("reference", 160),
            ("event", 80),
            ("commit", 64),
        )
        if (text := _text(subject.get(key), limit)) is not None
    }
    if url := _url(subject.get("url")):
        projected["url"] = url
    return {
        "flow_name": _text(flow_name, 160),
        "trigger_source": _text(details.get("source"), 40),
        "trigger_kind": _text(details.get("type"), 80),
        "trigger_event_id": _text(trigger_event_id, 200),
        "subject": projected,
        "thread_id": _text(
            details.get("_thread_id") or details.get("_session_thread_id"), 64
        ),
        "resume_from_execution_id": _text(resume.get("execution_id"), 64),
        "resume_root_execution_id": _text(resume.get("resume_root"), 64),
        "resume_pr_url": _url(resume.get("pr_url") or feedback.get("pr_url")),
        "feedback_head_sha": _text(feedback.get("head_sha"), 64),
        "parent_execution_id": str(parent_execution_id)
        if parent_execution_id
        else None,
        "root_execution_id": str(root_execution_id) if root_execution_id else None,
        "retry_of_execution_id": (
            str(retry_of_execution_id) if retry_of_execution_id else None
        ),
        "resume_execution_id": str(resume_execution_id)
        if resume_execution_id
        else None,
    }
