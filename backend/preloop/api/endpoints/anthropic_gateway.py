"""Anthropic-compatible gateway endpoints.

Serves Claude Code, Claude Desktop in gateway mode (``inferenceProvider:
gateway``) and a customer-run Claude apps gateway that uses Preloop as its
``provider: anthropic`` upstream. A trusted upstream key
(``model_gateway:trusted_upstream``) may name the developer behind each
request in identity headers; see
:mod:`preloop.services.gateway_upstream_identity`.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from preloop.api.deps import get_budget_enforcer
from preloop.models.db.session import get_db_session
from preloop.services.agent_session_headers import (
    CLAUDE_CODE_AGENT_ID_HEADER,
    claude_code_session_lineage,
)
from preloop.services.gateway_streaming import GatewayStreamingResponse
from preloop.services.gateway_upstream_identity import (
    CLIENT_UNKNOWN,
    GATEWAY_SOURCE_APPS_GATEWAY,
    GATEWAY_SOURCE_DIRECT,
    apply_trusted_upstream,
    detect_client,
    fallback_session_id,
    to_trusted_upstream_error,
)
from preloop.services.gateway_idp_auth import authenticate_idp_bearer
from preloop.services.model_gateway_auth import (
    ModelGatewayAuthContext,
    authenticate_bearer_token,
)
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService

router = APIRouter(include_in_schema=False)

#: Request headers Preloop already handles itself; every other ``anthropic-*``
#: header is relayed verbatim.
_HANDLED_ANTHROPIC_HEADERS = frozenset({"anthropic-version", "anthropic-beta"})


async def get_anthropic_gateway_auth_context(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    authorization: Optional[str] = Header(None),
    anthropic_version: Optional[str] = Header(None, alias="anthropic-version"),
    db: Session = Depends(get_db_session),
) -> ModelGatewayAuthContext:
    """Authenticate an Anthropic-compatible gateway request.

    On a trusted upstream key the upstream secret is checked (401 when
    configured and missing or wrong) and the identity headers resolve the
    gateway subject. On any other credential the identity headers are
    ignored.
    """
    if not anthropic_version:
        raise ModelGatewayAPIError(
            provider="anthropic",
            status_code=400,
            message="Missing anthropic-version header",
        )

    token = x_api_key
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    if not token:
        raise ModelGatewayAPIError(
            provider="anthropic",
            status_code=401,
            message="Missing API key",
        )

    if not x_api_key:
        # Only a bearer can be a customer IdP token; ``x-api-key`` never is.
        idp_context = await authenticate_idp_bearer(token, db)
        if idp_context is not None:
            return idp_context

    auth_context = await authenticate_bearer_token(token, db, owns_db_session=True)
    if not auth_context:
        raise ModelGatewayAPIError(
            provider="anthropic",
            status_code=401,
            message="Invalid authentication credentials",
        )
    return await apply_trusted_upstream(auth_context, request.headers, db)


def _extra_anthropic_headers(request: Request) -> Dict[str, str]:
    """Client ``anthropic-*`` headers other than version and beta."""
    return {
        name.lower(): value
        for name, value in request.headers.items()
        if name.lower().startswith("anthropic-")
        and name.lower() not in _HANDLED_ANTHROPIC_HEADERS
    }


def _attribution(
    request: Request, auth_context: ModelGatewayAuthContext
) -> Dict[str, Any]:
    """Usage ``meta_data`` attribution fields for this request."""
    subject = auth_context.gateway_subject
    attribution = {
        "gateway_source": (
            GATEWAY_SOURCE_APPS_GATEWAY
            if auth_context.trusted_upstream
            else GATEWAY_SOURCE_DIRECT
        ),
        "client": detect_client(request.headers) or CLIENT_UNKNOWN,
        "gateway_subject_id": str(subject.id) if subject is not None else None,
        "gateway_subject_email": subject.email if subject is not None else None,
    }
    if auth_context.auth_method is not None:
        attribution["auth_method"] = auth_context.auth_method
        attribution["idp_provider_id"] = str(auth_context.idp_provider_id)
    return attribution


def _run(
    auth_context: ModelGatewayAuthContext,
    service: OpenAIGatewayService,
    call: Callable[[], Any],
) -> Any:
    """Run a service call, mapping denials to the trusted upstream contract.

    Only a trusted upstream request that named a developer gets the 429
    contract; every other request keeps its error unchanged (403 for budget
    denials). Relayable upstream response headers ride on the error too.
    """
    try:
        return call()
    except ModelGatewayAPIError as exc:
        error = exc
        if auth_context.gateway_subject is not None:
            error = to_trusted_upstream_error(exc, auth_context.gateway_subject)
        if service.upstream_response_headers:
            error.extra_response_headers = dict(  # type: ignore[attr-defined]
                service.upstream_response_headers
            )
        if error is exc:
            raise
        raise error from exc


def _with_upstream_headers(result: Any, service: OpenAIGatewayService) -> Any:
    """Attach relayable upstream response headers to a JSON result."""
    if service.upstream_response_headers and isinstance(result, dict):
        return JSONResponse(content=result, headers=service.upstream_response_headers)
    return result


@router.post("/messages")
def create_message(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db_session),
    auth_context: ModelGatewayAuthContext = Depends(get_anthropic_gateway_auth_context),
    budget_enforcer: Any = Depends(get_budget_enforcer),
    x_preloop_session_id: Optional[str] = Header(None, alias="X-Preloop-Session-Id"),
    x_claude_code_session_id: Optional[str] = Header(
        None, alias="X-Claude-Code-Session-Id"
    ),
    x_claude_code_agent_id: Optional[str] = Header(
        None, alias=CLAUDE_CODE_AGENT_ID_HEADER
    ),
    anthropic_version: Optional[str] = Header(None, alias="anthropic-version"),
    anthropic_beta: Optional[str] = Header(None, alias="anthropic-beta"),
) -> Any:
    """Create an Anthropic-compatible message.

    The ``anthropic-version`` and ``anthropic-beta`` headers, and any other
    ``anthropic-*`` header, are forwarded so the subscription-OAuth
    passthrough preserves the client's requested API surface (e.g.
    prompt-caching betas) upstream.

    Claude Code does not send ``X-Preloop-Session-Id``; it stamps its own
    conversation id on ``X-Claude-Code-Session-Id`` (and, redundantly, inside
    ``metadata.user_id``). Accepting it here means each real Claude Code session
    gets its own runtime session instead of every run on a machine collapsing
    onto one eternal session row. Preloop's own header still wins when both are
    present, so an explicit per-run id is never overridden.

    A subagent's turns carry the parent's session id plus their own
    ``X-Claude-Code-Agent-Id``, so they are keyed by both and record the
    session they were spawned from (see
    :func:`preloop.services.agent_session_headers.claude_code_session_lineage`).
    A turn without that header is a parent turn and is keyed exactly as before.

    A trusted upstream request that names a developer but carries no session
    header is grouped as ``gw:<gateway_subject_id>:<UTC date>``.
    """
    lineage = claude_code_session_lineage(
        x_claude_code_session_id, x_claude_code_agent_id
    )
    subject = auth_context.gateway_subject
    client_session_id = x_preloop_session_id or lineage.session_id
    if client_session_id is None and subject is not None:
        client_session_id = fallback_session_id(subject)
    service = OpenAIGatewayService(
        db,
        auth_context,
        budget_enforcer=budget_enforcer,
        owns_db_session=True,
        client_session_id=client_session_id,
        # Claude Code's vendor header is read without a principal-type gate,
        # so a plain API key must not be opted in by it: only the explicit
        # Preloop header does that. A developer named by a trusted upstream
        # key is a known principal, so its sessions are recorded too.
        client_session_id_is_explicit=bool(x_preloop_session_id) or subject is not None,
        client_parent_session_id=(
            None if x_preloop_session_id else lineage.parent_session_id
        ),
    )
    service.gateway_attribution = _attribution(request, auth_context)
    service.extra_anthropic_headers = _extra_anthropic_headers(request)
    if payload.get("stream"):
        events = _run(
            auth_context,
            service,
            lambda: service.stream_message(
                payload,
                anthropic_version=anthropic_version,
                anthropic_beta=anthropic_beta,
            ),
        )
        return GatewayStreamingResponse(
            events,
            media_type="text/event-stream",
            headers=service.upstream_response_headers or None,
            on_complete=service.flush_deferred_stream_record,
        )
    result = _run(
        auth_context,
        service,
        lambda: service.create_message(
            payload,
            anthropic_version=anthropic_version,
            anthropic_beta=anthropic_beta,
        ),
    )
    return _with_upstream_headers(result, service)


@router.post("/messages/count_tokens")
def count_message_tokens(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db_session),
    auth_context: ModelGatewayAuthContext = Depends(get_anthropic_gateway_auth_context),
    anthropic_version: Optional[str] = Header(None, alias="anthropic-version"),
    anthropic_beta: Optional[str] = Header(None, alias="anthropic-beta"),
) -> Any:
    """Count input tokens for a Messages request.

    Forwarded upstream for Anthropic models; no usage row is written and no
    budget is charged. Authentication and model authorization still apply.
    """
    service = OpenAIGatewayService(db, auth_context, owns_db_session=True)
    service.extra_anthropic_headers = _extra_anthropic_headers(request)
    result = _run(
        auth_context,
        service,
        lambda: service.count_message_tokens(
            payload,
            anthropic_version=anthropic_version,
            anthropic_beta=anthropic_beta,
        ),
    )
    return _with_upstream_headers(result, service)


@router.get("/models")
def list_models(
    db: Session = Depends(get_db_session),
    auth_context: ModelGatewayAuthContext = Depends(get_anthropic_gateway_auth_context),
) -> Dict[str, Any]:
    """List gateway models in Anthropic's shape, filtered by allowed models."""
    return OpenAIGatewayService(
        db, auth_context, owns_db_session=True
    ).list_anthropic_models()
