"""OpenAI-compatible gateway endpoints."""

from __future__ import annotations

from typing import Any, Dict, Iterator, Optional

from fastapi import APIRouter, Body, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from preloop.models.db.session import get_db_session
from preloop.services.agent_session_headers import (
    native_parent_session_id_from_headers,
    native_session_id_from_headers,
)
from preloop.api.gateway_auth_dependency import get_model_gateway_auth_context
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.api.deps import get_budget_enforcer
from preloop.services.gateway_streaming import GatewayStreamingResponse
from preloop.services.openai_gateway import OpenAIGatewayService

router = APIRouter(include_in_schema=False)


_WARNING_HEADER_MAX_LEN = 256


def _sanitize_header_value(value: str, max_len: int = _WARNING_HEADER_MAX_LEN) -> str:
    """Make an arbitrary string safe to emit as an HTTP header value.

    The warning text embeds client-controlled input (the requested model
    spelling) and model names that may contain anything a user or an
    agent-onboarding import wrote. Three hazards are neutralized:

    * CR/LF would split the header (response-splitting/injection class);
    * non-latin-1 characters (CJK/emoji in a model name) raise
      ``UnicodeEncodeError`` inside Starlette's ``Response.init_headers``,
      turning a successful completion into a 500 on exactly the collision
      path this header exists to make visible;
    * unbounded model inventories could inflate the header past proxy
      limits, so the value is capped.
    """
    cleaned = value.replace("\r", " ").replace("\n", " ")
    cleaned = cleaned.encode("ascii", "replace").decode("ascii")
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3] + "..."
    return cleaned


def _with_gateway_warnings(
    result: Dict[str, Any], service: OpenAIGatewayService
) -> Any:
    """Attach the request's warning header to a non-streaming result.

    The service records non-fatal warnings while serving a request: the
    requested alias matched more than one binding, or a configured budget
    could not be enforced because the model has no known price. Surfacing
    them as ``X-Preloop-Warning`` keeps the body OpenAI-compatible while
    making the condition visible to the caller.
    """
    warning = service.response_warning
    if warning:
        return JSONResponse(
            content=result,
            headers={"X-Preloop-Warning": _sanitize_header_value(warning)},
        )
    return result


def _streaming_with_gateway_warnings(
    events: Iterator[str], service: OpenAIGatewayService
) -> GatewayStreamingResponse:
    """Attach the request's warning header to a streaming result.

    The service's ``stream_*`` methods are plain functions, not generators:
    they resolve the model, run budget preflight and open the upstream stream
    before handing back the body generator. So by the time ``events`` exists
    every pre-dispatch warning is already recorded on ``service`` and the
    headers have not been sent yet. Reading ``response_warning`` here, after
    the argument was evaluated, is what puts the warning on the wire for
    ``stream: true`` callers (issue #810).
    """
    warning = service.response_warning
    return GatewayStreamingResponse(
        events,
        media_type="text/event-stream",
        headers=(
            {"X-Preloop-Warning": _sanitize_header_value(warning)} if warning else None
        ),
        on_complete=service.flush_deferred_stream_record,
    )


@router.get("/models")
def list_models(
    db: Session = Depends(get_db_session),
    auth_context: ModelGatewayAuthContext = Depends(get_model_gateway_auth_context),
) -> Dict[str, Any]:
    """List models available via the Preloop gateway."""
    return OpenAIGatewayService(db, auth_context, owns_db_session=True).list_models()


