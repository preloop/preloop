"""Dynamic FastMCP extension that provides per-user tool filtering.

This extends FastMCP to support dynamic tool lists based on authenticated user context
while keeping FastMCP's proven StreamableHTTP transport implementation.

Phase 1B: Added support for proxied tools from external MCP servers.
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import copy
import hashlib
import inspect
import json
import keyword
import logging
import threading
import uuid
from contextvars import ContextVar
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

import httpx
from fastmcp import FastMCP
from fastmcp.tools import Tool
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from preloop.services.dynamic_mcp_server import (
    UserContext,
    has_tracker,
    get_tracker_types,
)
from preloop.services.mcp_client_pool import get_mcp_client_pool
from preloop.models.schemas.grant_introspection import IntrospectionConfig
from preloop.services.grant_introspection import GrantResult, grant_introspector
from preloop.models.crud import crud_account, crud_mcp_server, crud_tool_configuration
from preloop.services.mcp_tool_collisions import (
    exposed_tool_name,
    upstream_tool_name as _upstream_tool_name,
)
from preloop.models.db.session import get_db_session as get_db
from preloop.api.endpoints.tools import BUILTIN_TOOLS, TOOL_NAME_ALIASES
from preloop.services import kill_switch as kill_switch_service
from preloop.services.subject_governance import (
    _tool_enabled_override_names,
    get_scoped_tool_rules,
    is_tool_enabled_for_subject,
)
from preloop.services.sensitive_data import tool_policy as sensitive_tool_policy
from preloop.services.sensitive_data.storage import (
    StorageScope,
    apply_storage_redaction,
    attach_result_to_reference,
)
from preloop.services.sensitive_data.storage import (
    cached_config as apply_storage_redaction_config,
)
from preloop.utils.redaction import redact_dict

logger = logging.getLogger(__name__)


def _deprecated_alias_names() -> frozenset[str]:
    """Default-disabled builtin names that ``TOOL_NAME_ALIASES`` still exposes.

    The map is symmetric (``search`` ↔ ``search_issues``). Only the
    default-disabled side is hidden until a policy names it. Dropping the
    map entries in 0.18.0 removes this special case from the list and call
    filters without another edit at those sites.
    """
    default_enabled = {
        str(tool.get("name")): bool(tool.get("default_enabled", True))
        for tool in BUILTIN_TOOLS
    }
    return frozenset(
        name
        for name, alias in TOOL_NAME_ALIASES.items()
        if alias and alias != name and not default_enabled.get(name, True)
    )


# Derived once: the alias map and the builtin catalogue are import-time constants.
DEPRECATED_ALIAS_NAMES = _deprecated_alias_names()


def _rule_enables_deprecated_alias(rule: Any) -> bool:
    """True when a scoped rule should advertise a default-disabled alias.

    A rule that is switched off, or that denies the tool, names it without
    offering it. Calls stay gated by policy evaluation either way.
    """
    if not isinstance(rule, dict):
        return False
    if not rule.get("is_enabled", True):
        return False
    return rule.get("action") != "deny"


def _policy_enables_deprecated_alias(
    meta_data: Any,
    *,
    tool_name: str,
    subject_context: dict[str, Any],
) -> bool:
    """Whether a scoped policy should surface ``tool_name`` as an alias."""
    if tool_name not in DEPRECATED_ALIAS_NAMES:
        return False
    rules = get_scoped_tool_rules(
        meta_data, tool_name=tool_name, subject_context=subject_context
    )
    return any(_rule_enables_deprecated_alias(rule) for rule in rules)


def _tool_error_result(text: str) -> ToolResult:
    """Return an MCP error result so refusals survive output-schema checks."""
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        is_error=True,
    )


def _tool_result_error_text(result: Any) -> Optional[str]:
    """Extract the first text block from an error ToolResult, if any."""
    if result is None or not getattr(result, "is_error", False):
        return None
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            return str(text)
    return None


def _hash_arguments(arguments: Optional[dict[str, Any]]) -> str:
    """Return a short sha256 of redacted arguments for loop-detection signatures.

    Only the first 16 hex characters are kept. The hash distinguishes same-shape
    calls (e.g. get_pr(123) vs get_pr(124)) without persisting argument values.
    """
    payload = json.dumps(redact_dict(arguments or {}), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _reference_only_in_scope(
    account_id: Any,
    *,
    tool_name: str,
    server_name: Optional[str],
    managed_agent_id: Optional[str],
    config: Any = None,
) -> bool:
    """True when a reference-only rule covers this call (#1124).

    Fails toward privacy: when the account block cannot be read the call
    is treated as covered, so no unsalted argument hash is stored.
    """
    try:
        from preloop.services.sensitive_data import reference as reference_module
        from preloop.services.sensitive_data import storage as storage_module

        if config is None:
            if storage_module.has_cached_config(account_id):
                config = storage_module.cached_config(account_id)
            else:
                config = storage_module._load_config(account_id)
                storage_module.prime_cache(account_id, config)
        if config is None:
            return False
        return (
            reference_module.reference_rule_for(
                config,
                tool_name=tool_name,
                server_name=server_name,
                managed_agent_id=managed_agent_id,
            )
            is not None
        )
    except Exception:  # noqa: BLE001 - never store the hash on doubt
        return True


# A usage row is a timeline entry, not an audit log of contents: keep the
# per-key argument sizes bounded and never store the values themselves.
MAX_ARGUMENT_SUMMARY_KEYS = 50
MAX_ARGUMENT_KEY_LENGTH = 120
MAX_TOOL_CALL_SUMMARY_LENGTH = 500

# Outcome vocabulary for one governed tool call. ``succeeded`` is the current
# spelling; ``success`` is kept as a success for rows written before the
# outcome was split into succeeded/refused/failed.
TOOL_CALL_STATUS_SUCCEEDED = "succeeded"
TOOL_CALL_STATUS_REFUSED = "refused"
TOOL_CALL_STATUS_FAILED = "failed"


def _summarize_arguments(arguments: Optional[dict[str, Any]]) -> dict[str, int]:
    """Return a bounded ``{top-level key: byte size}`` map.

    The usage row must let an operator see that a call was oversized or
    malformed without retaining the payload, so only the names of the
    top-level keys and the serialized size of each value are recorded. The
    number of keys and the key length are capped; any overflow is folded into
    an ``"..."`` entry carrying the count of keys that were omitted.
    """
    if not arguments:
        return {}
    summary: dict[str, int] = {}
    items = list(arguments.items())
    for key, value in items[:MAX_ARGUMENT_SUMMARY_KEYS]:
        try:
            size = len(json.dumps(value, default=str))
        except (TypeError, ValueError):
            size = len(str(value))
        summary[str(key)[:MAX_ARGUMENT_KEY_LENGTH]] = size
    overflow = len(items) - MAX_ARGUMENT_SUMMARY_KEYS
    if overflow > 0:
        summary["..."] = overflow
    return summary


def _bounded_summary(text: Optional[str]) -> Optional[str]:
    """Keep a usage row's error/result string small enough for a timeline."""
    if not text:
        return None
    return str(text)[:MAX_TOOL_CALL_SUMMARY_LENGTH]


def _configs_visible_to_caller(
    configs: Iterable[Any], caller_managed_agent_id: Optional[str]
) -> List[Any]:
    """Order ToolConfiguration rows by scope for the calling agent.

    A row with ``managed_agent_id`` set applies only to that managed agent:
    rows scoped to a *different* agent are dropped entirely (they must not
    leak enablement, disablement, or justification requirements to other
    callers), and rows scoped to the calling agent are returned *after* the
    account-wide rows so that dict-style ``{tool_name: ...}`` builds let the
    agent-scoped value win.

    Args:
        configs: ToolConfiguration rows for the account.
        caller_managed_agent_id: The calling agent's id (from the API key's
            context), or None for callers without an agent identity.

    Returns:
        The visible rows, account-wide first, caller-scoped last.
    """
    caller = str(caller_managed_agent_id) if caller_managed_agent_id else None
    account_rows: List[Any] = []
    agent_rows: List[Any] = []
    for tc in configs:
        row_agent = getattr(tc, "managed_agent_id", None)
        if row_agent is None:
            account_rows.append(tc)
        elif caller is not None and str(row_agent) == caller:
            agent_rows.append(tc)
    return account_rows + agent_rows


# Context variable to pass policy evaluation results from _call_tool() to
# individual tool wrappers (which call require_approval()).
# When set, require_approval() should use this workflow_id instead of looking
# it up from the tool configuration.
_rule_workflow_id_var: ContextVar[Optional[str]] = ContextVar(
    "_rule_workflow_id_var", default=None
)

# Context variable carrying the matched-rule snapshot from _call_tool()'s
# policy evaluation through to require_approval(), which persists it on the
# approval request. Without this the approver sees the tool and the arguments
# but not WHICH rule demanded approval, so a boundary case is indistinguishable
# from a mid-band one. Set in the same places as _rule_workflow_id_var and
# cleared on every non-approval path so a stale rule can never be attributed
# to a later call.
_rule_context_var: ContextVar[Optional[dict]] = ContextVar(
    "_rule_context_var", default=None
)

# Context variable to pass a unique correlation_id from _call_tool() through
# to all audit-logging helpers (policy_evaluator, approval_helper, tool execution).
# Every audit log entry from the same tool invocation shares this ID so the
# frontend can group them into a single timeline entry.
_correlation_id_var: ContextVar[Optional[str]] = ContextVar(
    "_correlation_id_var", default=None
)

# Outcome stamped by a proxied wrapper denial (refused/failed) so call_tool's
# finally can persist the right status when FastMCP returns an error ToolResult.
_tool_outcome_var: ContextVar[Optional[str]] = ContextVar(
    "_tool_outcome_var", default=None
)

# The raw MCP content list an upstream server returned for a proxied call,
# captured by the wrapper before output filters and stringification. The
# outer call_tool finally reads it once to derive a browser_step from a
# Playwright MCP call (and its screenshot image) and then clears it. The
# value handed back to the agent is not affected.
_proxied_raw_result_var: ContextVar[Optional[Any]] = ContextVar(
    "_proxied_raw_result_var", default=None
)


def _wrapper_tool_error(text: str, *, status: str) -> ToolResult:
    """Return a tool error and stamp the outcome for the outer call_tool finally.

    Proxied wrappers used to return plain strings; FastMCP wraps those as a
    successful ToolResult, so the usage row was recorded as succeeded. Stamp
    the intended outcome here so the finally block can persist refused/failed.
    """
    _tool_outcome_var.set(status)
    return _tool_error_result(text)


# Audit ``tool_call`` statuses. ``executed`` means the upstream (or builtin)
# tool ran and reported success; the others record why it did not.
AUDIT_TOOL_CALL_EXECUTED = "executed"
AUDIT_TOOL_CALL_UPSTREAM_ERROR = "upstream_error"
AUDIT_TOOL_CALL_DECLINED = "declined"
AUDIT_TOOL_CALL_FAILED = "failed"
# Approval still open (async polling or parked): not forwarded, not declined.
AUDIT_TOOL_CALL_PENDING_APPROVAL = "pending_approval"
_PENDING_APPROVAL_PAYLOAD_STATUSES = frozenset({"pending_approval", "parked_for_human"})

# Wrapper outcome stamped when the upstream server answered with an error.
_WRAPPER_OUTCOME_UPSTREAM_ERROR = "upstream_error"
_AUDIT_REASON_MAX_CHARS = 200

# (error_code, short reason) for the audit row, stamped by the wrapper.
_tool_error_detail_var: ContextVar[Optional[tuple[Optional[str], Optional[str]]]] = (
    ContextVar("_tool_error_detail_var", default=None)
)


def _short_reason(text: Any) -> Optional[str]:
    if text is None:
        return None
    text = " ".join(str(text).split())
    if not text:
        return None
    if len(text) > _AUDIT_REASON_MAX_CHARS:
        return text[: _AUDIT_REASON_MAX_CHARS - 3] + "..."
    return text


def _upstream_error_detail(
    content: Any, structured: Optional[dict]
) -> tuple[str, Optional[str]]:
    """Derive an error code and a short reason from an upstream error result."""
    code: Optional[str] = None
    reason: Optional[str] = None
    if isinstance(structured, dict):
        nested = structured.get("error")
        source = nested if isinstance(nested, dict) else structured
        for key in ("code", "error_code", "error"):
            value = source.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                code = str(value)
                break
        for key in ("message", "error_description", "detail", "reason"):
            value = source.get(key)
            if isinstance(value, str) and value:
                reason = value
                break
    if reason is None:
        for block in content or []:
            text = getattr(block, "text", None)
            if text:
                reason = text
                break
    return (_short_reason(code) or "tool_error"), _short_reason(reason)


def _proxied_upstream_result(text: str, upstream: Any) -> Any:
    """Return what the agent receives for a proxied upstream result.

    A plain success keeps the historical string. When the upstream set
    ``isError`` or ``structuredContent`` both are forwarded unchanged, so a
    refusal reaches the agent as an error, and the outcome is stamped for the
    audit row.
    """
    is_error = getattr(upstream, "is_error", False) is True
    structured = getattr(upstream, "structured_content", None)
    if not isinstance(structured, dict):
        structured = None
    if not is_error and structured is None:
        return text
    if is_error:
        _tool_outcome_var.set(_WRAPPER_OUTCOME_UPSTREAM_ERROR)
        _tool_error_detail_var.set(
            _upstream_error_detail(list(upstream or []), structured)
        )
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured,
        is_error=is_error,
    )


def _resolve_proxied_tool_server(db: Any, account_id: str, tool_name: str) -> Any:
    """Return the MCP server that serves ``tool_name`` for the account now.

    Proxied wrappers are registered once per ``account_<id>_<tool>`` on the
    process-wide server, so they must not carry a server id from the time
    they were created: a server deleted and recreated under the same name
    gets a new id. Resolving on every call reads the same rows as
    ``list_tools`` (own and shared active servers), so every pod routes to
    the current server.

    ``tool_name`` is the name agents see (``<tool_prefix>_<tool>`` when the
    server has a prefix). When several servers expose it, the first one
    wins (#1135): own servers before shared ones, then the oldest by
    ``created_at`` and ``id``. Newer servers' same-named tools are shadowed.
    If the owner's tool is disabled by configuration, the name is not
    served at all; it is not handed to a shadowed server.

    One indexed query for this tool name, not a full tool discovery, so it
    stays as cheap as the single-row lookup it replaces.
    """
    candidates = crud_mcp_server.get_active_visible_for_tool(
        db, account_id=account_id, tool_name=tool_name
    )
    if not candidates:
        return None
    owner, enabled = candidates[0]
    return owner if enabled else None


@dataclass(frozen=True)
class GrantDispatchSnapshot:
    """One resolved owner and copied configuration for this invocation only."""

    account_id: str
    tool_name: str
    server_name: str
    upstream_name: str
    client_config: dict[str, Any]


_grant_dispatch_var: ContextVar[Optional[GrantDispatchSnapshot]] = ContextVar(
    "_grant_dispatch_var", default=None
)


