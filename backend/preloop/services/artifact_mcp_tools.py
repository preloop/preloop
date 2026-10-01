"""The ``deposit_artifact`` MCP tool: an agent stores an artifact on its session.

Agents in third-party sandboxes reach Preloop only through the MCP endpoint,
so this tool is their deposit path. It is a thin adapter over
:mod:`preloop.services.artifact_deposit` (#1080): the same storage rules, the
same timeline row, the same error codes. What it adds:

* The session comes from the session-bound credential, never an argument. A
  credential without one is refused with ``artifact_no_session``.
* ``resource_link`` is accepted only for an artifact of the caller's own
  session. It copies those bytes into a new artifact (new name, kind or
  labels) whose parent is the linked one, so lineage is explicit.
* ``labels.source_tool`` becomes the artifact's ``tool_name``.
* The answer is an MCP ``CallToolResult``: one ``resource_link`` block and the
  #1080 descriptor as ``structuredContent``. Refusals are tool errors whose
  text starts with the same code string the REST API returns.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import runtime_session_artifact as crud_artifact
from preloop.services import artifact_deposit
from preloop.services import artifact_shapes as shapes
from preloop.services.artifact_deposit import ArtifactDepositError
from preloop.services.model_gateway_auth import ModelGatewayAuthContext, NO_BEARER_TOKEN
from preloop.tools.builtin_defs import DEPOSIT_ARTIFACT_BLOCK_TYPES

logger = logging.getLogger(__name__)

DEPOSIT_ARTIFACT_TOOL_NAME = "deposit_artifact"
PRODUCER_DEPOSIT_MCP = "deposit_mcp"
SOURCE_TOOL_LABEL = "source_tool"
NAME_MAX_CHARS = 255
TOOL_NAME_MAX_CHARS = 255

ERROR_NO_SESSION = "artifact_no_session"
ERROR_REQUEST_INVALID = "artifact_request_invalid"
ERROR_LINK_OUTSIDE_SESSION = "artifact_link_outside_session"

#: One line per code the agent can act on. Codes not listed get the generic
#: hint; the code itself is always the first token of the error text.
_HINTS: dict[str, str] = {
    ERROR_NO_SESSION: (
        "this credential is not bound to a runtime session; connect with a "
        "key from POST /api/v1/auth/runtime-sessions/token and retry"
    ),
    ERROR_REQUEST_INVALID: "check name (1-255 chars), content and labels",
    ERROR_LINK_OUTSIDE_SESSION: (
        "resource_link must point at an artifact of your own session"
    ),
    artifact_deposit.ERROR_TOO_LARGE: "content is over the cap for this kind",
    artifact_deposit.ERROR_CONTENT_REQUIRED: "the block carries no bytes or text",
    artifact_deposit.ERROR_AUDIO_DISABLED: "audio storage is off for this account",
    artifact_deposit.ERROR_ACTIVITY_INVALID: "activity_id is not in this session",
    "storage_budget_exhausted": "the account artifact budget is full",
    "artifact_media_type_invalid": "this mimeType is not allowed for the kind",
    "artifact_content_mismatch": "the bytes do not match the declared mimeType",
}

_ARTIFACT_PATH = re.compile(
    r"/api/v1/runtime-sessions/(?P<session>[0-9a-fA-F-]{36})"
    r"/artifacts/(?P<artifact>[0-9a-fA-F-]{36})/?$"
)


@dataclass(frozen=True)
class ToolOutcome:
    """What the MCP layer turns into a ``CallToolResult``."""

    is_error: bool
    text: str
    content_block: dict[str, Any] | None = None
    structured: dict[str, Any] | None = None


def error(code: str, status: int | None = None) -> ToolOutcome:
    """A tool error whose text starts with the stable code."""
    hint = _HINTS.get(code, "see the Preloop artifact API docs for this code")
    structured: dict[str, Any] = {"error": {"code": code, "hint": hint}}
    if status is not None:
        structured["error"]["status"] = status
    return ToolOutcome(is_error=True, text=f"{code}: {hint}", structured=structured)


def auth_from_user_context(db: Session, user_context: Any) -> ModelGatewayAuthContext:
    """Rebuild the gateway auth context the deposit service expects.

    The MCP middleware already authenticated the bearer; this reloads the
    user and key rows by id so the session binding is read from the key, as
    the REST route does.
    """
    user = db.get(models.User, _uuid(getattr(user_context, "user_id", None)))
    if user is None:
        raise ArtifactDepositError(401, ERROR_NO_SESSION)
    api_key = None
    api_key_id = _uuid(getattr(user_context, "api_key_id", None))
    if api_key_id is not None:
        api_key = db.get(models.ApiKey, api_key_id)
    return ModelGatewayAuthContext(token=NO_BEARER_TOKEN, user=user, api_key=api_key)


def deposit_from_mcp(
    db: Session, *, user_context: Any, arguments: Mapping[str, Any]
) -> ToolOutcome:
    """Run one ``deposit_artifact`` call. Never raises for a bad request."""
    try:
        return _deposit(db, user_context=user_context, arguments=arguments)
    except ArtifactDepositError as exc:
        return error(exc.code, exc.status_code)


def _deposit(
    db: Session, *, user_context: Any, arguments: Mapping[str, Any]
) -> ToolOutcome:
    session_id = getattr(user_context, "runtime_session_id", None)
    auth = auth_from_user_context(db, user_context)
    # The key's binding is the authority; the context copy is a fallback for
    # credentials (OAuth) that carry it only there.
    session_id = auth.runtime_session_id or session_id
    if not session_id:
        raise ArtifactDepositError(400, ERROR_NO_SESSION)

    content = arguments.get("content")
    name = arguments.get("name")
    kind = arguments.get("kind")
    labels = arguments.get("labels")
    if (
        not isinstance(content, Mapping)
        or content.get("type") not in DEPOSIT_ARTIFACT_BLOCK_TYPES
        or not isinstance(name, str)
        or not name.strip()
        or len(name) > NAME_MAX_CHARS
        or (labels is not None and not isinstance(labels, Mapping))
        or (kind is not None and not isinstance(kind, str))
    ):
        raise ArtifactDepositError(422, ERROR_REQUEST_INVALID)

    parent_id = arguments.get("parent_artifact_id")
    if content.get("type") == shapes.MCP_TYPE_RESOURCE_LINK:
        payload, linked_id = _payload_from_link(
            db, auth=auth, session_id=session_id, block=content, kind=kind
        )
        parent_id = parent_id or linked_id
    else:
        payload = artifact_deposit.payload_from_content_block(content, kind=kind)

    merged_labels = dict(payload.labels)
    merged_labels.update(labels or {})
    source_tool = merged_labels.get(SOURCE_TOOL_LABEL)
    tool_name = (
        source_tool
        if isinstance(source_tool, str) and 0 < len(source_tool) <= TOOL_NAME_MAX_CHARS
        else None
    )

    result = artifact_deposit.deposit(
        db,
        auth=auth,
        runtime_session_id=str(session_id),
        payload=payload,
        name=name,
        labels=merged_labels or None,
        activity_id=arguments.get("activity_id"),
        parent_artifact_id=parent_id,
        tool_name=tool_name,
        producer=PRODUCER_DEPOSIT_MCP,
    )
    descriptor = result.artifact.model_dump(mode="json", by_alias=True)
    block = descriptor["content_block"]
    # MCP ResourceLink.uri is an absolute URI; the REST descriptor carries
    # the path, so the public base URL is prefixed here.
    block["uri"] = absolute_uri(block["uri"])
    return ToolOutcome(
        is_error=False,
        text=f"Stored {descriptor['kind']} {descriptor['name']} ({descriptor['id']})",
        content_block=block,
        structured=descriptor,
    )


def absolute_uri(path: str) -> str:
    """Prefix the deployment's public URL to an API path."""
    return settings.preloop_url.rstrip("/") + path


