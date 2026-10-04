"""``search_artifacts`` and ``get_artifact``: an agent reads artifacts (#1104).

The read half of the artifact tools. ``deposit_artifact`` (#1081) lets an
agent store a transcript or a report; these two let an agent, typically a
scheduled evaluator, find artifacts by kind, label and time window and read
their content.

Scope follows ``search_sessions`` (:mod:`preloop.services.agent_session_search`)
on purpose, so an operator learns one rule:

* ``own`` (default): artifacts of the sessions the calling agent identity
  ran (``runtime_session.runtime_principal_id`` equals the caller's), across
  runs. A flow execution's principal is the execution id, so for a flow the
  identity is the flow: every execution of it counts as "own". The identity comes from the authenticated credential, never from an
  argument.
* ``account``: every artifact of the account. Needs the
  ``artifact_search.account_scope`` grant in the governance store (read here,
  written by EE). Without it the call is refused naming the grant, never
  quietly narrowed.

``get_artifact`` applies the same scope to one id: an artifact outside it is
answered ``artifact_not_found``, the same answer as an id that does not
exist, so the tool is not an oracle for other agents' artifacts.

Content follows the shared MCP mapping in :mod:`preloop.services.artifact_shapes`:
search results are ``ResourceLink`` blocks, a read is the inline block
(``EmbeddedResource`` text for text kinds, the binary block for small
binaries) or a ``ResourceLink`` when it is too large to inline. Preloop
fields travel in ``_meta["preloop.dev/artifact"]``, plus ``truncated`` when
a text read was cut at ``max_bytes``.

Every call, answered or refused, writes one audit row with the agent as the
actor and ``source="mcp"``, like ``search_sessions`` reads.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_audit_log
from preloop.models.crud import runtime_session_artifact as crud_artifact
from preloop.services import artifact_deposit, session_search_audit
from preloop.services import artifact_shapes as shapes
from preloop.services.agent_session_search import account_scope_granted
from preloop.services.artifact_mcp_tools import absolute_uri
from preloop.tools.builtin_defs import (
    ARTIFACT_READ_SCOPE_ACCOUNT,
    ARTIFACT_READ_SCOPE_OWN,
    ARTIFACT_READ_SCOPES,
    DEPOSIT_ARTIFACT_KINDS,
    GET_ARTIFACT_BLOB_MAX_BYTES,
    GET_ARTIFACT_DEFAULT_MAX_BYTES,
    SEARCH_ARTIFACTS_DEFAULT_LIMIT,
    SEARCH_ARTIFACTS_MAX_LIMIT,
)

logger = logging.getLogger(__name__)

SEARCH_ARTIFACTS_TOOL_NAME = "search_artifacts"
GET_ARTIFACT_TOOL_NAME = "get_artifact"

#: The grant that widens both tools past the caller's own sessions.
ACCOUNT_SCOPE_GRANT = "artifact_search.account_scope"

ERROR_ACCOUNT_SCOPE_NOT_GRANTED = "account_scope_not_granted"
ERROR_NO_AGENT_IDENTITY = "no_agent_identity"
ERROR_INVALID_REQUEST = "invalid_request"
ERROR_NOT_FOUND = "artifact_not_found"
ERROR_UNAVAILABLE = "artifact_unavailable"

AUDIT_RESOURCE_TYPE = "runtime_session_artifact"
AUDIT_ACTION_SEARCH = "query"
AUDIT_ACTION_READ = "read"

_HINTS: Dict[str, str] = {
    ERROR_ACCOUNT_SCOPE_NOT_GRANTED: (
        f"reading every artifact of the account needs the "
        f"'{ACCOUNT_SCOPE_GRANT}' grant, which an operator has not given this "
        "agent; repeat with scope 'own' or ask an operator for the grant"
    ),
    ERROR_NO_AGENT_IDENTITY: (
        "this credential carries no agent identity, so there are no 'own' "
        "sessions; a call made by an agent runtime has one"
    ),
    ERROR_NOT_FOUND: (
        "no artifact with this id in your scope; artifacts of other agents' "
        f"sessions need the '{ACCOUNT_SCOPE_GRANT}' grant"
    ),
    ERROR_UNAVAILABLE: "the artifact's bytes have expired or been evicted",
}


@dataclass(frozen=True)
class ReadOutcome:
    """What the MCP layer turns into a ``CallToolResult``."""

    is_error: bool
    text: str
    blocks: List[Dict[str, Any]] = field(default_factory=list)
    structured: Optional[Dict[str, Any]] = None

    def content(self) -> List[Dict[str, Any]]:
        """The ``CallToolResult.content`` list, as block dicts.

        A successful call leads with ``structuredContent`` serialized as a
        ``TextContent`` block, as the MCP spec recommends for tools that
        return structured output: several clients (OpenCode among them)
        show the model only text blocks, and a search answer made only of
        ``resource_link`` blocks reached the model as an empty string.
        """
        if self.is_error or self.structured is None:
            return [{"type": "text", "text": self.text}]
        return [
            {"type": "text", "text": json.dumps(self.structured, default=str)},
            *self.blocks,
        ]


def _error(code: str, detail: Optional[str] = None) -> ReadOutcome:
    hint = detail or _HINTS.get(code, "check the arguments")
    return ReadOutcome(
        is_error=True,
        text=f"{code}: {hint}",
        structured={"error": {"code": code, "hint": hint}},
    )


@dataclass(frozen=True)
class Caller:
    """The authenticated identity a read is scoped to and audited as."""

    account_id: Any
    runtime_principal_id: Optional[str]
    api_key_id: Optional[str]
    managed_agent_id: Optional[str]
    #: Flow execution the credential belongs to, if any. Its flow is the
    #: identity "own" spans across runs.
    flow_execution_id: Optional[str] = None

    @classmethod
    def from_user_context(cls, user_context: Any) -> "Caller":
        principal = getattr(user_context, "runtime_principal_id", None)
        return cls(
            account_id=user_context.account_id,
            runtime_principal_id=(str(principal).strip() or None)
            if principal
            else None,
            api_key_id=getattr(user_context, "api_key_id", None),
            managed_agent_id=getattr(user_context, "managed_agent_id", None),
            flow_execution_id=getattr(user_context, "flow_execution_id", None),
        )

    def flow_id(self, db: Session) -> Optional[UUID]:
        """The flow this caller's execution belongs to, account bound."""
        execution_id = _as_uuid(self.flow_execution_id)
        if execution_id is None:
            return None
        from preloop.models.models import Flow, FlowExecution

        return (
            db.query(FlowExecution.flow_id)
            .join(Flow, Flow.id == FlowExecution.flow_id)
            .filter(
                FlowExecution.id == execution_id,
                Flow.account_id == self.account_id,
            )
            .scalar()
        )

    def actor(self) -> session_search_audit.SearchActor:
        return session_search_audit.agent_actor(
            managed_agent_id=self.managed_agent_id,
            api_key_id=self.api_key_id,
            runtime_principal_id=self.runtime_principal_id,
            source=session_search_audit.SOURCE_MCP,
        )

    def subject_context(self) -> Dict[str, Optional[str]]:
        return {
            "api_key_id": self.api_key_id,
            "managed_agent_id": self.managed_agent_id,
        }


