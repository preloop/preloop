"""External hand-off boundaries shared by delivery, notifications and MCP."""

import json
import re
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit

from preloop.models.schemas.external_approval import ExternalApprovalResult

# The replay binding is server-owned metadata, never a tool argument.
scope_replay: ContextVar[dict[str, Any] | None] = ContextVar(
    "scope_replay", default=None
)
handoff_reference: ContextVar[str | None] = ContextVar(
    "handoff_reference", default=None
)


def workflow_config(workflow: Any) -> dict[str, Any]:
    """Read optional settings without interpreting missing attributes as true."""
    value = getattr(workflow, "approval_config", None)
    return value if isinstance(value, dict) else {}


def uses_external_link(workflow: Any) -> bool:
    """External response parsing and deferred notifications are opt-in."""
    return workflow_config(workflow).get("external_approval_link") is True


def external_url(request: Any) -> str | None:
    """Return only a persisted string, preserving its exact representation."""
    value = getattr(request, "external_url", None)
    return value if isinstance(value, str) and value else None


def hide_agent_comment(request: Any, workflow: Any = None) -> bool:
    """Approval free text is private whenever the workflow uses external links."""
    workflow = (
        workflow
        if workflow is not None
        else getattr(request, "approval_workflow", None)
    )
    return workflow_config(workflow).get("expose_comment_to_agent") is False or (
        (external_url(request) or uses_external_link(workflow))
        and request.status != "declined"
    )


def agent_visible_comment(request: Any) -> str | None:
    """Apply the same comment policy to native hooks, legacy MCP and resumes."""
    if hide_agent_comment(request):
        return None
    return getattr(request, "approver_comment", None)


def allowed_hosts(workflow: Any, webhook_url: str) -> tuple[str, ...]:
    """Use exact ASCII DNS hosts; no wildcards, suffix matches, or URL entries."""
    configured = workflow_config(workflow).get("external_approval_hosts")
    hosts = [urlsplit(webhook_url).hostname] if configured is None else configured
    if not isinstance(hosts, (list, tuple)) or not hosts or len(hosts) > 32:
        raise ValueError("External approval host allow-list is invalid")
    result = []
    for host in hosts:
        if not isinstance(host, str) or not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host
        ):
            raise ValueError("External approval allow-list requires exact DNS hosts")
        if ".." in host:
            raise ValueError("External approval host is invalid")
        result.append(host.lower())
    return tuple(result)


def parse_handoff(body: bytes, hosts: tuple[str, ...]) -> dict[str, str] | None:
    """Validate an untrusted webhook answer without rewriting its URL.

    URL credentials, controls, backslashes and nonstandard ports are refused
    to avoid browser/parser disagreements. An allowed service can still redirect
    a browser: operators must trust that host's redirect policy.
    """
    if len(body) > 16384:
        raise ValueError("External approval response exceeds 16 KiB")
    if not body:
        return None
    try:
        data = json.loads(body)
    except RecursionError:
        raise ValueError("External approval response is too deeply nested") from None
    if not isinstance(data, dict):
        raise ValueError("External approval response must be an object")
    if "external_url" not in data and "external_reference" not in data:
        return None
    url = data.get("external_url")
    reference = data.get("external_reference")
    ExternalApprovalResult(external_reference=reference)
    if not reference or not isinstance(url, str) or not url or len(url) > 8192:
        raise ValueError("External approval response requires a reference and URL")
    if any(ord(c) <= 32 or ord(c) >= 127 for c in url) or any(
        c in url for c in '\\"<>`'
    ):
        raise ValueError("External approval URL contains unsafe characters")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.hostname not in hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.hostname.endswith(".")
    ):
        raise ValueError("External approval URL is not on the HTTPS host allow-list")
    # References appear in a tool error, so they cannot contain control characters.
    if any(ord(c) < 32 or ord(c) == 127 for c in reference):
        raise ValueError("External reference contains control characters")
    return {"external_reference": reference, "external_url": url}


def scope_refusal(value: Any) -> bool:
    """Recognize an actual HTTP 403 carrying RFC 6750 insufficient_scope."""
    from preloop.services.mcp_client_pool import (
        _unwrap_exception_group,
        MCPAuthenticationError,
    )

    if isinstance(value, BaseException):
        value = _unwrap_exception_group(value)
        if isinstance(value, MCPAuthenticationError):
            return value.status_code == 403 and value.insufficient_scope
        response = getattr(value, "response", None)
        if response is None or response.status_code != 403:
            return False
        challenge = response.headers.get("www-authenticate", "")
        if re.search(r'\berror\s*=\s*"insufficient_scope"', challenge, re.IGNORECASE):
            return True
        try:
            body = response.json()
        except (ValueError, RuntimeError):
            return False
    else:
        if not getattr(value, "is_error", False):
            return False
        body = getattr(value, "structured_content", None)
        if not isinstance(body, dict):
            return False
        error = body.get("error")
        status = body.get("status", body.get("status_code"))
        if isinstance(error, dict):
            status = error.get("status", error.get("status_code", status))
        if status != 403:
            return False
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    return (
        error == "insufficient_scope"
        or (isinstance(error, dict) and error.get("code") == "insufficient_scope")
        or body.get("code") == "insufficient_scope"
    )


def outcome_error(outcome: str, reference: str | None = None) -> str:
    """Expose the outcome and correlation reference, never upstream text."""
    return f"External approval {outcome}" + (
        f" (reference: {reference})" if reference else ""
    )