@router.post("/chat/completions")
def create_chat_completion(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db_session),
    auth_context: ModelGatewayAuthContext = Depends(get_model_gateway_auth_context),
    budget_enforcer: Any = Depends(get_budget_enforcer),
    x_preloop_session_id: Optional[str] = Header(None, alias="X-Preloop-Session-Id"),
) -> Any:
    """Create an OpenAI-compatible chat completion.

    OpenCode (``x-session-id``) and Codex (``session-id``/``thread-id``) both
    stamp their own conversation id on every request; reading it is what stops
    every conversation on a machine collapsing onto one eternal runtime session.
    Preloop's own header still wins, and agent-native headers are only trusted
    for the agent the credential identifies — see
    :mod:`preloop.services.agent_session_headers`.

    OpenCode additionally names the session that spawned a subagent's session
    (``x-parent-session-id``); it is read on the same terms and only when the
    session id came from the harness, so an operator-supplied
    ``X-Preloop-Session-Id`` is never given a parent it did not ask for.
    """
    service = OpenAIGatewayService(
        db,
        auth_context,
        budget_enforcer=budget_enforcer,
        owns_db_session=True,
        client_session_id=x_preloop_session_id
        or native_session_id_from_headers(request.headers, auth_context=auth_context),
        # Only the explicit Preloop header opts a plain API key into a
        # runtime session; a vendor-native header must not.
        client_session_id_is_explicit=bool(x_preloop_session_id),
        client_parent_session_id=(
            None
            if x_preloop_session_id
            else native_parent_session_id_from_headers(
                request.headers, auth_context=auth_context
            )
        ),
    )
    if payload.get("stream"):
        return _streaming_with_gateway_warnings(
            service.stream_chat_completion(payload), service
        )
    return _with_gateway_warnings(service.create_chat_completion(payload), service)


@router.post("/responses")
def create_response(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db_session),
    auth_context: ModelGatewayAuthContext = Depends(get_model_gateway_auth_context),
    budget_enforcer: Any = Depends(get_budget_enforcer),
    x_preloop_session_id: Optional[str] = Header(None, alias="X-Preloop-Session-Id"),
) -> Any:
    """Create an OpenAI-compatible response.

    This is the route Codex uses (``wire_api = "responses"``). Codex sends its
    conversation uuid as ``session-id``/``thread-id`` headers and again as the
    body's ``prompt_cache_key``; both are read, so the fix survives either
    signal being dropped. See :func:`create_chat_completion` for precedence.
    """
    service = OpenAIGatewayService(
        db,
        auth_context,
        budget_enforcer=budget_enforcer,
        owns_db_session=True,
        client_identity_headers=request.headers,
        client_session_id=x_preloop_session_id
        or native_session_id_from_headers(request.headers, auth_context=auth_context),
        # Only the explicit Preloop header opts a plain API key into a
        # runtime session; a vendor-native header must not.
        client_session_id_is_explicit=bool(x_preloop_session_id),
        client_parent_session_id=(
            None
            if x_preloop_session_id
            else native_parent_session_id_from_headers(
                request.headers, auth_context=auth_context
            )
        ),
    )
    if payload.get("stream"):
        return _streaming_with_gateway_warnings(
            service.stream_response(payload), service
        )
    return _with_gateway_warnings(service.create_response(payload), service)


@router.post("/embeddings")
def create_embedding(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db_session),
    auth_context: ModelGatewayAuthContext = Depends(get_model_gateway_auth_context),
    budget_enforcer: Any = Depends(get_budget_enforcer),
    x_preloop_session_id: Optional[str] = Header(None, alias="X-Preloop-Session-Id"),
) -> Any:
    """Create OpenAI-compatible embeddings.

    Same authentication, account scoping, budget preflight and usage
    accounting as the completions routes; embeddings carry no stream, so
    there is no SSE branch here. Parent lineage is read on the same terms
    as chat and responses: a subagent whose first request is an embedding
    must still record who spawned it.
    """
    service = OpenAIGatewayService(
        db,
        auth_context,
        budget_enforcer=budget_enforcer,
        owns_db_session=True,
        client_session_id=x_preloop_session_id
        or native_session_id_from_headers(request.headers, auth_context=auth_context),
        # Only the explicit Preloop header opts a plain API key into a
        # runtime session; a vendor-native header must not.
        client_session_id_is_explicit=bool(x_preloop_session_id),
        client_parent_session_id=(
            None
            if x_preloop_session_id
            else native_parent_session_id_from_headers(
                request.headers, auth_context=auth_context
            )
        ),
    )
    return _with_gateway_warnings(service.create_embedding(payload), service)