_grant_binding_var: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "_grant_binding_var", default=None
)


async def _evaluate_snapshot_grant(
    snapshot: GrantDispatchSnapshot,
) -> Optional[GrantResult]:
    """Check grant state for the token on the copied dispatch configuration."""
    auth = snapshot.client_config["auth_config"]
    if not isinstance(auth, dict) or auth.get("introspection") is None:
        return None
    config = IntrospectionConfig.model_validate(auth["introspection"])
    auth_type = snapshot.client_config["auth_type"]
    if auth_type not in {"bearer", "oauth"}:
        raise ValueError("introspection requires bearer or OAuth authentication")
    token = auth.get("token" if auth_type == "bearer" else "access_token")
    return await grant_introspector.evaluate(
        token if isinstance(token, str) else None,
        config,
        server_id=snapshot.client_config["server_id"],
    )


async def _prepare_grant_dispatch(
    account_id: str, tool_name: str
) -> tuple[Optional[GrantDispatchSnapshot], Optional[GrantResult]]:
    """Resolve through CRUD, release the DB, then introspect the forwarded token."""

    def load() -> Optional[GrantDispatchSnapshot]:
        db = next(get_db())
        try:
            server = _resolve_proxied_tool_server(db, account_id, tool_name)
            if server is None:
                return None
            return GrantDispatchSnapshot(
                account_id=account_id,
                tool_name=tool_name,
                server_name=server.name,
                upstream_name=_upstream_tool_name(
                    getattr(server, "tool_prefix", None), tool_name
                ),
                client_config={
                    "server_id": str(server.id),
                    "url": server.url,
                    "auth_type": server.auth_type,
                    "auth_config": deepcopy(server.auth_config),
                    "transport": server.transport,
                },
            )
        finally:
            db.close()

    snapshot = await asyncio.wait_for(asyncio.to_thread(load), timeout=30)
    if snapshot is None:
        return None, None
    return snapshot, await _evaluate_snapshot_grant(snapshot)


def _audit_grant_kwargs(
    audit_service: Any, grant: Optional[dict[str, Any]]
) -> dict[str, Any]:
    """Keep older optional audit plugins compatible until their paired upgrade."""
    if grant is None:
        return {}
    try:
        params = inspect.signature(audit_service.log_tool_call_async).parameters
    except (TypeError, ValueError):
        params = {}
    if "grant" in params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    ):
        return {"grant": grant}
    _warn_dropped_audit_field("grant")
    return {}


def _record_grant_denial(
    account_id: str,
    tool_name: str,
    grant: GrantResult,
    *,
    user_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> None:
    """Record the hard grant gate through the existing policy-denial audit path."""
    from preloop.services.policy_evaluator import _log_policy_decision_async

    _log_policy_decision_async(
        account_id=uuid.UUID(account_id),
        tool_name=tool_name,
        action="deny",
        rule_description=grant.deny_reason or "introspection_unavailable",
        condition_matched=None,
        tool_args={},
        user_id=uuid.UUID(user_id) if user_id else None,
        correlation_id=correlation_id,
        extra_details={"grant": grant.binding},
    )


def _proxied_exception_outcome(
    cause: BaseException, *, server_id: Optional[str], unavailable: bool
) -> str:
    """Classify a raised proxied call, stamp audit detail, record last_error.

    An upstream HTTP status (e.g. 401) is an ``upstream_error`` with an
    ``http_<status>`` code; anything else is ``failed``. Transport and auth
    failures are written to the MCP server's ``last_error``.
    """
    status_code = getattr(getattr(cause, "response", None), "status_code", None)
    reason = _short_reason(cause) or type(cause).__name__
    if isinstance(cause, httpx.HTTPStatusError) and isinstance(status_code, int):
        outcome = _WRAPPER_OUTCOME_UPSTREAM_ERROR
        code = f"http_{status_code}"
    else:
        outcome = TOOL_CALL_STATUS_FAILED
        code = "unavailable" if unavailable else type(cause).__name__
    _tool_error_detail_var.set((code, reason))
    if server_id and (unavailable or status_code in (401, 403)):
        _record_mcp_server_error(server_id, f"{code}: {reason}")
    return outcome


def _record_mcp_server_error(server_id: str, message: str) -> None:
    """Best effort: store a transport/auth failure on the MCP server row."""
    try:
        db = next(get_db())
        try:
            server = crud_mcp_server.get(db, id=server_id)
            if server is not None:
                server.last_error = message[:2000]
                db.commit()
        finally:
            db.close()
    except Exception:
        logger.debug("Failed to record MCP server last_error", exc_info=True)


def _is_pending_approval_payload(text: Optional[str]) -> bool:
    """True for the async-polling / parked payload ``require_approval`` returns."""
    if not text or not text.lstrip().startswith("{"):
        return False
    try:
        payload = json.loads(text)
    except ValueError:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("status") in _PENDING_APPROVAL_PAYLOAD_STATUSES
    )


def _audit_tool_call_outcome(
    *,
    exec_status: str,
    exec_error: Optional[str],
    wrapper_outcome: Optional[str],
    result: Any,
    error_detail: Optional[tuple[Optional[str], Optional[str]]],
) -> tuple[str, Optional[str], Optional[str]]:
    """Return (audit status, error code, short reason) for a governed call."""
    code, reason = error_detail or (None, None)
    if exec_status == "failed":
        return AUDIT_TOOL_CALL_FAILED, code, reason or _short_reason(exec_error)
    full_error_text = _tool_result_error_text(result)
    error_text = _short_reason(full_error_text)
    if wrapper_outcome == _WRAPPER_OUTCOME_UPSTREAM_ERROR:
        return AUDIT_TOOL_CALL_UPSTREAM_ERROR, code, reason or error_text
    if wrapper_outcome == TOOL_CALL_STATUS_REFUSED and _is_pending_approval_payload(
        full_error_text
    ):
        return AUDIT_TOOL_CALL_PENDING_APPROVAL, None, None
    if wrapper_outcome == TOOL_CALL_STATUS_REFUSED:
        return AUDIT_TOOL_CALL_DECLINED, code, reason or error_text
    if wrapper_outcome == TOOL_CALL_STATUS_FAILED or error_text is not None:
        return AUDIT_TOOL_CALL_FAILED, code, reason or error_text
    return AUDIT_TOOL_CALL_EXECUTED, None, None


def post_approval_exec_outcome(tool_result: Any) -> tuple[str, Optional[str]]:
    """Status and error for a tool replayed after approval (async-poll path).

    That path calls the tool without the governed ``call_tool`` finally, so
    read and clear the wrapper stamps here: an upstream ``isError`` result is
    ``upstream_error``, any other error result is ``failed``.
    """
    wrapper_outcome = _tool_outcome_var.get(None)
    code, reason = _tool_error_detail_var.get(None) or (None, None)
    _tool_outcome_var.set(None)
    _tool_error_detail_var.set(None)
    if getattr(tool_result, "is_error", False) is not True:
        return AUDIT_TOOL_CALL_EXECUTED, None
    reason = reason or _short_reason(_tool_result_error_text(tool_result))
    error = f"{code}: {reason}" if code and reason else (code or reason)
    if wrapper_outcome == _WRAPPER_OUTCOME_UPSTREAM_ERROR:
        return AUDIT_TOOL_CALL_UPSTREAM_ERROR, error
    return AUDIT_TOOL_CALL_FAILED, error


_DROPPED_AUDIT_ERROR_FIELDS: set[str] = set()
_DROPPED_AUDIT_ERROR_FIELDS_LOCK = threading.Lock()


def _warn_dropped_audit_field(field: str) -> None:
    """Warn once per field without including upstream content or credentials."""
    with _DROPPED_AUDIT_ERROR_FIELDS_LOCK:
        if field in _DROPPED_AUDIT_ERROR_FIELDS:
            return
        _DROPPED_AUDIT_ERROR_FIELDS.add(field)
    logger.warning(
        "Audit service does not accept %s; error detail was dropped",
        field,
    )


def _audit_error_kwargs(
    audit_service: Any, error_code: Optional[str], error_reason: Optional[str]
) -> dict[str, Any]:
    """Pass error_code/error_reason only to audit services that accept them."""
    if error_code is None and error_reason is None:
        return {}
    try:
        params = inspect.signature(audit_service.log_tool_call_async).parameters
    except (TypeError, ValueError):
        for key, value in (("error_code", error_code), ("error_reason", error_reason)):
            if value is not None:
                _warn_dropped_audit_field(key)
        return {}
    accepts_any = any(p.kind is p.VAR_KEYWORD for p in params.values())
    out: dict[str, Any] = {}
    for key, value in (("error_code", error_code), ("error_reason", error_reason)):
        if value is not None and (accepts_any or key in params):
            out[key] = value
        elif value is not None:
            _warn_dropped_audit_field(key)
    return out


BUILTIN_SERVER_NAME = "preloop-mcp"


def _load_sensitive_data_policy(account_id: str):
    """Load the account's ``sensitive_data`` block and detector config (sync).

    Strict read: a database error or a malformed stored block raises, and
    the caller refuses the call. "No rules" must mean the operator wrote
    none, never that the policy could not be read.
    """
    from preloop.services.sensitive_data.policy_store import (
        detector_config_from,
        load_sensitive_data_config,
    )

    from preloop.services.sensitive_data.storage import prime_cache

    db = next(get_db())
    try:
        config = load_sensitive_data_config(db, account_id, strict=True)
    finally:
        db.close()
    # This read happened off the event loop; the storage hooks in the audit
    # and activity writers reuse it instead of reading again on the loop.
    prime_cache(account_id, config)
    if not config.has_tool_rules():
        return None, None
    return config, detector_config_from(config)


def _resolve_approval_workflow_id(
    account_id: str, workflow_name: Optional[str]
) -> Optional[str]:
    """Workflow id for a sensitive-data rule: by name, else the account default."""
    from preloop.models.crud import crud_approval_workflow

    db = next(get_db())
    try:
        if workflow_name:
            workflow = crud_approval_workflow.get_by_name(
                db, account_id=account_id, name=workflow_name
            )
            return str(workflow.id) if workflow else None
        workflow = crud_approval_workflow.get_default(db, account_id=account_id)
        return str(workflow.id) if workflow else None
    finally:
        db.close()


def _sensitive_denial_text(outcome: Any, *, where: str) -> str:
    """Refusal shown to the agent. Types only, never the matched values."""
    summary = outcome.detector_summary()
    types = ", ".join(summary.get("pii.types_found") or []) or "sensitive data"
    if summary.get("detector_timeout"):
        return (
            f"Access denied: sensitive data rule '{outcome.rule_id}' could not "
            f"scan {where} in time (fail closed)."
        )
    return (
        f"Access denied: sensitive data rule '{outcome.rule_id}' matched "
        f"{where} (types: {types})."
    )


# Context variable to pass justification extracted from tool arguments
# through to require_approval(). The justification is injected into the tool
# schema by _list_tools() and popped from arguments in _call_tool() before
# the actual tool function is invoked.
_justification_var: ContextVar[Optional[str]] = ContextVar(
    "_justification_var", default=None
)

# Context variable to bypass approval checks during re-execution of an
# already-approved async tool call.  Set by get_approval_status() before
# replaying the tool so that require_approval() returns (True, "") immediately.
_bypass_approval_var: ContextVar[bool] = ContextVar(
    "_bypass_approval_var", default=False
)

# The approver's comment from the already-approved request being re-executed.
# Set by get_approval_status() alongside _bypass_approval_var so tools that
# consume the comment as their result (ask_user: the comment IS the human's
# answer) do not lose it when require_approval() short-circuits during replay.
_approved_comment_var: ContextVar[Optional[str]] = ContextVar(
    "_approved_comment_var", default=None
)

# The validated form answer and the approval id of the request being
# re-executed. A structured ask_user returns JSON built from both, so an
# async-approval replay has to carry them the same way it carries the
# comment; without them the agent would get an answer with no provenance.
_approved_answer_var: ContextVar[Optional[dict]] = ContextVar(
    "_approved_answer_var", default=None
)
_approved_id_var: ContextVar[Optional[str]] = ContextVar(
    "_approved_id_var", default=None
)

# Context variable to ensure internal proxied tool names are only called via proxy translation
_is_proxy_translation_var: ContextVar[bool] = ContextVar(
    "_is_proxy_translation_var", default=False
)


def _strip_fields_from_json_text(text: str, dropped_fields: set[str]) -> Optional[str]:
    """Strip top-level fields from a JSON object or list-of-objects string.

    Args:
        text: Raw text that may contain a JSON document.
        dropped_fields: Top-level keys to remove from each result object.

    Returns:
        The re-serialized JSON with the requested keys removed, or ``None`` if
        the text is not JSON, is not a dict/list-of-dicts, or nothing changed.
    """
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None

    changed = False

    def _strip_obj(obj: object) -> object:
        nonlocal changed
        if isinstance(obj, dict):
            for field in dropped_fields:
                if field in obj:
                    del obj[field]
                    changed = True
        return obj

    if isinstance(parsed, dict):
        _strip_obj(parsed)
    elif isinstance(parsed, list):
        for item in parsed:
            _strip_obj(item)
    else:
        # Scalars / strings: nothing to strip.
        return None

    if not changed:
        return None
    return json.dumps(parsed)


def apply_output_filters(
    result_items: list,
    *,
    account_id: str,
    tool_name: str,
    server_name: Optional[str] = None,
    managed_agent_id: Optional[str] = None,
) -> list:
    """Strip operator-configured fields from a tool result before the agent sees it.

    Looks up enabled :class:`ToolOutputFilter` rows that match the current
    call and removes the union of their ``dropped_fields`` from every
    JSON-parseable result content block (a dict, or a list of dicts). This
    trims wasted context tokens without changing the upstream tool.

    The function is fully defensive: if no filters match, content is not JSON,
    parsing fails, or anything raises, the original ``result_items`` is
    returned unchanged. It never raises into the proxy path.

    Args:
        result_items: The MCP content blocks returned by the upstream tool.
            Each item is expected to expose a ``.text`` attribute holding the
            block's text (e.g. ``TextContent``); other items pass through.
        account_id: Owning account id for the current call.
        tool_name: Name of the tool that produced the result.
        server_name: MCP server name for the current call, if known.
        managed_agent_id: Managed agent id for the current call, if known.

    Returns:
        The (possibly trimmed) list of content blocks.
    """
    try:
        if not result_items:
            return result_items

        from preloop.models.crud import crud_tool_output_filter
        from preloop.models.db.session import get_db_session as _get_db

        db = next(_get_db())
        try:
            filters = crud_tool_output_filter.list_active_for_tool(
                db,
                account_id=account_id,
                tool_name=tool_name,
                server_name=server_name,
                managed_agent_id=managed_agent_id,
            )
        finally:
            db.close()

        if not filters:
            return result_items

        dropped_fields: set[str] = set()
        for flt in filters:
            for field in flt.dropped_fields or []:
                if isinstance(field, str):
                    dropped_fields.add(field)

        if not dropped_fields:
            return result_items

        applied = False
        for item in result_items:
            text = getattr(item, "text", None)
            if not isinstance(text, str):
                continue
            stripped = _strip_fields_from_json_text(text, dropped_fields)
            if stripped is not None:
                item.text = stripped
                applied = True

        if applied:
            logger.info(
                "Applied output filter(s) to tool '%s' (server=%s, account=%s): "
                "stripped fields %s",
                tool_name,
                server_name,
                account_id,
                sorted(dropped_fields),
            )

        return result_items
    except Exception as exc:  # noqa: BLE001 - never break the proxy path
        logger.debug(
            "apply_output_filters skipped for tool '%s' due to error: %s",
            tool_name,
            exc,
        )
        return result_items