def _audit(
    db: Session,
    caller: Caller,
    *,
    action: str,
    status: str,
    details: Dict[str, Any],
    resource_id: Optional[str] = None,
) -> None:
    """One audit row per call; auditing never fails the read."""
    try:
        crud_audit_log.log_action(
            db,
            account_id=caller.account_id,
            action=action,
            resource_type=AUDIT_RESOURCE_TYPE,
            resource_id=resource_id,
            status=status,
            details={**caller.actor().as_details(), **details},
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Artifact read audit row could not be written for account %s",
            caller.account_id,
            exc_info=True,
        )


def _resolve_scope(
    db: Session, caller: Caller, scope: Optional[str]
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return ``(scope, principal_filter, refusal_code)``."""
    requested = (scope or ARTIFACT_READ_SCOPE_OWN).strip().lower()
    if requested not in ARTIFACT_READ_SCOPES:
        return requested, None, ERROR_INVALID_REQUEST
    if requested == ARTIFACT_READ_SCOPE_ACCOUNT:
        if not _granted(db, caller):
            return requested, None, ERROR_ACCOUNT_SCOPE_NOT_GRANTED
        return requested, None, None
    if not caller.runtime_principal_id:
        return requested, None, ERROR_NO_AGENT_IDENTITY
    return requested, caller.runtime_principal_id, None


def _granted(db: Session, caller: Caller) -> bool:
    account = crud_account.get(db, id=caller.account_id)
    meta_data = getattr(account, "meta_data", None) if account else None
    return account_scope_granted(
        meta_data,
        subject_context=caller.subject_context(),
        grant=ACCOUNT_SCOPE_GRANT,
    )


def _instant(value: Any, name: str) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} is not ISO 8601: {value!r}") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{name} needs a timezone offset")
    return parsed


def _kinds(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(k, str) for k in value):
        raise ValueError("kind must be a list of artifact kinds")
    unknown = [k for k in value if k not in DEPOSIT_ARTIFACT_KINDS]
    if unknown:
        raise ValueError(
            f"unknown kind {unknown[0]!r}; one of " + ", ".join(DEPOSIT_ARTIFACT_KINDS)
        )
    return list(dict.fromkeys(value)) or None


def _labels(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("labels must be an object of key: value")
    # An empty value means "any" (a preset may ship {site: ""} for the
    # operator to fill in), so it does not narrow the search.
    cleaned = {k: v for k, v in value.items() if v not in ("", None)}
    if not cleaned:
        return None
    try:
        return crud_artifact.validate_labels(cleaned)
    except ValueError as exc:
        raise ValueError(f"labels: {exc}") from None


def _limit(value: Any) -> int:
    if value is None:
        return SEARCH_ARTIFACTS_DEFAULT_LIMIT
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer") from None
    return max(1, min(number, SEARCH_ARTIFACTS_MAX_LIMIT))


def search(db: Session, *, caller: Caller, arguments: Mapping[str, Any]) -> ReadOutcome:
    """Run one ``search_artifacts`` call. Never raises for a bad request."""
    scope, principal, refusal = _resolve_scope(db, caller, arguments.get("scope"))
    audit_filters: Dict[str, Any] = {
        key: arguments.get(key)
        for key in ("kind", "labels", "since", "until", "limit")
        if arguments.get(key) not in (None, "")
    }
    if arguments.get("q"):
        audit_filters["query_hash"] = session_search_audit.query_hash(
            str(arguments.get("q"))
        )

    def refuse(code: str, detail: Optional[str] = None) -> ReadOutcome:
        _audit(
            db,
            caller,
            action=AUDIT_ACTION_SEARCH,
            status=session_search_audit.STATUS_DENIED,
            details={"scope": scope, "reason": code, "filters": audit_filters},
        )
        return _error(code, detail)

    if refusal == ERROR_INVALID_REQUEST:
        return refuse(
            refusal, "scope must be one of: " + ", ".join(ARTIFACT_READ_SCOPES)
        )
    if refusal:
        return refuse(refusal)
    try:
        q = arguments.get("q")
        if q is not None and not isinstance(q, str):
            raise ValueError("q must be a string")
        kinds = _kinds(arguments.get("kind"))
        labels = _labels(arguments.get("labels"))
        since = _instant(arguments.get("since"), "since")
        until = _instant(arguments.get("until"), "until")
        if since and until and until <= since:
            raise ValueError("until must be after since")
        limit = _limit(arguments.get("limit"))
        cursor = arguments.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("cursor must be a string")
        before = artifact_deposit.decode_cursor(cursor) if cursor else None
    except ValueError as exc:
        return refuse(ERROR_INVALID_REQUEST, str(exc))

    rows = crud_artifact.search_page(
        db,
        account_id=caller.account_id,
        runtime_principal_id=principal,
        flow_id=caller.flow_id(db) if principal else None,
        limit=limit + 1,
        kinds=kinds,
        labels=labels,
        since=since,
        until=until,
        query=(q or "").strip() or None,
        before=before,
    )
    page = rows[:limit]
    next_cursor = (
        artifact_deposit.encode_cursor(page[-1][0]) if len(rows) > limit else None
    )
    items: List[Dict[str, Any]] = []
    blocks: List[Dict[str, Any]] = []
    for artifact, excerpt in page:
        descriptor = artifact_deposit.describe(artifact).model_dump(
            mode="json", by_alias=True
        )
        descriptor["content_block"]["uri"] = absolute_uri(
            descriptor["content_block"]["uri"]
        )
        blocks.append(descriptor["content_block"])
        items.append({**descriptor, "excerpt": excerpt})
    _audit(
        db,
        caller,
        action=AUDIT_ACTION_SEARCH,
        status=session_search_audit.STATUS_SUCCESS,
        details={
            "scope": scope,
            "filters": audit_filters,
            "result_count": len(items),
            "artifact_ids": [item["id"] for item in items],
        },
    )
    text = (
        f"{len(items)} artifact(s) in scope '{scope}'"
        + (", more with next_cursor" if next_cursor else "")
        if items
        else f"No artifacts match in scope '{scope}'."
    )
    return ReadOutcome(
        is_error=False,
        text=text,
        blocks=blocks,
        structured={
            "scope": scope,
            "returned": len(items),
            "next_cursor": next_cursor,
            "items": items,
        },
    )


def _max_bytes(value: Any) -> int:
    if value is None:
        return GET_ARTIFACT_DEFAULT_MAX_BYTES
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("max_bytes must be an integer") from None
    if number < 1:
        raise ValueError("max_bytes must be at least 1")
    return min(number, GET_ARTIFACT_BLOB_MAX_BYTES)


def _utf8_prefix(data: bytes, limit: int) -> str:
    """The longest UTF-8 prefix of ``data`` within ``limit`` bytes."""
    return data[:limit].decode("utf-8", errors="ignore")


def get(db: Session, *, caller: Caller, arguments: Mapping[str, Any]) -> ReadOutcome:
    """Run one ``get_artifact`` call. Never raises for a bad request."""
    raw_id = arguments.get("artifact_id")
    scope = ARTIFACT_READ_SCOPE_OWN

    def refuse(code: str, detail: Optional[str] = None) -> ReadOutcome:
        _audit(
            db,
            caller,
            action=AUDIT_ACTION_READ,
            status=session_search_audit.STATUS_DENIED,
            resource_id=str(raw_id)[:64] if raw_id else None,
            details={"scope": scope, "reason": code},
        )
        return _error(code, detail)

    try:
        artifact_id = UUID(str(raw_id))
        max_bytes = _max_bytes(arguments.get("max_bytes"))
    except ValueError as exc:
        return refuse(ERROR_INVALID_REQUEST, str(exc) or "artifact_id must be a UUID")

    artifact = crud_artifact.get(
        db, account_id=caller.account_id, artifact_id=artifact_id
    )
    if artifact is None:
        return refuse(ERROR_NOT_FOUND)
    if not _owned(db, caller, artifact):
        if not _granted(db, caller):
            return refuse(ERROR_NOT_FOUND)
        scope = ARTIFACT_READ_SCOPE_ACCOUNT
    if artifact.availability != "available":
        return refuse(ERROR_UNAVAILABLE)
    try:
        data = crud_artifact.decrypt(artifact)
    except ValueError:
        return refuse(ERROR_UNAVAILABLE)

    uri = absolute_uri(
        artifact_deposit.artifact_uri(artifact.runtime_session_id, artifact.id)
    )
    textual = shapes._is_textual(artifact.content_type)
    truncated = False
    if textual:
        truncated = len(data) > max_bytes
        payload = shapes.ArtifactPayload(
            kind=artifact.kind,
            name=artifact.name,
            content_type=artifact.content_type,
            text=_utf8_prefix(data, max_bytes)
            if truncated
            else data.decode("utf-8", errors="replace"),
            labels=dict(artifact.labels or {}),
            sha256=artifact.sha256,
        )
        block = shapes.to_mcp_content_block(
            payload,
            uri=uri,
            inline=True,
            artifact_id=str(artifact.id),
            producer=artifact.producer,
        )
    else:
        payload = shapes.ArtifactPayload(
            kind=artifact.kind,
            name=artifact.name,
            content_type=artifact.content_type,
            data=data,
            labels=dict(artifact.labels or {}),
            sha256=artifact.sha256,
        )
        block = shapes.to_mcp_content_block(
            payload,
            uri=uri,
            inline=len(data) <= GET_ARTIFACT_BLOB_MAX_BYTES,
            artifact_id=str(artifact.id),
            producer=artifact.producer,
        )
    meta = block["_meta"][shapes.META_KEY]
    meta["truncated"] = truncated
    meta["size_bytes"] = int(artifact.size_bytes)
    _audit(
        db,
        caller,
        action=AUDIT_ACTION_READ,
        status=session_search_audit.STATUS_SUCCESS,
        resource_id=str(artifact.id),
        details={
            "scope": scope,
            "runtime_session_id": str(artifact.runtime_session_id),
            "kind": artifact.kind,
            "returned_block": block["type"],
            "truncated": truncated,
        },
    )
    descriptor = artifact_deposit.describe(artifact).model_dump(
        mode="json", by_alias=True
    )
    descriptor["content_block"]["uri"] = uri
    descriptor["truncated"] = truncated
    return ReadOutcome(
        is_error=False,
        text=f"{artifact.kind} {artifact.name or artifact.id}"
        + (f" (first {max_bytes} bytes)" if truncated else ""),
        blocks=[block],
        structured=descriptor,
    )


def _owned(db: Session, caller: Caller, artifact: Any) -> bool:
    if not caller.runtime_principal_id:
        return False
    own = crud_artifact.own_session_ids(
        account_id=caller.account_id,
        runtime_principal_id=caller.runtime_principal_id,
        flow_id=caller.flow_id(db),
    )
    from preloop.models.models import RuntimeSession

    return (
        db.query(RuntimeSession.id)
        .filter(
            RuntimeSession.id == artifact.runtime_session_id,
            RuntimeSession.id.in_(own),
        )
        .first()
        is not None
    )


def _as_uuid(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


__all__ = [
    "ACCOUNT_SCOPE_GRANT",
    "Caller",
    "GET_ARTIFACT_TOOL_NAME",
    "ReadOutcome",
    "SEARCH_ARTIFACTS_TOOL_NAME",
    "get",
    "search",
]