def _payload_from_link(
    db: Session,
    *,
    auth: ModelGatewayAuthContext,
    session_id: str,
    block: Mapping[str, Any],
    kind: str | None,
) -> tuple[shapes.ArtifactPayload, str]:
    """Copy an artifact of the caller's own session, for re-labelling."""
    uri = block.get("uri")
    match = _ARTIFACT_PATH.search(urlparse(uri).path if isinstance(uri, str) else "")
    if match is None or match["session"].lower() != str(session_id).lower():
        raise ArtifactDepositError(403, ERROR_LINK_OUTSIDE_SESSION)
    source = crud_artifact.get(
        db, account_id=auth.account_id, artifact_id=UUID(match["artifact"])
    )
    if source is None or str(source.runtime_session_id) != str(session_id).lower():
        raise ArtifactDepositError(403, ERROR_LINK_OUTSIDE_SESSION)
    try:
        data = crud_artifact.decrypt(source)
    except ValueError as exc:
        raise ArtifactDepositError(410, str(exc)) from None
    meta = shapes._preloop_meta(block.get("_meta"))
    payload = shapes.make_payload(
        kind=kind or source.kind,
        name=source.name,
        content_type=source.content_type,
        data=data,
        labels={**dict(source.labels or {}), **dict(meta.get("labels") or {})},
    )
    return payload, str(source.id)


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value is not None else None
    except ValueError:
        return None