#: Python annotations used for the wrapped function signature of a proxied
#: tool. FastMCP validates ``tools/call`` arguments against that signature, so
#: entries must accept every value the upstream ``inputSchema`` allows --
#: anything rejected here never reaches the upstream server.
_JSON_SCHEMA_PYTHON_TYPES: Dict[str, str] = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "List[Any]",
    "object": "Dict[str, Any]",
}


def _schema_type_names(param_def: Dict[str, Any]) -> List[str]:
    """Collect the primitive JSON Schema type names a property allows.

    Handles both ``"type": "array"`` and the union form upstream servers
    commonly emit for nullable properties, ``"type": ["null", "array"]``, as
    well as ``anyOf``/``oneOf`` alternatives.

    Args:
        param_def: JSON Schema fragment for a single tool parameter.

    Returns:
        De-duplicated type names in declaration order; empty when the schema
        does not declare a primitive type.
    """
    raw_type = param_def.get("type")
    if isinstance(raw_type, str):
        return [raw_type]
    if isinstance(raw_type, list):
        names = [t for t in raw_type if isinstance(t, str)]
        return list(dict.fromkeys(names))

    # `anyOf`/`oneOf` are the other common way nullable unions are expressed.
    for key in ("anyOf", "oneOf"):
        alternatives = param_def.get(key)
        if not isinstance(alternatives, list):
            continue
        names: List[str] = []
        for alternative in alternatives:
            if not isinstance(alternative, dict):
                continue
            alternative_type = alternative.get("type")
            if isinstance(alternative_type, str):
                names.append(alternative_type)
            elif isinstance(alternative_type, list):
                names.extend(t for t in alternative_type if isinstance(t, str))
        if names:
            return list(dict.fromkeys(names))

    return []


def _python_type_for_schema(param_def: Dict[str, Any]) -> str:
    """Map a JSON Schema parameter to a Python annotation.

    The annotation feeds the generated wrapper signature that FastMCP uses to
    validate proxied ``tools/call`` arguments, so it must be at least as
    permissive as the schema advertised in ``tools/list``. Unknown or
    unrepresentable shapes fall back to ``Any`` (forwarded unchanged) instead
    of the previous ``str`` default, which rejected every array argument whose
    schema used a union type such as ``["null", "array"]``.

    Args:
        param_def: JSON Schema fragment for a single tool parameter.

    Returns:
        A Python annotation expression, e.g. ``str``, ``List[Any]`` or
        ``Optional[List[Any]]``.
    """
    type_names = _schema_type_names(param_def)
    if not type_names:
        return "Any"

    nullable = "null" in type_names
    mapped: List[str] = []
    for type_name in type_names:
        if type_name == "null":
            continue
        python_type = _JSON_SCHEMA_PYTHON_TYPES.get(type_name)
        if python_type is None:
            # A shape we cannot express (e.g. a custom keyword); accept
            # anything rather than block a call the upstream would allow.
            return "Any"
        if python_type not in mapped:
            mapped.append(python_type)

    if not mapped:
        # Only `null` was declared, so any value is acceptable.
        return "Any"
    if len(mapped) == 1:
        base = mapped[0]
        return f"Optional[{base}]" if nullable else base
    union = ", ".join(mapped)
    return f"Optional[Union[{union}]]" if nullable else f"Union[{union}]"


def _optional_annotation(annotation: str) -> str:
    """Wrap an annotation in ``Optional[...]`` unless it already allows None.

    Args:
        annotation: Python annotation expression.

    Returns:
        The annotation, made nullable exactly once.
    """
    if annotation == "Any":
        return annotation
    if annotation.startswith("Optional["):
        return annotation
    return f"Optional[{annotation}]"


#: Keys supplied to ``exec()`` when generating a proxied-tool wrapper. The
#: generated body reads these as globals (``tool_name``, ``param_names``,
#: ``account_id``, ...). An upstream property with the same name would become
#: a function parameter and shadow the trusted value for the whole body.
_WRAPPER_NAMESPACE_KEYS = (
    "self",
    "account_id",
    "tool_name",
    "_resolve_proxied_tool_server",
    "_upstream_tool_name",
    "_grant_dispatch_var",
    "_grant_binding_var",
    "deepcopy",
    "_prepare_grant_dispatch",
    "_record_grant_denial",
    "param_names",
    "logger",
    "get_db",
    "crud_mcp_server",
    "get_mcp_client_pool",
    "apply_output_filters",
    "Optional",
    "Union",
    "Any",
    "List",
    "Dict",
    "Context",
    "_rule_workflow_id_var",
    "_correlation_id_var",
    "_proxied_raw_result_var",
    "_wrapper_tool_error",
    "_proxied_upstream_result",
    "_proxied_exception_outcome",
)

#: Locals assigned in the generated wrapper body before argument collection.
#: Colliding parameter names would make ``locals().get(param_name)`` forward
#: the body's own object instead of the caller-supplied argument.
_RESERVED_WRAPPER_BODY_LOCALS = frozenset(
    {
        "ctx",
        "snapshot",
        "grant",
        "arguments",
        "user_context",
        "param_name",
        "value",
        "server_id",
        "upstream_name",
    }
)

#: Builtins the generated wrapper body calls. An upstream property with one
#: of these names would become a parameter and shadow the builtin (``type(ctx)``
#: becomes ``TypeError``). They must not be interpolated raw; alias instead.
_WRAPPER_BODY_BUILTINS = (
    "type",
    "next",
    "list",
    "str",
    "isinstance",
    "getattr",
    "hasattr",
    "locals",
    "Exception",
)

_RESERVED_WRAPPER_LOCALS = (
    frozenset(_WRAPPER_NAMESPACE_KEYS)
    | _RESERVED_WRAPPER_BODY_LOCALS
    | frozenset(_WRAPPER_BODY_BUILTINS)
)

_WRAPPER_BODY_BUILTIN_SET = frozenset(_WRAPPER_BODY_BUILTINS)


def _is_safe_tool_identifier(name: str) -> bool:
    """Return whether *name* is safe as a proxied tool name in ``internal_name``.

    Tool names are interpolated only inside the ``account_<id>_`` prefix, so
    they cannot shadow wrapper locals, namespace globals, or builtins. Only
    identifier syntax and keywords matter here.

    Args:
        name: Candidate tool name from an upstream MCP server.

    Returns:
        True if *name* may be used in a generated wrapper function name.
    """
    return isinstance(name, str) and name.isidentifier() and not keyword.iskeyword(name)


def _is_safe_generated_identifier(name: str) -> bool:
    """Return whether *name* is safe to interpolate raw as a parameter name.

    Upstream ``tools/list`` names are attacker-controlled. Generated wrappers
    ``exec()`` a function whose signature interpolates those names, so anything
    that is not a non-keyword identifier, that collides with locals in the
    generated body, that shadows an exec-namespace global, or that shadows a
    builtin the body calls, must not be interpolated raw.

    Args:
        name: Candidate parameter name from an upstream MCP server.

    Returns:
        True if *name* may be interpolated into generated Python source.
    """
    return _is_safe_tool_identifier(name) and name not in _RESERVED_WRAPPER_LOCALS


def _unused_wrapper_alias(base: str, taken: set[str]) -> str:
    """Return a unique identifier derived from *base* that is safe to interpolate.

    Args:
        base: Original upstream parameter name (a wrapper-body builtin).
        taken: Names already used by the schema or earlier aliases.

    Returns:
        A unique alias such as ``type_`` or ``type__``.
    """
    candidate = f"{base}_"
    while (
        candidate in taken
        or candidate in _RESERVED_WRAPPER_LOCALS
        or keyword.iskeyword(candidate)
        or not candidate.isidentifier()
    ):
        candidate = f"{candidate}_"
    return candidate


class DynamicFastMCP(FastMCP):
    """FastMCP extension with per-user dynamic tool filtering.

    This subclass overrides FastMCP's tool listing and execution to provide
    per-request filtering based on authenticated user context. It keeps all
    of FastMCP's StreamableHTTP transport functionality while adding dynamic
    tool visibility.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._user_context_provider: Optional[Callable[[], Optional[UserContext]]] = (
            None
        )
        # Track proxied tool -> server mapping for routing
        self._proxied_tool_servers: Dict[str, str] = {}
        self._proxied_tool_server_names: Dict[str, str] = {}
        # Track registered proxied tools to avoid re-registration
        self._registered_proxied_tools: set = set()
        # original schema key -> generated wrapper parameter name
        self._proxied_param_aliases: Dict[str, Dict[str, str]] = {}
        logger.info("DynamicFastMCP initialized")

    def unregister_proxied_tools(
        self, account_id: str, server_id: str, tool_names: List[str]
    ) -> int:
        """Drop this process's wrappers for a deleted MCP server's tools.

        Wrappers resolve their server at call time, so this is cleanup, not
        a routing fix: other pods keep a wrapper that answers "no active MCP
        server" until another server in the account exposes the same name
        again. The next ``list_tools`` re-registers a name that is still
        served. Returns the number of wrappers removed.
        """
        safe_account_id = str(account_id).replace("-", "_")
        removed = 0
        for tool_name in tool_names:
            internal_name = f"account_{safe_account_id}_{tool_name}"
            if internal_name in self._registered_proxied_tools:
                self._registered_proxied_tools.discard(internal_name)
                self._proxied_param_aliases.pop(internal_name, None)
                try:
                    self.local_provider.remove_tool(internal_name)
                except Exception:
                    logger.debug("Proxied wrapper already absent", exc_info=True)
                removed += 1
            # The name maps are process-wide and keyed by tool name; only
            # clear entries that still point at the deleted server.
            if self._proxied_tool_servers.get(tool_name) == str(server_id):
                self._proxied_tool_servers.pop(tool_name, None)
                self._proxied_tool_server_names.pop(tool_name, None)
        return removed

    def set_user_context_provider(self, provider: Callable[[], Optional[UserContext]]):
        """Set a function that provides current user context.

        This function will be called during tool listing and execution to get
        the current authenticated user's context.

        Args:
            provider: Function that returns UserContext or None
        """
        self._user_context_provider = provider
        logger.info("User context provider registered")

    async def list_tools(self, *, run_middleware: bool = True) -> list[Tool]:
        """Override FastMCP's list_tools to filter based on user context.

        This method is called by FastMCP's protocol handler to get the list
        of available tools. We filter the full tool list based on the current
        user's context.

        Phase 1B: Now includes proxied tools from external MCP servers.

        Args:
            run_middleware: Whether to run middleware (passed to super)

        Returns:
            List of tools available to the current user
        """
        logger.info("!!! list_tools called - ENTRY POINT !!!")
        # Get current user context
        user_context = self._get_current_user_context()
        logger.info(f"!!! Got user context: {user_context} !!!")

        if not user_context:
            logger.warning("No user context available, returning empty tool list")
            return []

        logger.info(
            f"Filtering tools for user {user_context.username}, has_tracker={user_context.has_tracker}"
        )

        # Start with empty list
        available_tools = []

        # Add built-in tools (filtered by tracker requirements metadata)
        default_tools = [
            t for t in await super().list_tools(run_middleware=run_middleware)
        ]
        # Filter out internal proxied tool names (they start with "account_")
        builtin_tools = [t for t in default_tools if not t.name.startswith("account_")]

        builtin_meta = {t["name"]: t for t in BUILTIN_TOOLS}

        filtered_tools = []
        for tool in builtin_tools:
            meta = builtin_meta.get(tool.name)
            if not meta:
                filtered_tools.append(tool)
                continue

            required_types = meta.get("required_tracker_types") or []
            requires_tracker = meta.get("requires_tracker", False)

            if requires_tracker and not user_context.has_tracker:
                logger.info(
                    f"Skipping tool '{tool.name}' (requires tracker but none configured)"
                )
                continue

            if required_types and not any(
                t in user_context.tracker_types for t in required_types
            ):
                logger.info(
                    f"Skipping tool '{tool.name}' (requires tracker types {required_types}, have {user_context.tracker_types})"
                )
                continue

            filtered_tools.append(tool)

        available_tools.extend(filtered_tools)
        logger.info(
            f"Added {len(filtered_tools)} default tools after tracker-type filtering "
            f"(filtered out {len(builtin_tools) - len(filtered_tools)} tracker-specific tools, "
            f"{len(default_tools) - len(builtin_tools)} internal names)"
        )

        if user_context.mcp_tools_cache is not None:
            logger.info("Returning cached tools from UserContext")
            return user_context.mcp_tools_cache

        # Add proxied tools from external MCP servers (Phase 1B)
        # Now with dynamic registration for streaming approval support
        proxied_tools_data = []
        justification_modes = {}
        builtin_enabled_map = {}
        account_meta = {}

        try:
            # One shared DB session for all list_tools metadata lookups so we
            # do not open three concurrent connections per tools/list call.
            def _fetch_list_tools_metadata():
                db = next(get_db())
                try:
                    from preloop.services.mcp_tool_discovery import (
                        _get_proxied_tools_sync,
                    )
                    from preloop.models.crud import crud_account

                    proxied = _get_proxied_tools_sync(user_context.account_id, db)
                    configs = crud_tool_configuration.get_multi_by_account(
                        db, account_id=str(user_context.account_id), limit=1000
                    )
                    # Scope-aware: agent-scoped rows apply only to the calling
                    # agent and override the account-wide row; rows scoped to
                    # other agents are invisible here.
                    visible = _configs_visible_to_caller(
                        configs, getattr(user_context, "managed_agent_id", None)
                    )
                    # required wins over optional when the two alias names disagree.
                    modes: dict[str, str] = {}
                    for tc in visible:
                        if tc.justification_mode not in ("optional", "required"):
                            continue
                        for alias_name in _tool_enabled_override_names(tc.tool_name):
                            if (
                                tc.justification_mode == "required"
                                or alias_name not in modes
                            ):
                                modes[alias_name] = tc.justification_mode
                    # A disable stored under either alias name disables both.
                    # An enable does not override a disable of the other name.
                    enabled: dict[str, bool] = {}
                    for tc in visible:
                        if tc.tool_source != "builtin":
                            continue
                        for alias_name in _tool_enabled_override_names(tc.tool_name):
                            if enabled.get(alias_name) is False:
                                continue
                            enabled[alias_name] = bool(tc.is_enabled)
                    acc = crud_account.get(db, id=user_context.account_id)
                    meta = getattr(acc, "meta_data", {}) or {}
                    return proxied, modes, enabled, meta
                finally:
                    db.close()

            logger.info("Fetching DB metadata for list_tools...")
            loop = asyncio.get_event_loop()
            (
                proxied_tools_data,
                justification_modes,
                builtin_enabled_map,
                account_meta,
            ) = await asyncio.wait_for(
                loop.run_in_executor(None, _fetch_list_tools_metadata),
                timeout=30,
            )
            logger.info(f"Fetched {len(proxied_tools_data)} proxied tools")

            # Dynamically register wrapper functions for proxied tools
            proxied_tool_map = {}  # Track original_name -> internal_name mapping

            for mcp_server, mcp_tool in proxied_tools_data:
                # The name agents see: ``<tool_prefix>_<tool>`` when the
                # server has an explicit prefix (#1135), else the upstream
                # name. Discovery already dropped shadowed duplicates.
                exposed_name = exposed_tool_name(
                    getattr(mcp_server, "tool_prefix", None), mcp_tool.name
                )
                if not _is_safe_tool_identifier(exposed_name):
                    logger.warning(
                        "Skipping proxied tool with unsafe name %r; "
                        "not interpolating into generated wrapper source",
                        exposed_name,
                    )
                    continue

                # Create internal name with namespace (sanitize account_id)
                safe_account_id = user_context.account_id.replace("-", "_")
                internal_name = f"account_{safe_account_id}_{exposed_name}"
                proxied_tool_map[exposed_name] = (
                    internal_name,
                    mcp_tool,
                    mcp_server,
                )

                # Only register if not already registered
                if internal_name not in self._registered_proxied_tools:
                    logger.info(
                        f"Dynamically registering proxied tool: {exposed_name} "
                        f"(internal: {internal_name})"
                    )

                    # Create wrapper function with approval and streaming
                    try:
                        wrapper = self._create_proxied_tool_wrapper(
                            tool_name=exposed_name,
                            account_id=user_context.account_id,
                            description=mcp_tool.description or "",
                            input_schema=mcp_tool.input_schema,
                        )
                    except Exception:
                        logger.warning(
                            "Skipping proxied tool %r: wrapper creation failed",
                            exposed_name,
                            exc_info=True,
                        )
                        continue
                    if wrapper is None:
                        continue

                    # Register with FastMCP using @mcp.tool() decorator
                    self.tool()(wrapper)

                    # Track as registered
                    self._registered_proxied_tools.add(internal_name)

                # Always track the mapping for name translation
                self._proxied_tool_servers[exposed_name] = str(mcp_server.id)
                self._proxied_tool_server_names[exposed_name] = mcp_server.name

            # Now get all registered tools and map back to original names
            all_registered = await super().list_tools(run_middleware=run_middleware)
            logger.info(
                f"Total registered tools after dynamic registration: {len(all_registered)}"
            )

            for original_name, (
                internal_name,
                mcp_tool,
                mcp_server,
            ) in proxied_tool_map.items():
                # Check if this tool was registered
                if any(t.name == internal_name for t in all_registered):
                    # Add with original name for client visibility
                    tool = Tool(
                        name=original_name,  # Client sees original name
                        description=mcp_tool.description or "",
                        parameters=mcp_tool.input_schema,
                    )
                    available_tools.append(tool)
                    logger.info(
                        f"Exposing proxied tool: {original_name} (internal: {internal_name}) mcp_server: {mcp_server}"
                    )
                else:
                    logger.warning(
                        f"Proxied tool {internal_name} was not found in registered tools!"
                    )

            logger.info(
                f"Added {len(proxied_tool_map)} proxied tools to available list"
            )

        except Exception as e:
            logger.error(f"Error loading proxied tools: {e}", exc_info=True)
            # Continue with just default tools

        # ── Enforce builtin tool enable/disable configuration ────────────
        # A builtin tool is exposed only if:
        #   - an explicit ToolConfiguration row enables it, or
        #   - no config row exists AND its metadata does not default-disable it.
        # Flow executions are exempt: they carry an explicit allow-list
        # (allowed_flow_tools) that opts into exactly the tools the flow
        # needs, so account-level disables must not break preset flows.
        if user_context.allowed_flow_tools is None:
            subject_context = {
                "api_key_id": user_context.api_key_id,
                "managed_agent_id": getattr(user_context, "managed_agent_id", None),
            }
            before_count = len(available_tools)
            enabled_filtered = []
            for tool in available_tools:
                meta = builtin_meta.get(tool.name)
                if meta is None:
                    # Not a builtin tool (proxied); handled elsewhere
                    enabled_filtered.append(tool)
                    continue
                explicit = builtin_enabled_map.get(tool.name)
                if explicit is not None:
                    if explicit:
                        enabled_filtered.append(tool)
                    else:
                        logger.info(
                            f"Skipping builtin tool '{tool.name}' "
                            "(disabled by tool configuration)"
                        )
                elif meta.get("default_enabled", True):
                    enabled_filtered.append(tool)
                elif _policy_enables_deprecated_alias(
                    account_meta,
                    tool_name=tool.name,
                    subject_context=subject_context,
                ):
                    enabled_filtered.append(tool)
                else:
                    logger.info(
                        f"Skipping builtin tool '{tool.name}' "
                        "(default-disabled, no explicit enable configured)"
                    )
            available_tools = enabled_filtered
            if before_count != len(available_tools):
                logger.info(
                    f"Builtin enable/disable filter removed "
                    f"{before_count - len(available_tools)} tools"
                )

        # SECURITY: Enforce flow-specific tool restrictions if present
        # This provides defense-in-depth: even if an agent is compromised,
        # it cannot call tools outside the flow's allowed list
        if user_context.allowed_flow_tools is not None:
            original_count = len(available_tools)
            allowed = set(user_context.allowed_flow_tools)
            # Backward-compatible alias matching (#1044): search and search_issues
            for alias_src, alias_dst in TOOL_NAME_ALIASES.items():
                if alias_src in allowed:
                    allowed.add(alias_dst)
            available_tools = [tool for tool in available_tools if tool.name in allowed]
            logger.info(
                f"Flow execution restriction: filtered {original_count} tools down to "
                f"{len(available_tools)} allowed tools for flow execution "
                f"{user_context.flow_execution_id}"
            )

        # ── Inject justification parameter based on ToolConfiguration ────
        # ── Inject justification parameter based on ToolConfiguration ────
        try:
            modified_tools = []
            for tool in available_tools:
                j_mode = justification_modes.get(tool.name)
                if j_mode:
                    modified_schema = copy.deepcopy(tool.parameters)
                    if "properties" not in modified_schema:
                        modified_schema["properties"] = {}
                    modified_schema["properties"]["justification"] = {
                        "type": "string",
                        "description": (
                            "Provide your reasoning and context for why "
                            "this tool is being called. This will be "
                            "reviewed by approvers and logged for audit "
                            "purposes."
                        ),
                    }
                    if j_mode == "required":
                        required = list(modified_schema.get("required", []))
                        if "justification" not in required:
                            required.append("justification")
                            modified_schema["required"] = required
                    tool = Tool(
                        name=tool.name,
                        description=tool.description or "",
                        parameters=modified_schema,
                    )
                    logger.info(
                        f"Injected justification parameter "
                        f"(mode={j_mode}) into "
                        f"tool '{tool.name}'"
                    )
                modified_tools.append(tool)

            available_tools = modified_tools
        except Exception as e:
            logger.error(
                f"Error injecting justification parameters: {e}",
                exc_info=True,
            )

        logger.info(
            f"Returning {len(available_tools)} total tools for user {user_context.username} before governance filter"
        )

        # ── Apply subject governance to filter out disabled tools ─────────
        try:
            subject_context = {
                "api_key_id": user_context.api_key_id,
                "flow_id": getattr(user_context, "flow_id", None),
                "managed_agent_id": getattr(user_context, "managed_agent_id", None),
            }

            final_tools = []
            for tool in available_tools:
                if is_tool_enabled_for_subject(
                    account_meta, tool_name=tool.name, subject_context=subject_context
                ):
                    final_tools.append(tool)

            available_tools = final_tools

        except Exception as e:
            logger.error(
                f"Error filtering tools through subject governance: {e}", exc_info=True
            )

        for tool in available_tools:
            logger.info(f"  - {tool.name}")

        user_context.mcp_tools_cache = available_tools
        return available_tools

    def _get_current_user_context(self) -> Optional[UserContext]:
        """Get the current user context for this request.

        This calls the user context provider function that was registered
        via set_user_context_provider().

        Returns:
            UserContext if available, None otherwise
        """
        if not self._user_context_provider:
            logger.warning("No user context provider registered")
            return None

        try:
            context = self._user_context_provider()
            if context:
                logger.debug(f"Got user context: {context.username}")
            else:
                logger.warning("User context provider returned None")
            return context
        except Exception as e:
            logger.error(f"Error getting user context: {e}", exc_info=True)
            return None

    def _create_proxied_tool_wrapper(
        self,
        tool_name: str,
        account_id: str,
        description: str,
        input_schema: dict,
    ) -> Optional[Callable[..., Any]]:
        """Factory to create wrapper functions for proxied tools with approval and streaming.

        Creates a function with explicit parameters based on the input_schema.
        FastMCP doesn't support **kwargs, so we need to build the function dynamically.

        Args:
            tool_name: Name of the tool. The serving MCP server is resolved
                from ``(account_id, tool_name)`` on every call, never bound
                here, so a recreated server is picked up without a restart.
            account_id: Owner account ID
            description: Tool description
            input_schema: Tool input schema (JSON Schema)

        Returns:
            Async wrapper function with Context support and explicit parameters,
            or ``None`` if *tool_name* is not a safe tool identifier.
        """
        from fastmcp import Context

        if not _is_safe_tool_identifier(tool_name):
            logger.warning(
                "Skipping proxied tool wrapper for unsafe tool name %r",
                tool_name,
            )
            return None

        # Create internal name with namespace to avoid collisions
        # Sanitize account_id for Python identifier (replace hyphens with underscores)
        safe_account_id = account_id.replace("-", "_")
        internal_name = f"account_{safe_account_id}_{tool_name}"

        # Extract parameters from input schema
        properties = input_schema.get("properties", {})
        required_params = set(input_schema.get("required", []))

        # Build parameter list dynamically. ``param_names`` stores
        # ``(alias, original)`` so builtins can be interpolated as aliases
        # while ``arguments[original]`` still forwards the upstream key.
        req_params = []
        opt_params = []
        param_names = []
        taken_names = set(properties) if isinstance(properties, dict) else set()

        for param_name, param_def in properties.items():
            if not _is_safe_tool_identifier(param_name):
                logger.warning(
                    "Skipping unsafe parameter name %r on proxied tool %r",
                    param_name,
                    tool_name,
                )
                continue
            if param_name in _WRAPPER_BODY_BUILTIN_SET:
                alias = _unused_wrapper_alias(param_name, taken_names)
                taken_names.add(alias)
                logger.info(
                    "Aliasing builtin-colliding parameter %r to %r on tool %r",
                    param_name,
                    alias,
                    tool_name,
                )
            elif not _is_safe_generated_identifier(param_name):
                logger.warning(
                    "Skipping unsafe parameter name %r on proxied tool %r",
                    param_name,
                    tool_name,
                )
                continue
            else:
                alias = param_name
            param_names.append((alias, param_name))
            if not isinstance(param_def, dict):
                param_def = {}

            # Mirror the advertised JSON Schema instead of flattening complex
            # shapes (arrays, unions, objects) to `str`.
            type_str = _python_type_for_schema(param_def)

            # Add Optional if not required
            if param_name not in required_params:
                opt_params.append(f"{alias}: {_optional_annotation(type_str)} = None")
            else:
                req_params.append(f"{alias}: {type_str}")

        # Add Context parameter at the end
        opt_params.append("ctx: Optional[Context] = None")

        # Create function signature string
        params = req_params + opt_params
        params_str = ", ".join(params)

        # Names interpolated below were validated as safe generated identifiers.
        wrapper_code = f"""
async def {internal_name}({params_str}):
    # DEBUG: Log Context availability
    logger.info(f"[WRAPPER] {{tool_name}} called with Context: {{ctx is not None}}")
    if ctx:
        logger.info(f"[WRAPPER] Context type: {{type(ctx)}}, has report_progress: {{hasattr(ctx, 'report_progress')}}")

    # SECURITY CHECK: Verify caller owns this tool
    user_context = self._get_current_user_context()
    if not user_context or user_context.account_id != account_id:
        logger.warning(
            f"Security violation: User {{user_context.account_id if user_context else 'None'}} "
            f"attempted to call tool '{{tool_name}}' owned by {{account_id}}"
        )
        return _wrapper_tool_error(
            "Access denied: Tool not available", status="refused"
        )

    # Collect all arguments. ``param_names`` is ``(alias, original)``.
    arguments = {{}}
    for param_name in param_names:
        value = locals().get(param_name[0])
        if value is not None:
            arguments[param_name[1]] = value

    # Read justification from context var — _call_tool() already stripped it
    # from arguments and stored it in _justification_var.
    from preloop.services.dynamic_fastmcp import _justification_var
    justification = _justification_var.get(None)

    snapshot = _grant_dispatch_var.get(None)
    if snapshot is None or snapshot.account_id != account_id or snapshot.tool_name != tool_name:
        try:
            snapshot, grant = await _prepare_grant_dispatch(account_id, tool_name)
        except Exception:
            return _wrapper_tool_error("Access denied: introspection_unavailable", status="refused")
        if grant is not None:
            _grant_binding_var.set(deepcopy(grant.binding))
        if grant is not None and grant.deny_reason:
            _record_grant_denial(account_id, tool_name, grant, user_id=user_context.user_id,
                                correlation_id=_correlation_id_var.get(None))
            return _wrapper_tool_error(f"Access denied: {{grant.deny_reason}}", status="refused")
    if snapshot is None:
        return _wrapper_tool_error("Access denied: no active MCP server provides this tool", status="refused")

    # Check approval with streaming (we have Context!)
    # The workflow_id may have been set by _call_tool() after evaluating access rules.
    from preloop.services.approval_helper import require_approval
    rule_workflow_id = _rule_workflow_id_var.get(None)
    corr_id = _correlation_id_var.get(None)

    approved, error = await require_approval(
        tool_name=tool_name,
        tool_source="mcp",
        account_id=account_id,
        arguments=arguments,
        ctx=ctx,
        workflow_id=rule_workflow_id,
        correlation_id=corr_id,
        justification=justification,
        server_name=snapshot.server_name,
    )

    if not approved:
        return _wrapper_tool_error(error, status="refused")

    denial = await self._grant_dispatch_denial(snapshot, user_context)
    if denial:
        return _wrapper_tool_error(denial, status="refused")

    # Call external MCP server
    try:
        server_id = snapshot.client_config["server_id"]
        server_name = snapshot.server_name
        upstream_name = snapshot.upstream_name
        client_pool = get_mcp_client_pool()
        client = await client_pool.get_client(**snapshot.client_config)

        # Approval and connection setup may outlive the initial halt check.
        denial = await self._halt_dispatch_denial(account_id)
        if denial:
            return _wrapper_tool_error(denial, status="refused")
        denial = await self._grant_dispatch_denial(snapshot, user_context)
        if denial:
            return _wrapper_tool_error(denial, status="refused")
        # Call tool on external server
        result = await client.call_tool(upstream_name, arguments)
        # Keep isError/structuredContent before filters rebuild the list.
        upstream = result
        logger.info(
            f"Tool {{tool_name}} returned from external server "
            f"(is_error={{getattr(upstream, 'is_error', False)}})"
        )
        # Keep the raw content list for the outer call_tool finally
        # (browser_step derivation). Filters and the string conversion
        # below only shape what the agent receives.
        _proxied_raw_result_var.set(result)

        # Apply operator-configured output filters BEFORE the result
        # reaches the agent, stripping unused fields to save context tokens.
        if isinstance(result, list):
            result = apply_output_filters(
                result,
                account_id=account_id,
                tool_name=tool_name,
                server_name=server_name,
                managed_agent_id=getattr(
                    user_context, "managed_agent_id", None
                ),
            )

        # Convert result to string; forward isError/structuredContent.
        if isinstance(result, list):
            text = "\\n".join(
                item.text if hasattr(item, "text") else str(item)
                for item in result
            )
        else:
            text = str(result)
        return _proxied_upstream_result(text, upstream)

    except Exception as e:
        from preloop.services.mcp_client_pool import (
            _unwrap_exception_group,
            is_mcp_unavailable_error,
        )

        cause = _unwrap_exception_group(e)
        server_label = snapshot.server_name
        logger.error(
            f"Error executing proxied tool {{tool_name}} via {{server_label}}: {{cause}}",
            exc_info=True,
        )
        unavailable = is_mcp_unavailable_error(cause)
        outcome = _proxied_exception_outcome(
            cause, server_id=locals().get("server_id"), unavailable=unavailable
        )
        if unavailable:
            return _wrapper_tool_error(
                f"The '{{server_label}}' MCP server is temporarily unavailable, so the "
                f"'{{tool_name}}' tool could not run. Please retry in a moment; if it "
                f"keeps happening the server may be down.",
                status=outcome,
            )
        return _wrapper_tool_error(
            f"Error executing tool '{{tool_name}}': {{cause}}",
            status=outcome,
        )
"""

        # Create local namespace with required variables. Keys must match
        # ``_WRAPPER_NAMESPACE_KEYS`` so the identifier guard cannot drift.
        namespace_values = {
            "self": self,
            "account_id": account_id,
            "tool_name": tool_name,
            "_resolve_proxied_tool_server": _resolve_proxied_tool_server,
            "_upstream_tool_name": _upstream_tool_name,
            "_grant_dispatch_var": _grant_dispatch_var,
            "_grant_binding_var": _grant_binding_var,
            "deepcopy": deepcopy,
            "_prepare_grant_dispatch": _prepare_grant_dispatch,
            "_record_grant_denial": _record_grant_denial,
            "param_names": param_names,
            "logger": logger,
            "get_db": get_db,
            "crud_mcp_server": crud_mcp_server,
            "get_mcp_client_pool": get_mcp_client_pool,
            "apply_output_filters": apply_output_filters,
            "Optional": Optional,
            "Union": Union,
            "Any": Any,
            "List": List,
            "Dict": Dict,
            "Context": Context,
            "_rule_workflow_id_var": _rule_workflow_id_var,
            "_correlation_id_var": _correlation_id_var,
            "_proxied_raw_result_var": _proxied_raw_result_var,
            "_wrapper_tool_error": _wrapper_tool_error,
            "_proxied_upstream_result": _proxied_upstream_result,
            "_proxied_exception_outcome": _proxied_exception_outcome,
        }
        if namespace_values.keys() != set(_WRAPPER_NAMESPACE_KEYS):
            raise RuntimeError(
                "wrapper namespace values must match _WRAPPER_NAMESPACE_KEYS"
            )
        namespace = {key: namespace_values[key] for key in _WRAPPER_NAMESPACE_KEYS}

        # Execute the code to create the function
        exec(wrapper_code, namespace)
        wrapper = namespace[internal_name]

        # Set function metadata
        wrapper.__doc__ = description
        # Store original name and owner for reference
        wrapper._display_name = tool_name  # type: ignore
        wrapper._account_id = account_id  # type: ignore
        original_to_alias = {original: alias for alias, original in param_names}
        self._proxied_param_aliases[internal_name] = original_to_alias

        logger.info(
            "Created wrapper function for %s with parameters: %s",
            tool_name,
            [original for _, original in param_names],
        )

        return wrapper

    def _remap_wrapper_arguments(
        self, internal_name: str, arguments: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Rewrite client keys to generated aliases before FastMCP dispatch.

        When both an original key and its alias are present, the original
        key's value wins so a stray alias cannot replace the in-spec value.
        Remapping an already-aliased dict is a no-op (idempotent).

        Args:
            internal_name: Registered wrapper name (``account_<id>_<tool>``).
            arguments: Client-facing ``tools/call`` arguments.

        Returns:
            Arguments keyed by wrapper parameter names, or *arguments* when
            no alias map exists.
        """
        aliases = self._proxied_param_aliases.get(internal_name)
        if not arguments or not aliases:
            return arguments
        mapped: Dict[str, Any] = {}
        originals_applied: set[str] = set()
        for key, value in arguments.items():
            target = aliases.get(key, key)
            if target != key:
                mapped[target] = value
                originals_applied.add(target)
                continue
            if target in originals_applied:
                continue
            mapped[target] = value
        return mapped

    async def call_tool(
        self,
        name: str,
        arguments: dict | None = None,
        *,
        version=None,
        run_middleware: bool = True,
        task_meta=None,
    ):
        """Override tool execution for access validation and name translation.

        This is called by FastMCP's protocol handler before executing a tool.
        We check if the user has access to the requested tool, then translate
        the tool name if it's a proxied tool (client name -> internal name).

        Approval is now handled at the function level (in tool implementations)
        for both builtin and proxied tools, allowing streaming progress updates.

        Args:
            name: Name of the tool
            arguments: Arguments derived from FastMCP
            version: Optional Tool Version
            run_middleware: Whether to run middleware
            task_meta: Optional Background Task Metadata

        Returns:
            ToolResult from tool execution
        """
        arguments = arguments or {}

        # Extract justification from arguments before it reaches the tool function.
        # Justification is injected into the schema by list_tools() but isn't part
        # of the actual tool's function signature.
        justification = arguments.pop("justification", None)
        _justification_var.set(justification)

        logger.info(f"!!! call_tool called for tool: {name} !!!")

        # ── Internal proxied tool name re-entry ─────────────────────────
        # When we translate a client-facing proxied tool name to its internal
        # `account_<id>_<tool>` form below and call ``super().call_tool``,
        # FastMCP's tool dispatch re-enters this override with the internal
        # name. The access checks, justification/policy evaluation, and audit
        # logging here are scoped to the user-visible tool name and were
        # already performed on the first entry, so we shortcut directly to
        # the FastMCP base implementation. The dynamically-registered wrapper
        # (see ``_create_proxied_tool_wrapper``) still enforces account-level
        # ownership before dispatching to the upstream MCP server.
        if name in self._registered_proxied_tools:
            if not _is_proxy_translation_var.get(False):
                logger.warning(
                    f"Blocked direct invocation of internal proxied tool name: {name}"
                )
                denial = (
                    f"Access denied: Cannot invoke internal tool name '{name}' directly"
                )
                self._record_attributed_refusal(name, arguments, denial)
                return _tool_error_result(denial)

            logger.info(
                f"Internal proxied tool re-entry: {name} (skipping duplicate checks)"
            )
            return await super().call_tool(
                name,
                self._remap_wrapper_arguments(name, arguments),
                version=version,
                run_middleware=run_middleware,
                task_meta=task_meta,
            )

        _grant_dispatch_var.set(None)
        _grant_binding_var.set(None)
        grant_binding = None

        # Get current user context before allocating a correlation id so a
        # missing-context return cannot leave the context var set.
        user_context = self._get_current_user_context()
        if not user_context:
            logger.warning("No user context available for tool call")
            return _tool_error_result("Error: No user context available")

        # Generate the correlation id before any check that can refuse the
        # call. Every outcome of this invocation — including a refusal that
        # never reaches the tool — must share one id so the usage row and the
        # audit trail can be joined.
        correlation_id = str(uuid.uuid4())
        _correlation_id_var.set(correlation_id)
        _tool_outcome_var.set(None)

        async def _refuse(text: str) -> ToolResult:
            """Record a refused call (usage and audit rows), return its error."""
            self._audit_refused_tool_call(
                user_context,
                client_tool_name=name,
                arguments=arguments,
                reason=text,
                correlation_id=correlation_id,
                grant=grant_binding,
            )
            _grant_dispatch_var.set(None)
            _grant_binding_var.set(None)
            try:
                self._persist_tool_call_activity(
                    user_context,
                    tool_name=name,
                    client_tool_name=name,
                    status=TOOL_CALL_STATUS_REFUSED,
                    summary=text,
                    arguments=arguments,
                    correlation_id=correlation_id,
                )
            except Exception as exc:  # pragma: no cover - best effort only
                logger.debug("Failed to persist refused tool call '%s': %s", name, exc)
            _correlation_id_var.set(None)
            _tool_outcome_var.set(None)
            return _tool_error_result(text)

        # ── Account kill switch (#157) ───────────────────────────────────
        # One account-scoped control that halts ALL tool calls. Runs before
        # every other per-call check (justification, enablement, policy) so
        # an emergency stop cannot be shadowed, and also runs on the async
        # post-approval re-execution path (_bypass_approval_var): an
        # approval granted before (or even during) the halt must not let a
        # tool execute while the account is halted.
        try:

            def _check_kill_switch():
                db = next(get_db())
                try:
                    return kill_switch_service.tools_halted(db, user_context.account_id)
                finally:
                    db.close()

            tools_are_halted = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _check_kill_switch),
                timeout=30,
            )
        except Exception as e:
            # SECURITY: fail closed — if halt state cannot be verified,
            # block the call rather than risk executing tools during an
            # emergency.
            logger.error(
                f"Kill-switch check failed for tool '{name}': {e}. "
                f"Blocking tool call (fail closed)."
            )
            return await _refuse(
                "Error: Unable to verify account halt state "
                f"for tool '{name}'. Please try again."
            )
        if tools_are_halted:
            logger.warning(
                f"Tool '{name}' for user {user_context.username} rejected "
                f"by account kill switch"
            )
            denied_text = kill_switch_service.TOOL_DENIAL_MESSAGE
            if name == "permission_prompt":
                # Claude Code parses this tool's response as its
                # permission behavior schema; a plain string would
                # surface as a confusing parse failure instead of a
                # clean deny.
                denied_text = json.dumps(
                    {
                        "behavior": "deny",
                        "message": kill_switch_service.TOOL_DENIAL_MESSAGE,
                    }
                )
            return await _refuse(denied_text)

        # ── Server-side justification enforcement ─────────────────────────
        # Schema injection alone isn't sufficient — clients can skip
        # validation. Verify server-side that required justifications are
        # actually provided.
        # Skip during async re-execution (_bypass_approval_var=True) because
        # justification was already validated on the original call and is not
        # persisted in tool_args.
        if not _bypass_approval_var.get(False):
            try:

                def _check_tool_config():
                    db = next(get_db())
                    try:
                        configs = crud_tool_configuration.get_multi_by_account(
                            db,
                            account_id=str(user_context.account_id),
                            limit=1000,
                        )
                        # Scope-aware (mirrors list_tools): agent-scoped rows
                        # apply only to the calling agent and override the
                        # account-wide row; rows scoped to other agents are
                        # invisible.
                        visible = _configs_visible_to_caller(
                            configs,
                            getattr(user_context, "managed_agent_id", None),
                        )
                        requires_just = False
                        builtin_enabled = None
                        for tc in visible:
                            if (
                                tc.justification_mode == "required"
                                and name in _tool_enabled_override_names(tc.tool_name)
                            ):
                                requires_just = True
                            if tc.tool_source != "builtin":
                                continue
                            if name not in _tool_enabled_override_names(tc.tool_name):
                                continue
                            # Disable wins when search and search_issues disagree.
                            if builtin_enabled is False:
                                continue
                            builtin_enabled = bool(tc.is_enabled)
                        return requires_just, builtin_enabled
                    finally:
                        db.close()

                (
                    requires_justification,
                    builtin_explicit_enabled,
                ) = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, _check_tool_config),
                    timeout=30,
                )

                # ── Enforce builtin tool enable/disable at call time ──────
                # Mirrors the list_tools filter: a disabled builtin tool must
                # not be invocable by name either. Flow executions are exempt
                # because their explicit allow-list (allowed_flow_tools) is
                # enforced separately via the list_tools access check below.
                builtin_call_meta = next(
                    (t for t in BUILTIN_TOOLS if t["name"] == name), None
                )
                if (
                    builtin_call_meta is not None
                    and user_context.allowed_flow_tools is None
                ):
                    alias_enabled_by_policy = False
                    if (
                        builtin_explicit_enabled is None
                        and name in DEPRECATED_ALIAS_NAMES
                    ):

                        def _alias_account_meta() -> dict:
                            db = next(get_db())
                            try:
                                acc = crud_account.get(db, id=user_context.account_id)
                                meta = getattr(acc, "meta_data", {}) or {}
                                return meta if isinstance(meta, dict) else {}
                            finally:
                                db.close()

                        call_account_meta = await asyncio.wait_for(
                            asyncio.get_event_loop().run_in_executor(
                                None, _alias_account_meta
                            ),
                            timeout=30,
                        )
                        call_subject_context = {
                            "api_key_id": user_context.api_key_id,
                            "managed_agent_id": getattr(
                                user_context, "managed_agent_id", None
                            ),
                        }
                        alias_enabled_by_policy = _policy_enables_deprecated_alias(
                            call_account_meta,
                            tool_name=name,
                            subject_context=call_subject_context,
                        )
                    is_disabled = builtin_explicit_enabled is False or (
                        builtin_explicit_enabled is None
                        and not builtin_call_meta.get("default_enabled", True)
                        and not alias_enabled_by_policy
                    )
                    if is_disabled:
                        logger.warning(f"Blocked call to disabled builtin tool: {name}")
                        denied_text = (
                            f"Access denied: Tool '{name}' is disabled "
                            "for this account. Enable it on the Tools "
                            "page to use it."
                        )
                        if name == "permission_prompt":
                            # Claude Code parses this tool's response as its
                            # permission behavior schema; a plain string would
                            # surface as a confusing parse failure instead of
                            # a clean deny.
                            denied_text = json.dumps(
                                {
                                    "behavior": "deny",
                                    "message": (
                                        "The Preloop permission_prompt tool is "
                                        "disabled for this account. Enable it on "
                                        "the Tools page in the Preloop console, "
                                        "then retry."
                                    ),
                                }
                            )
                        return await _refuse(denied_text)
                if requires_justification and not justification:
                    return await _refuse(
                        f"Justification required: Tool '{name}' requires a "
                        f"'justification' parameter explaining why this tool "
                        f"is being called."
                    )
            except Exception as e:
                # SECURITY: Fail closed — if we cannot verify whether
                # justification is required, block the call rather than
                # allowing potentially unjustified tool executions.
                logger.error(
                    f"Justification enforcement check failed for '{name}': {e}. "
                    f"Blocking tool call (fail closed)."
                )
                return await _refuse(
                    f"Error: Unable to verify justification requirements "
                    f"for tool '{name}'. Please try again."
                )

        # Check if user has access to this tool
        available_tools = await self.list_tools(run_middleware=run_middleware)
        if not any(tool.name == name for tool in available_tools):
            logger.warning(
                f"User {user_context.username} attempted to call "
                f"unauthorized tool: {name}"
            )
            return await _refuse(f"Access denied: Tool '{name}' is not available")

        # Resolve the same prefix/first-wins owner used by dispatch once.
        # Keep its copied credentials in memory through any synchronous wait.
        if name in self._proxied_tool_servers:
            try:
                snapshot, grant = await _prepare_grant_dispatch(
                    user_context.account_id, name
                )
            except Exception:
                grant_binding = grant_introspector._unavailable()
                _record_grant_denial(
                    user_context.account_id,
                    name,
                    GrantResult(grant_binding, "introspection_unavailable"),
                    user_id=user_context.user_id,
                    correlation_id=correlation_id,
                )
                return await _refuse("Access denied: introspection_unavailable")
            if snapshot is None:
                return await _refuse(
                    "Access denied: no active MCP server provides this tool"
                )
            _grant_dispatch_var.set(snapshot)
            if grant is not None:
                grant_binding = grant.binding
                _grant_binding_var.set(grant_binding)
                if grant.deny_reason:
                    _record_grant_denial(
                        user_context.account_id,
                        name,
                        grant,
                        user_id=user_context.user_id,
                        correlation_id=correlation_id,
                    )
                    return await _refuse(f"Access denied: {grant.deny_reason}")

        # ── Sensitive data rules on tool arguments (#1122) ───────────────
        # Runs before the access rules so their conditions can read the
        # detector bindings (pii.found, pii.types_found, pii.paths) next to
        # args. No rule in scope means no detector runs. A replayed call
        # (post-approval) was scanned on its first pass and is not rescanned.
        scope_server_name = self._proxied_tool_server_names.get(
            name, BUILTIN_SERVER_NAME
        )
        if _grant_dispatch_var.get(None) is not None:
            scope_server_name = _grant_dispatch_var.get().server_name
        scope_agent_id = getattr(user_context, "managed_agent_id", None)
        sensitive_config = None
        sensitive_detectors = None
        sensitive_bindings: Optional[dict] = None
        args_outcome = None
        matched_rule_description: Optional[str] = None
        upstream_arguments: Optional[dict] = None
        try:
            sensitive_config, sensitive_detectors = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, _load_sensitive_data_policy, user_context.account_id
                ),
                timeout=30,
            )
        except Exception as e:
            # SECURITY: fail closed. A rule the operator wrote must not be
            # skipped because the policy could not be read.
            logger.error(
                f"Sensitive data policy load failed for '{name}': {e}. "
                "Blocking tool call (fail closed)."
            )
            return await _refuse(
                f"Access denied: sensitive data policy for '{name}' could not "
                "be loaded. Please retry."
            )
        # The block the load above primed, held for this call so the writers
        # in the finally block never depend on the cache TTL (long calls).
        storage_config = apply_storage_redaction_config(user_context.account_id)
        if sensitive_config is not None and not _bypass_approval_var.get(False):
            args_outcome = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: sensitive_tool_policy.evaluate_tool_target(
                    config=sensitive_config,
                    detector_config=sensitive_detectors,
                    target="tool.args",
                    payload=arguments,
                    tool_name=name,
                    server_name=scope_server_name,
                    managed_agent_id=scope_agent_id,
                    account_id=user_context.account_id,
                    user_id=user_context.user_id,
                    correlation_id=correlation_id,
                ),
            )
            if args_outcome.action == "deny":
                return await _refuse(
                    _sensitive_denial_text(args_outcome, where="tool arguments")
                )
            if args_outcome.scan is not None:
                sensitive_bindings = args_outcome.bindings()
            upstream_types = args_outcome.upstream_redaction_types(sensitive_config)
            if upstream_types:
                # redact_upstream: the server receives redacted arguments.
                # ``arguments`` keeps the original for the in-memory approval
                # wait; every stored copy goes through apply_storage_redaction.
                from preloop.services.sensitive_data.redact import redact_structure

                upstream_arguments = redact_structure(
                    arguments,
                    (
                        sensitive_detectors or sensitive_tool_policy.DetectorConfig()
                    ).with_types(upstream_types),
                )[0]

        # ── Evaluate access rules (ToolAccessRule) ──────────────────────
        # This is the central enforcement point for all tool calls.
        # evaluate_policy_async() checks rules in priority order and returns:
        #   "deny"             -> block the call, return denial reason
        #   "require_approval" -> let the per-tool require_approval() handle it
        #   "allow"            -> proceed to execution
        try:
            from preloop.models.db.session import get_async_db_session
            from preloop.services.policy_evaluator import evaluate_policy_async

            async with get_async_db_session() as db:
                _policy_decision = await evaluate_policy_async(
                    db=db,
                    tool_name=name,
                    tool_args=arguments,
                    account_id=uuid.UUID(user_context.account_id),
                    user_id=uuid.UUID(user_context.user_id),
                    server_name=scope_server_name,
                    extra_bindings=(
                        {**(sensitive_bindings or {}), "grant": grant_binding}
                        if grant_binding is not None
                        else sensitive_bindings
                    ),
                    subject_context={
                        "api_key_id": user_context.api_key_id,
                        "flow_id": getattr(user_context, "flow_id", None),
                        "managed_agent_id": getattr(
                            user_context, "managed_agent_id", None
                        ),
                        "runtime_session_id": user_context.runtime_session_id,
                        "runtime_principal_type": user_context.runtime_principal_type,
                        "runtime_principal_id": user_context.runtime_principal_id,
                        "runtime_principal_name": user_context.runtime_principal_name,
                    },
                    correlation_id=correlation_id,
                    extra_details={
                        "runtime_session_id": user_context.runtime_session_id,
                        "runtime_principal_type": user_context.runtime_principal_type,
                        "runtime_principal_id": user_context.runtime_principal_id,
                        "runtime_principal_name": user_context.runtime_principal_name,
                        "api_key_id": user_context.api_key_id,
                        "api_key_name": user_context.api_key_name,
                        **(
                            {"grant": grant_binding}
                            if grant_binding is not None
                            else {}
                        ),
                    },
                )

            action, approval_workflow_id, reason = _policy_decision
            matched_rule_description = reason

            logger.info(
                f"Policy evaluation for '{name}': action={action}, "
                f"workflow_id={approval_workflow_id}, reason={reason}"
            )

            if action == "deny":
                denial_msg = reason or "Tool call denied by access rule"
                return await _refuse(f"Access denied: {denial_msg}")

            if action == "require_approval":
                # Carry the matched rule through to require_approval() so it
                # lands on the approval row. getattr because tests (and any
                # future caller) may patch the evaluator with a plain tuple;
                # a missing snapshot must read as "not recorded", never raise
                # on the enforcement path.
                _rule_context_var.set(getattr(_policy_decision, "rule_context", None))
                if approval_workflow_id:
                    # Store the workflow_id so require_approval() in the tool
                    # wrapper picks it up instead of relying on the legacy
                    # config-level policy.
                    _rule_workflow_id_var.set(str(approval_workflow_id))
                else:
                    # SECURITY: a rule asked for approval but no workflow could
                    # be resolved (no rule/config workflow and no account-level
                    # default). Fail closed instead of silently allowing the
                    # tool through, otherwise an explicit ``require_approval``
                    # rule would behave like ``allow``.
                    _rule_workflow_id_var.set(None)
                    _rule_context_var.set(None)
                    logger.error(
                        f"Tool '{name}' matched require_approval rule but no "
                        "approval workflow is configured (rule, tool config, "
                        "and account default are all unset). Blocking the call."
                    )
                    return await _refuse(
                        f"Tool '{name}' requires approval but no "
                        "approval workflow is configured for this "
                        "account. Configure an approval workflow "
                        "(or mark one as default) and retry."
                    )
            elif args_outcome is not None and args_outcome.action == "require_approval":
                # Access rules allowed the call but a sensitive-data rule
                # wants a human. Same plumbing as an access rule: the tool's
                # own require_approval() picks the workflow and the rule
                # context up from the context vars.
                workflow_id = await asyncio.get_event_loop().run_in_executor(
                    None,
                    _resolve_approval_workflow_id,
                    user_context.account_id,
                    args_outcome.approval_workflow,
                )
                if not workflow_id:
                    _rule_workflow_id_var.set(None)
                    _rule_context_var.set(None)
                    logger.error(
                        f"Tool '{name}' matched sensitive data rule "
                        f"'{args_outcome.rule_id}' (require_approval) but no "
                        "approval workflow is configured. Blocking the call."
                    )
                    return await _refuse(
                        f"Tool '{name}' requires approval but no "
                        "approval workflow is configured for this "
                        "account. Configure an approval workflow "
                        "(or mark one as default) and retry."
                    )
                _rule_workflow_id_var.set(str(workflow_id))
                _rule_context_var.set(args_outcome.rule_context())
            else:
                _rule_workflow_id_var.set(None)
                _rule_context_var.set(None)

        except Exception as e:
            logger.error(
                f"Error evaluating access rules for '{name}': {e}", exc_info=True
            )
            # SECURITY: fail CLOSED. If the central policy evaluation itself
            # raises (DB outage, malformed principal id, etc.) we must not fall
            # through to execution — otherwise any explicit ``deny`` /
            # ``require_approval`` rule (including subject-governance
            # tool-disabled denials) could be bypassed simply by inducing an
            # infrastructure error. Block the call and surface a ret600able
            # error to the agent.
            _rule_workflow_id_var.set(None)
            _rule_context_var.set(None)
            return await _refuse(
                f"Access denied: policy evaluation for '{name}' "
                "failed and the request was blocked as a safety "
                "measure. Please retry; if this persists, contact "
                "your Preloop administrator."
            )

        # ── Translate and execute ───────────────────────────────────────
        # Translate tool name for proxied tools
        # Client calls "calculate_fibonacci", we translate to "account_123_calculate_fibonacci"
        client_tool_name = name
        translation_token = None
        dispatch_arguments = (
            upstream_arguments if upstream_arguments is not None else arguments
        )
        if name in self._proxied_tool_servers:
            safe_account_id = user_context.account_id.replace("-", "_")
            internal_name = f"account_{safe_account_id}_{name}"
            logger.info(f"Translating proxied tool name: {name} -> {internal_name}")
            # Modify target name for the FastMCP router
            name = internal_name
            dispatch_arguments = self._remap_wrapper_arguments(
                internal_name, dispatch_arguments
            )
            translation_token = _is_proxy_translation_var.set(True)
        else:
            # Builtin tool - call with original name
            logger.info(f"Calling builtin tool: {name}")

        # Call parent
        import time

        start_time = time.monotonic()
        exec_status = "executed"
        exec_error: Optional[str] = None
        result: Any = None
        try:
            result = await super().call_tool(
                name,
                dispatch_arguments,
                version=version,
                run_middleware=run_middleware,
                task_meta=task_meta,
            )
            if sensitive_config is not None:
                result = await self._enforce_sensitive_result_policy(
                    user_context,
                    config=sensitive_config,
                    detector_config=sensitive_detectors,
                    client_tool_name=client_tool_name,
                    server_name=scope_server_name,
                    managed_agent_id=scope_agent_id,
                    result=result,
                    correlation_id=correlation_id,
                )
        except Exception as e:
            exec_status = "failed"
            exec_error = str(e)
            raise
        finally:
            if translation_token is not None:
                _is_proxy_translation_var.reset(translation_token)
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            wrapper_outcome = _tool_outcome_var.get(None)
            proxied_raw_result = _proxied_raw_result_var.get(None)
            error_detail = _tool_error_detail_var.get(None)
            audit_status, audit_error_code, audit_error_reason = (
                _audit_tool_call_outcome(
                    exec_status=exec_status,
                    exec_error=exec_error,
                    wrapper_outcome=wrapper_outcome,
                    result=result,
                    error_detail=error_detail,
                )
            )

            _grant_dispatch_var.set(None)
            _grant_binding_var.set(None)

            # Clean up context vars after execution
            _rule_workflow_id_var.set(None)
            _rule_context_var.set(None)
            _correlation_id_var.set(None)
            _tool_outcome_var.set(None)
            _proxied_raw_result_var.set(None)
            _tool_error_detail_var.set(None)

            # ── Audit: log tool execution ───────────────────────────────
            try:
                from preloop.plugins.base import get_plugin_manager

                plugin_manager = get_plugin_manager()
                audit_service = plugin_manager.get_service("audit_service")
                if audit_service:
                    audit_scope = StorageScope(
                        target="tool.args",
                        tool_name=client_tool_name,
                        server_name=scope_server_name,
                        managed_agent_id=scope_agent_id,
                    )
                    audit_service.log_tool_call_async(
                        db_factory=lambda: next(get_db()),
                        account_id=uuid.UUID(user_context.account_id),
                        user_id=uuid.UUID(user_context.user_id),
                        # The name the client called, not the internal
                        # ``account_<id>_`` router name: the audit trail,
                        # the Activity feed and the policy sub-events all
                        # show the tool the agent asked for.
                        tool_name=client_tool_name,
                        # Credential scrub first, then the account's redact
                        # rules: the chain hashes the redacted row (#1123).
                        # Under reference-only the record also gets the
                        # result's fingerprint and $result kept fields; the
                        # result itself is not stored (#1368).
                        tool_args=attach_result_to_reference(
                            user_context.account_id,
                            apply_storage_redaction(
                                user_context.account_id,
                                redact_dict(arguments),
                                scope=audit_scope,
                                config=storage_config,
                            ),
                            result=result,
                            scope=audit_scope,
                            config=storage_config,
                        ),
                        result=audit_status,
                        duration_ms=elapsed_ms,
                        policy_decision=None,
                        # The access rule the evaluator matched for this
                        # call, as on its policy_* row (same correlation_id).
                        rule_matched=matched_rule_description,
                        correlation_id=correlation_id,
                        runtime_session_id=user_context.runtime_session_id,
                        runtime_principal_type=user_context.runtime_principal_type,
                        runtime_principal_id=user_context.runtime_principal_id,
                        runtime_principal_name=user_context.runtime_principal_name,
                        api_key_id=user_context.api_key_id,
                        api_key_name=user_context.api_key_name,
                        **_audit_grant_kwargs(audit_service, grant_binding),
                        **_audit_error_kwargs(
                            audit_service, audit_error_code, audit_error_reason
                        ),
                    )
            except Exception as audit_err:
                logger.debug(f"Failed to audit tool execution: {audit_err}")

            # ── Runtime session activity persistence ──────────────────────
            # One usage row per governed call, carrying the outcome
            # (succeeded/refused/failed) and a bounded argument summary, so the
            # execution timeline can tell a refusal from a success. A wrapper
            # denial returns ToolResult(is_error=True) with a stamped outcome.
            # An un-stamped error result means the handler ran and failed
            # (FastMCP turning a raise into is_error); that is failed, not
            # refused. Refused is only the stamped and _refuse paths.
            if user_context is not None:
                result_error_text = _tool_result_error_text(result)
                if exec_status == "failed":
                    activity_status = TOOL_CALL_STATUS_FAILED
                    activity_summary = exec_error
                elif wrapper_outcome in (
                    TOOL_CALL_STATUS_REFUSED,
                    TOOL_CALL_STATUS_FAILED,
                ):
                    activity_status = wrapper_outcome
                    activity_summary = result_error_text or exec_error
                elif result_error_text is not None:
                    activity_status = TOOL_CALL_STATUS_FAILED
                    activity_summary = result_error_text
                else:
                    activity_status = TOOL_CALL_STATUS_SUCCEEDED
                    activity_summary = None
                try:
                    self._persist_tool_call_activity(
                        user_context,
                        tool_name=name,
                        client_tool_name=client_tool_name,
                        status=activity_status,
                        summary=activity_summary,
                        arguments=arguments,
                        correlation_id=correlation_id,
                        elapsed_ms=elapsed_ms,
                        storage_config=storage_config,
                    )
                except Exception as activity_err:
                    logger.debug(
                        f"Failed to persist runtime session activity: {activity_err}"
                    )

                # ── Browser step derived from a Playwright MCP call ──────
                # Observation only: the step records the call the firewall
                # forwarded. A failure here is logged and never changes the
                # result the agent receives.
                if client_tool_name in self._proxied_tool_servers:
                    try:
                        self._persist_playwright_browser_step(
                            user_context,
                            client_tool_name=client_tool_name,
                            arguments=arguments,
                            status=activity_status,
                            correlation_id=correlation_id,
                            raw_result=proxied_raw_result,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to derive browser step from tool '%s'",
                            client_tool_name,
                        )

            try:
                from preloop.services.otel_export import emit_tool_call

                emit_tool_call(
                    tool_name=client_tool_name,
                    runtime_session_id=user_context.runtime_session_id,
                    account_id=user_context.account_id,
                    status=exec_status,
                    duration_ms=elapsed_ms,
                    server_name=self._proxied_tool_server_names.get(
                        client_tool_name, "preloop-mcp"
                    ),
                )
            except Exception:
                logger.debug("OTLP tool export failed", exc_info=True)

        return result

    async def _enforce_sensitive_result_policy(
        self,
        user_context: UserContext,
        *,
        config: Any,
        detector_config: Any,
        client_tool_name: str,
        server_name: str,
        managed_agent_id: Optional[str],
        result: Any,
        correlation_id: Optional[str],
    ) -> Any:
        """Apply ``tool.result`` sensitive-data rules to an executed result.

        deny replaces the result with a refusal; require_approval holds the
        result on the approval workflow and releases it only when approved;
        notify records a notice and returns the result unchanged. A result
        that is already an error is not scanned.
        """
        if result is None or getattr(result, "is_error", False):
            return result
        outcome = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: sensitive_tool_policy.evaluate_tool_target(
                config=config,
                detector_config=detector_config,
                target="tool.result",
                payload=result,
                tool_name=client_tool_name,
                server_name=server_name,
                managed_agent_id=managed_agent_id,
                account_id=user_context.account_id,
                user_id=user_context.user_id,
                correlation_id=correlation_id,
            ),
        )
        if outcome.action in ("allow", "notify", "redact"):
            upstream_types = outcome.upstream_redaction_types(config)
            if upstream_types:
                # redact_upstream on tool.result: the agent sees redacted text.
                return sensitive_tool_policy.redact_tool_result(
                    result,
                    (
                        detector_config or sensitive_tool_policy.DetectorConfig()
                    ).with_types(upstream_types),
                )
            return result
        if outcome.action == "deny":
            return _wrapper_tool_error(
                _sensitive_denial_text(outcome, where="the tool result"),
                status=TOOL_CALL_STATUS_REFUSED,
            )
        # require_approval: hold the result until a human decides.
        workflow_id = await asyncio.get_event_loop().run_in_executor(
            None,
            _resolve_approval_workflow_id,
            user_context.account_id,
            outcome.approval_workflow,
        )
        if not workflow_id:
            logger.error(
                f"Tool '{client_tool_name}' result matched sensitive data rule "
                f"'{outcome.rule_id}' (require_approval) but no approval "
                "workflow is configured. Withholding the result."
            )
            return _wrapper_tool_error(
                f"Result withheld: tool '{client_tool_name}' requires approval "
                "but no approval workflow is configured for this account.",
                status=TOOL_CALL_STATUS_REFUSED,
            )
        from preloop.services.approval_helper import require_approval

        summary = outcome.detector_summary()
        approved, error = await require_approval(
            tool_name=client_tool_name,
            tool_source="mcp"
            if client_tool_name in self._proxied_tool_servers
            else "builtin",
            account_id=user_context.account_id,
            # Hash and detector summary only: the approval row never holds
            # the result text.
            arguments={
                "target": "tool.result",
                "rule_id": outcome.rule_id,
                "detector_summary": summary,
                "text_sha256": summary
                and (outcome.summary or outcome.scan).text_sha256,
            },
            workflow_id=str(workflow_id),
            correlation_id=correlation_id,
            rule_context=outcome.rule_context(),
        )
        if approved:
            return result
        return _wrapper_tool_error(
            error or "Result withheld: approval was not granted.",
            status=TOOL_CALL_STATUS_REFUSED,
        )

    def _persist_tool_call_activity(
        self,
        user_context: UserContext,
        *,
        tool_name: str,
        client_tool_name: str,
        status: str,
        summary: Optional[str],
        arguments: Optional[dict[str, Any]],
        correlation_id: Optional[str],
        elapsed_ms: Optional[int] = None,
        storage_config: Any = None,
    ) -> None:
        """Write one governed tool-call outcome and fan it out to live streams.

        This is the single place a usage row is created for a governed call,
        whether it succeeded, was refused before execution, or failed in
        transport. The ``arguments`` payload is reduced to a bounded summary
        (key names and sizes) so the row can flag an oversized or malformed
        call without retaining customer data.
        """
        if not getattr(user_context, "runtime_session_id", None):
            return

        from preloop.models.crud import crud_runtime_session_activity
        from preloop.services.account_realtime import (
            ACCOUNT_TOPIC_AUDIT,
            ACCOUNT_TOPIC_GATEWAY_ACTIVITY,
            ACCOUNT_TOPIC_MANAGED_AGENTS,
            ACCOUNT_TOPIC_RUNTIME_SESSIONS,
            build_account_event,
            emit_account_event,
        )

        server_name = self._proxied_tool_server_names.get(
            client_tool_name, "preloop-mcp"
        )
        # Error and result text can carry values; apply the account's redact
        # rules before the row is written (#1123).
        bounded_summary = _bounded_summary(
            apply_storage_redaction(
                user_context.account_id,
                summary,
                scope=StorageScope(
                    tool_name=client_tool_name,
                    server_name=server_name,
                    managed_agent_id=getattr(user_context, "managed_agent_id", None),
                ),
                config=storage_config,
            )
        )
        arguments_summary = _summarize_arguments(arguments)
        # An unsalted hash of the arguments can be confirmed offline from a
        # guessed value, so a call under a reference-only rule keeps none.
        arguments_hash = (
            None
            if _reference_only_in_scope(
                user_context.account_id,
                tool_name=client_tool_name,
                server_name=server_name,
                managed_agent_id=getattr(user_context, "managed_agent_id", None),
                config=storage_config,
            )
            else _hash_arguments(arguments)
        )
        from datetime import datetime, timedelta, timezone

        ended_at = datetime.now(timezone.utc)
        metadata: dict[str, Any] = {
            "correlation_id": correlation_id,
            # Key names and sizes only: the usage timeline must never
            # carry the argument payload. arguments_hash lets loop
            # detection tell same-shape calls apart.
            "arguments_summary": arguments_summary,
            "arguments_hash": arguments_hash,
        }
        if elapsed_ms is not None:
            # Parsed "detected" markers are stamped at call start; this row
            # is stamped at call end. started_at lets the timeline match
            # them across the whole call, not a fixed 5s window.
            started_at = ended_at - timedelta(milliseconds=max(int(elapsed_ms), 0))
            metadata["started_at"] = started_at.isoformat()
        # Persist the client-visible name so the execution timeline can match
        # recorded rows to parsed markers (proxied tools use an internal
        # account_<id>_<tool> name only for FastMCP dispatch).
        persisted_tool_name = client_tool_name
        db = next(get_db())
        try:
            activity = crud_runtime_session_activity.log_tool_call(
                db,
                account_id=user_context.account_id,
                runtime_session_id=user_context.runtime_session_id,
                flow_execution_id=user_context.flow_execution_id,
                api_key_id=user_context.api_key_id,
                server_name=server_name,
                tool_name=persisted_tool_name,
                status=status,
                summary=bounded_summary,
                metadata=metadata,
                timestamp=ended_at,
            )
            activity_timestamp = (
                activity.timestamp.isoformat() if activity.timestamp else None
            )
            managed_agent = None
            if (
                user_context.runtime_principal_type
                and user_context.runtime_principal_id
            ):
                from preloop.models.crud import crud_managed_agent

                managed_agent = crud_managed_agent.get_by_source(
                    db,
                    account_id=user_context.account_id,
                    session_source_type=user_context.runtime_principal_type,
                    session_source_id=user_context.runtime_principal_id,
                )
            emit_account_event(
                build_account_event(
                    account_id=user_context.account_id,
                    topic=ACCOUNT_TOPIC_RUNTIME_SESSIONS,
                    event_type="runtime_session_updated",
                    payload={
                        "runtime_session_id": str(user_context.runtime_session_id),
                        "session_source_type": user_context.runtime_principal_type,
                        "session_source_id": user_context.runtime_principal_id,
                        "session_reference": user_context.runtime_principal_name,
                        "runtime_principal_type": user_context.runtime_principal_type,
                        "runtime_principal_id": user_context.runtime_principal_id,
                        "runtime_principal_name": user_context.runtime_principal_name,
                        "last_activity_at": activity_timestamp,
                        "tool_name": persisted_tool_name,
                        "server_name": server_name,
                        "status": status,
                    },
                    runtime_session_id=user_context.runtime_session_id,
                    execution_id=user_context.flow_execution_id,
                )
            )
            emit_account_event(
                build_account_event(
                    account_id=user_context.account_id,
                    topic=ACCOUNT_TOPIC_GATEWAY_ACTIVITY,
                    event_type="mcp_call",
                    payload={
                        "runtime_session_id": str(user_context.runtime_session_id),
                        "runtime_principal_type": user_context.runtime_principal_type,
                        "runtime_principal_id": user_context.runtime_principal_id,
                        "runtime_principal_name": user_context.runtime_principal_name,
                        "managed_agent_id": str(managed_agent.id)
                        if managed_agent is not None
                        else user_context.managed_agent_id,
                        "api_key_id": user_context.api_key_id,
                        "api_key_name": user_context.api_key_name,
                        "server_name": server_name,
                        "tool_name": persisted_tool_name,
                        "status": status,
                        "summary": bounded_summary,
                        "correlation_id": correlation_id,
                        "timestamp": activity_timestamp,
                    },
                    runtime_session_id=user_context.runtime_session_id,
                    execution_id=user_context.flow_execution_id,
                )
            )
            emit_account_event(
                build_account_event(
                    account_id=user_context.account_id,
                    topic=ACCOUNT_TOPIC_AUDIT,
                    event_type="audit_event",
                    payload={
                        "action": "tool_call",
                        "runtime_session_id": str(user_context.runtime_session_id),
                        "runtime_principal_type": user_context.runtime_principal_type,
                        "runtime_principal_id": user_context.runtime_principal_id,
                        "runtime_principal_name": user_context.runtime_principal_name,
                        "tool_name": persisted_tool_name,
                        "server_name": server_name,
                        "status": status,
                        "correlation_id": correlation_id,
                    },
                    runtime_session_id=user_context.runtime_session_id,
                    execution_id=user_context.flow_execution_id,
                )
            )
            if managed_agent is not None:
                emit_account_event(
                    build_account_event(
                        account_id=user_context.account_id,
                        topic=ACCOUNT_TOPIC_MANAGED_AGENTS,
                        event_type="managed_agent_updated",
                        payload={
                            "agent_id": str(managed_agent.id),
                            "runtime_session_id": str(user_context.runtime_session_id),
                            "display_name": user_context.runtime_principal_name,
                            "session_source_type": user_context.runtime_principal_type,
                            "session_source_id": user_context.runtime_principal_id,
                            "last_seen_at": activity_timestamp,
                            "tool_name": persisted_tool_name,
                            "server_name": server_name,
                            "status": status,
                        },
                        runtime_session_id=user_context.runtime_session_id,
                        execution_id=user_context.flow_execution_id,
                    )
                )
        finally:
            db.close()

    def _persist_playwright_browser_step(
        self,
        user_context: UserContext,
        *,
        client_tool_name: str,
        arguments: Optional[dict[str, Any]],
        status: str,
        correlation_id: Optional[str],
        raw_result: Any,
    ) -> None:
        """Write one ``browser_step`` for a proxied Playwright MCP tool call.

        Runs after the ``tool_call`` row for the same call. The step reuses
        the call's correlation id as ``source_step_id``, so the two rows join
        and a repeated derivation is a no-op. For ``browser_take_screenshot``
        the first image in the upstream result becomes the step's screenshot
        artifact, validated and bounded the same way as an image posted to
        the browser-steps API. Nothing here changes what the agent receives.

        Skipped when the call has no runtime session, when
        ``mcp_playwright_derive_browser_steps`` is off, when the tool is not a
        mapped Playwright tool, or when the call was refused before it
        reached the browser.

        Args:
            user_context: Caller identity; supplies account and session.
            client_tool_name: Tool name as the agent called it.
            arguments: Client-facing arguments of the call.
            status: ``tool_call`` activity status of the call.
            correlation_id: Correlation id shared with the ``tool_call`` row.
            raw_result: Raw MCP content list from upstream, or ``None``.
        """
        from preloop.config import settings
        from preloop.services.playwright_steps import is_playwright_tool

        if not getattr(user_context, "runtime_session_id", None):
            return
        if not settings.mcp_playwright_derive_browser_steps:
            return
        if not is_playwright_tool(client_tool_name) or not correlation_id:
            return

        from preloop.models.crud import crud_runtime_session_activity
        from preloop.schemas.browser_step import ERROR_STORAGE_BUDGET_EXHAUSTED
        from preloop.services.browser_steps import (
            attach_screenshot,
            enforce_session_screenshot_bound,
            screenshot_bytes_error,
        )
        from preloop.services.playwright_steps import derive_step, extract_screenshot
        from preloop.services.session_search_index import index_browser_step

        account_id = uuid.UUID(str(user_context.account_id))
        runtime_session_id = uuid.UUID(str(user_context.runtime_session_id))

        db = next(get_db())
        try:
            step_index = crud_runtime_session_activity.next_browser_step_index(
                db, runtime_session_id=runtime_session_id
            )
            step = derive_step(
                tool_name=client_tool_name,
                arguments=arguments,
                status=status,
                correlation_id=correlation_id,
                step_index=step_index,
            )
            if step is None:
                return
            row, created = crud_runtime_session_activity.log_browser_step(
                db,
                account_id=account_id,
                runtime_session_id=runtime_session_id,
                api_key_id=user_context.api_key_id,
                step=step,
                commit=False,
            )
            if not created:
                return

            image: Optional[bytes] = None
            content_type: Optional[str] = None
            if step.action == "screenshot":
                extracted = extract_screenshot(raw_result)
                if extracted is not None:
                    media_type, data = extracted
                    error = screenshot_bytes_error(media_type, data)
                    if error is not None:
                        logger.info(
                            "Skipping Playwright screenshot for step %s: %s",
                            correlation_id,
                            error,
                        )
                    else:
                        image, content_type = data, media_type
            if image is not None and content_type is not None:
                # A full account budget drops the image and keeps the step.
                try:
                    with db.begin_nested():
                        attach_screenshot(
                            db,
                            account_id=account_id,
                            runtime_session_id=runtime_session_id,
                            activity=row,
                            content_type=content_type,
                            data=image,
                            source=step.source,
                            source_ref=step.source_step_id,
                        )
                except ValueError as exc:
                    if str(exc) != ERROR_STORAGE_BUDGET_EXHAUSTED:
                        raise
                    logger.info(
                        "Playwright screenshot for step %s not stored: %s",
                        correlation_id,
                        ERROR_STORAGE_BUDGET_EXHAUSTED,
                    )
                else:
                    enforce_session_screenshot_bound(
                        db,
                        account_id=account_id,
                        runtime_session_id=runtime_session_id,
                    )

            index_browser_step(db, activity=row, commit=False)
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise
        finally:
            db.close()

    def _client_visible_registered_name(
        self, name: str, account_id: Optional[str]
    ) -> str:
        """Strip an ``account_<id>_`` prefix so the usage row matches the client."""
        if not account_id:
            return name
        prefix = f"account_{account_id.replace('-', '_')}_"
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix) :]
        return name

    def _audit_refused_tool_call(
        self,
        user_context: UserContext,
        *,
        client_tool_name: str,
        arguments: Optional[dict[str, Any]],
        reason: str,
        correlation_id: Optional[str],
        grant: Optional[dict[str, Any]] = None,
    ) -> None:
        """Write the audit ``tool_call`` row for a call refused before dispatch.

        Status is ``declined`` with a short reason and the call's
        ``correlation_id``, for every caller type. Arguments go through the
        same credential scrub, redact rules and reference-only record as an
        executed call. When the sensitive data block cannot be read (one of
        the refusal causes) only the argument key names are stored, never
        the values. Best effort: an audit failure never changes the refusal.
        """
        try:
            from preloop.plugins.base import get_plugin_manager
            from preloop.services.sensitive_data import storage as storage_module

            audit_service = get_plugin_manager().get_service("audit_service")
            if not audit_service:
                return
            account_id = user_context.account_id
            server_name = self._proxied_tool_server_names.get(
                client_tool_name, BUILTIN_SERVER_NAME
            )
            scope_agent_id = getattr(user_context, "managed_agent_id", None)
            tool_args: Any
            try:
                if storage_module.has_cached_config(account_id):
                    config = storage_module.cached_config(account_id)
                else:
                    # Strict read: ``resolve_config`` would degrade to "no
                    # rules" and store raw values on a failed read.
                    config = storage_module._load_config(account_id)
                    storage_module.prime_cache(account_id, config)
                tool_args = apply_storage_redaction(
                    account_id,
                    redact_dict(arguments or {}),
                    scope=StorageScope(
                        target="tool.args",
                        tool_name=client_tool_name,
                        server_name=server_name,
                        managed_agent_id=scope_agent_id,
                    ),
                    config=config,
                )
            except Exception:
                logger.warning(
                    "Sensitive data policy unavailable; refused call audited "
                    "with argument names only"
                )
                tool_args = {
                    "arguments_withheld": True,
                    "arg_keys": sorted(str(k) for k in (arguments or {})),
                }
            audit_service.log_tool_call_async(
                db_factory=lambda: next(get_db()),
                account_id=uuid.UUID(str(account_id)),
                user_id=uuid.UUID(str(user_context.user_id)),
                tool_name=client_tool_name,
                tool_args=tool_args,
                result=AUDIT_TOOL_CALL_DECLINED,
                duration_ms=0,
                policy_decision=None,
                # The guard that refused the call (kill switch, availability,
                # justification, ...). Audit services without error_reason
                # support (#1280) still show why the call was declined.
                rule_matched=_short_reason(reason),
                correlation_id=correlation_id,
                runtime_session_id=user_context.runtime_session_id,
                runtime_principal_type=user_context.runtime_principal_type,
                runtime_principal_id=user_context.runtime_principal_id,
                runtime_principal_name=user_context.runtime_principal_name,
                api_key_id=user_context.api_key_id,
                api_key_name=user_context.api_key_name,
                **_audit_grant_kwargs(audit_service, grant),
                **_audit_error_kwargs(audit_service, "refused", _short_reason(reason)),
            )
        except Exception as audit_err:
            logger.debug(f"Failed to audit refused tool call: {audit_err}")

    def _record_attributed_refusal(
        self,
        name: str,
        arguments: Optional[dict[str, Any]],
        text: str,
        *,
        account_id: Optional[str] = None,
    ) -> None:
        """Persist a refused row when this request already has a session.

        Denials that return before the governed ``call_tool`` path still
        belong on the timeline. A replay with no HTTP context has no
        session to attribute, and stays silent.
        """
        user_context = self._get_current_user_context()
        if user_context is None:
            return
        owner = account_id or getattr(user_context, "account_id", None)
        client_tool_name = self._client_visible_registered_name(name, owner)
        correlation_id = _correlation_id_var.get(None) or str(uuid.uuid4())
        self._audit_refused_tool_call(
            user_context,
            client_tool_name=client_tool_name,
            arguments=arguments,
            reason=text,
            correlation_id=correlation_id,
        )
        try:
            self._persist_tool_call_activity(
                user_context,
                tool_name=name,
                client_tool_name=client_tool_name,
                status=TOOL_CALL_STATUS_REFUSED,
                summary=text,
                arguments=arguments,
                correlation_id=correlation_id,
            )
        except Exception as exc:  # pragma: no cover - best effort only
            logger.debug("Failed to persist refused tool call '%s': %s", name, exc)

    async def _grant_dispatch_denial(
        self, snapshot: GrantDispatchSnapshot, user_context: UserContext
    ) -> Optional[str]:
        """Recheck after human/connection waits, without resolving another owner."""
        try:
            grant = await _evaluate_snapshot_grant(snapshot)
        except Exception:
            grant = GrantResult(
                grant_introspector._unavailable(), "introspection_unavailable"
            )
        if grant is None:
            return None
        # The central invocation holds this safe dictionary for final audit.
        # Copy before mutating: a mocked/cache result may reuse the same object.
        binding = _grant_binding_var.get(None)
        fresh_binding = deepcopy(grant.binding)
        if binding is not None:
            binding.clear()
            binding.update(fresh_binding)
        if grant.deny_reason:
            _record_grant_denial(
                snapshot.account_id,
                snapshot.tool_name,
                grant,
                user_id=user_context.user_id,
                correlation_id=_correlation_id_var.get(None),
            )
            return f"Access denied: {grant.deny_reason}"
        return None

    async def _halt_dispatch_denial(self, account_id: str) -> Optional[str]:
        """Check fresh halt state after waits and fail closed before dispatch."""

        def check() -> bool:
            db = next(get_db())
            try:
                return "tools" in kill_switch_service.crud_account_halt.active_scopes(
                    db, account_id=account_id
                )
            finally:
                db.close()

        try:
            halted = await asyncio.wait_for(asyncio.to_thread(check), timeout=5)
        except Exception:
            logger.exception("Unable to verify halt state before tool dispatch")
            return "Access denied: unable to verify account halt state"
        return kill_switch_service.TOOL_DENIAL_MESSAGE if halted else None

    async def call_registered_tool_without_policy(
        self,
        name: str,
        arguments: dict | None = None,
        *,
        account_id: str,
    ):
        """Execute an already-registered tool without re-running policy checks.

        Async approval polling calls this only after the original tool call has
        been approved and claimed for idempotent re-execution. A halt denial
        is recorded as refused when the polling request still has a session.
        """
        _grant_dispatch_var.set(None)
        _grant_binding_var.set(None)
        # The durable approval owns this dispatch, even without HTTP context.
        denial = await self._halt_dispatch_denial(account_id)
        if denial:
            self._record_attributed_refusal(
                name, arguments, denial, account_id=account_id
            )
            return _tool_error_result(denial)
        translation_token = None
        if name in self._registered_proxied_tools:
            translation_token = _is_proxy_translation_var.set(True)
        try:
            return await super().call_tool(
                name, self._remap_wrapper_arguments(name, arguments or {})
            )
        finally:
            if translation_token is not None:
                _is_proxy_translation_var.reset(translation_token)


def create_dynamic_mcp_server() -> DynamicFastMCP:
    """Create a DynamicFastMCP server instance.

    Returns:
        Configured DynamicFastMCP instance
    """
    mcp = DynamicFastMCP("preloop-mcp")
    logger.info("Created DynamicFastMCP server")
    return mcp


def create_user_context_from_scope(scope: dict) -> Optional[UserContext]:
    """Extract user context from ASGI scope.

    This is called by middleware to build UserContext from the authenticated
    user information stored in the ASGI scope.

    Args:
        scope: ASGI scope dict with user authentication info

    Returns:
        UserContext if user is authenticated, None otherwise
    """
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

    auth_user = scope.get("user")

    if not isinstance(auth_user, AuthenticatedUser):
        logger.warning("No authenticated user in scope")
        return None

    user = getattr(auth_user.access_token, "user", None)

    if not user:
        logger.warning("No user cached in access token")
        return None

    # Extract API key if available (for flow execution context)
    api_key = getattr(auth_user.access_token, "api_key", None)

    # Check tracker status
    db = next(get_db())
    try:
        # Get user's account for tracker check
        # Handle detached instance by catching the error and querying directly
        from sqlalchemy.orm.exc import DetachedInstanceError
        from preloop.models.crud import crud_account

        try:
            account = user.account if hasattr(user, "account") else None
        except DetachedInstanceError:
            account = None

        if not account:
            # Fallback: query account if relationship not loaded or detached
            account = crud_account.get(db, id=user.account_id)

        user_has_tracker = has_tracker(account, db) if account else False
        user_tracker_types = get_tracker_types(account, db) if account else []

        # Extract flow execution context from API key if present
        flow_execution_id = None
        runtime_session_id = None
        allowed_flow_tools = None
        runtime_principal_type = None
        runtime_principal_id = None
        runtime_principal_name = None
        managed_agent_id = None
        flow_id = None
        api_key_id = str(api_key.id) if api_key else None
        api_key_name = api_key.name if api_key else None
        if api_key and api_key.context_data:
            flow_execution_id = api_key.context_data.get("flow_execution_id")
            if flow_execution_id and api_key.context_data.get("flow_id"):
                flow_id = str(api_key.context_data.get("flow_id"))
            runtime_session_id = api_key.context_data.get("runtime_session_id")
            managed_agent_id = api_key.context_data.get("managed_agent_id")
            runtime_principal = api_key.context_data.get("runtime_principal") or {}
            runtime_principal_type = runtime_principal.get("type")
            runtime_principal_id = runtime_principal.get("id")
            runtime_principal_name = runtime_principal.get("name")
            # Combine allowed_mcp_tools with tool names from allowed_mcp_servers
            allowed_mcp_tools = api_key.context_data.get("allowed_mcp_tools")
            if allowed_mcp_tools is not None:
                # Extract tool names from the allowed_mcp_tools list
                # This could be a list of strings or a list of dicts with "name" or "tool_name" key
                allowed_flow_tools = []
                for tool in allowed_mcp_tools:
                    if isinstance(tool, str):
                        allowed_flow_tools.append(tool)
                    elif isinstance(tool, dict):
                        # Support both "tool_name" (from DB schema) and "name" (legacy)
                        tool_name = tool.get("tool_name") or tool.get("name")
                        if tool_name:
                            allowed_flow_tools.append(tool_name)

                logger.info(
                    f"Flow execution context: execution_id={flow_execution_id}, "
                    f"allowed_tools={allowed_flow_tools}"
                )

        user_context = UserContext(
            user_id=str(user.id),
            account_id=str(user.account_id),
            username=user.username,
            has_tracker=user_has_tracker,
            enabled_default_tools=[],  # Empty = all tools
            enabled_proxied_tools=[],
            tracker_types=user_tracker_types,
            flow_execution_id=flow_execution_id,
            runtime_session_id=runtime_session_id,
            allowed_flow_tools=allowed_flow_tools,
            runtime_principal_type=runtime_principal_type,
            runtime_principal_id=runtime_principal_id,
            runtime_principal_name=runtime_principal_name,
            api_key_id=api_key_id,
            api_key_name=api_key_name,
            managed_agent_id=(
                str(managed_agent_id) if managed_agent_id is not None else None
            ),
            flow_id=flow_id,
        )

        logger.info(
            f"Created user context for {user.username} "
            f"(account: {user.account_id}), has_tracker={user_has_tracker}, "
            f"tracker_types={user_tracker_types}, "
            f"flow_execution_id={flow_execution_id}, "
            f"runtime_session_id={runtime_session_id}, "
            f"runtime_principal={runtime_principal_type}:{runtime_principal_id}"
        )

        return user_context
    finally:
        db.close()


# Cross-module ContextVars (imported by initialize_mcp / approval_helper).
# Referenced here so maintainability scanners do not treat them as unused
# module globals — same pattern as Alembic ``_ALEMBIC_IDENTIFIERS``. Kept
# out of ``__all__`` so ``import *`` stays a public-API surface only.
_CONTEXT_VAR_EXPORTS = (
    _rule_workflow_id_var,
    _rule_context_var,
    _correlation_id_var,
    _justification_var,
    _bypass_approval_var,
    _approved_comment_var,
    _approved_answer_var,
    _approved_id_var,
    _is_proxy_translation_var,
    _proxied_raw_result_var,
)
assert _CONTEXT_VAR_EXPORTS, "contextvar exports must be defined"
