"""OpenAI-compatible model gateway service."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional, Protocol
from urllib import error as urllib_error
from urllib import request as urllib_request

import httpx
import litellm
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import (
    crud_ai_model,
    crud_api_usage,
    crud_managed_agent,
    crud_managed_agent_ai_model_binding,
    crud_runtime_session,
    crud_runtime_session_activity,
)
from preloop.models.models.ai_model import AIModel
from preloop.services.account_realtime import (
    ACCOUNT_TOPIC_MANAGED_AGENTS,
    ACCOUNT_TOPIC_RUNTIME_SESSIONS,
    build_account_event,
    emit_account_event,
)
from preloop.services.account_governance_cache import get_cached_account_meta_data
from preloop.services.context_optimization import (
    ContextOptimizationStats,
    estimate_tokens,
    optimize_messages,
    resolve_context_optimization_settings,
    sanitize_tool_choice,
    strip_disabled_tools,
    subject_governance_affects_gateway_context,
    tool_choice_named_tool,
    tool_definition_name,
)
from preloop.services.model_gateway_auth import (
    ModelGatewayAuthContext,
    compute_authorized_model_ids,
    resolve_managed_agent_id_for_context,
)
from preloop.services.model_gateway_budget import (
    BudgetCheckResult,
    ModelGatewayBudgetService,
)
from preloop.services.subject_governance import build_subject_context_from_api_key
from preloop.services.model_gateway_events import ModelGatewayEventEmitter
from preloop.services.model_gateway_errors import (
    GatewayProvider,
    ModelGatewayAPIError,
)
from preloop.services.model_price_catalog import schedule_price_lookup
from preloop.services.model_pricing import (
    _iter_litellm_model_candidates,
    estimate_ai_model_usage_cost_detailed,
)
from preloop.services.litellm_routing import to_litellm_model
from preloop.services.pricing_overrides import resolve_pricing_override
from preloop.services.model_runtime_resolver import resolve_ai_model_runtime
from preloop.services.gateway_usage_search import GatewayUsageSearchService
from preloop.services.secret_service import (
    ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
    CredentialRefreshError,
    OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
    ResolvedModelCredentials,
    get_secret_service,
)
from preloop.utils.audit import log_model_gateway_request

logger = logging.getLogger(__name__)

_RUNTIME_SESSION_ACTIVITY_TOUCH_MIN_INTERVAL = timedelta(seconds=30)
_RUNTIME_SESSION_SUMMARY_REFRESH_EVERY_REQUESTS = 10

# Anthropic subscription-OAuth passthrough. Anthropic validates
# subscription-OAuth (Claude Code Pro/Max) requests structurally: the first
# ``system`` block must be exactly the Claude Code sentinel string, and
# violations are rejected with a disguised 429 ``rate_limit_error``. The
# litellm transcode path joins system blocks into a single string and drops
# ``cache_control``, which destroys that structure — so OAuth-backed
# Anthropic-protocol traffic is forwarded verbatim instead (see
# ``_anthropic_oauth_passthrough_token``).
ANTHROPIC_OAUTH_PASSTHROUGH_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_OAUTH_BETA_FLAG = "oauth-2025-04-20"
ANTHROPIC_DEFAULT_API_VERSION = "2023-06-01"
_ANTHROPIC_PASSTHROUGH_TIMEOUT_SECONDS = 600


def _supports_ambient_provider_credentials(ai_model: AIModel) -> bool:
    provider = (ai_model.provider_name or "").strip().lower()
    return provider in {"bedrock", "amazon-bedrock"}


def _bedrock_region(ai_model: AIModel) -> Optional[str]:
    raw_meta_data = getattr(ai_model, "meta_data", None)
    meta_data = raw_meta_data if isinstance(raw_meta_data, dict) else {}
    provider_runtime = (
        meta_data.get("provider_runtime")
        if isinstance(meta_data.get("provider_runtime"), dict)
        else {}
    )
    region = provider_runtime.get("region")
    return str(region).strip() if region else None


def _bedrock_credential_kwargs(secret_value: Optional[str]) -> Dict[str, Any]:
    raw_secret = (secret_value or "").strip()
    if not raw_secret:
        return {}

    try:
        payload = json.loads(raw_secret)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}

    if not isinstance(payload, dict):
        return {}

    kwargs: Dict[str, Any] = {}
    for source_key, target_key in (
        ("aws_access_key_id", "aws_access_key_id"),
        ("aws_secret_access_key", "aws_secret_access_key"),
        ("aws_session_token", "aws_session_token"),
        ("aws_region_name", "aws_region_name"),
    ):
        value = payload.get(source_key)
        if value:
            kwargs[target_key] = str(value).strip()
    return kwargs


class ModelGatewayBackend(Protocol):
    def completion(self, **kwargs: Any) -> Any:
        pass


_ANTHROPIC_OAUTH_ENV_LOCK = threading.Lock()


@contextmanager
def _anthropic_oauth_environment(auth_token: str) -> Iterator[None]:
    """Force LiteLLM's Anthropic client to use OAuth, not ambient API keys."""
    with _ANTHROPIC_OAUTH_ENV_LOCK:
        previous_api_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        previous_auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        os.environ["ANTHROPIC_AUTH_TOKEN"] = auth_token
        try:
            yield
        finally:
            if previous_api_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = previous_api_key
            if previous_auth_token is None:
                os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
            else:
                os.environ["ANTHROPIC_AUTH_TOKEN"] = previous_auth_token


class LiteLLMModelGatewayBackend:
    def completion(self, **kwargs: Any) -> Any:
        anthropic_auth_token = kwargs.pop("_preloop_anthropic_auth_token", None)
        if anthropic_auth_token:
            with _anthropic_oauth_environment(str(anthropic_auth_token)):
                return litellm.completion(**kwargs)
        return litellm.completion(**kwargs)


def get_model_gateway_backend(
    backend_name: Optional[str] = None,
) -> ModelGatewayBackend:
    normalized_backend_name = (
        (backend_name or settings.model_gateway_upstream_backend or "litellm")
        .strip()
        .lower()
    )
    if normalized_backend_name == "litellm":
        return LiteLLMModelGatewayBackend()
    raise ValueError(
        f"Unsupported model gateway upstream backend: {normalized_backend_name}"
    )


# Per-run session id (X-Preloop-Session-Id) validation. The header maps to a
# LangGraph ``thread_id`` / wizard-emitted run id. We accept a conservative,
# URL/identifier-safe charset and cap the length; anything else is ignored so a
# malformed header never errors and simply falls back to source keying.
_CLIENT_SESSION_ID_MAX_LEN = 200
_CLIENT_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]+$")


def _normalize_client_session_id(raw: Optional[str]) -> Optional[str]:
    """Validate and normalize the client-supplied per-run session id.

    Args:
        raw: Raw ``X-Preloop-Session-Id`` header value (may be ``None``).

    Returns:
        The trimmed id when it is non-empty, within the length cap, and uses
        only the safe charset; otherwise ``None`` (caller falls back to the
        existing source-keyed behavior).
    """
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > _CLIENT_SESSION_ID_MAX_LEN:
        return None
    if not _CLIENT_SESSION_ID_RE.match(candidate):
        return None
    return candidate


class OpenAIGatewayService:
    """Service for Preloop's OpenAI-compatible gateway."""

    def __init__(
        self,
        db: Session,
        auth_context: ModelGatewayAuthContext,
        upstream_backend: Optional[ModelGatewayBackend] = None,
        budget_enforcer: Optional[Any] = None,
        client_session_id: Optional[str] = None,
        skip_runtime_session_resolution: bool = False,
    ) -> None:
        self.db = db
        self.auth_context = auth_context
        self.upstream_backend = upstream_backend or get_model_gateway_backend()
        self.budget_enforcer = budget_enforcer
        # Per-run session id supplied by the client (X-Preloop-Session-Id).
        # Validated/normalized once; invalid values fall back to source keying.
        self._client_session_id = _normalize_client_session_id(client_session_id)
        self._resolved_runtime_session_id: Optional[str] = None
        self._resolved_runtime_session_attempted = skip_runtime_session_resolution
        self._last_context_optimization: Optional[ContextOptimizationStats] = None
        self._last_tools_meta: Optional[List[Dict[str, Any]]] = None
        # Upstream credential type ("oauth" | "api_key" | "ambient") of the
        # credential used to call the provider on THIS request, captured at
        # resolution time and read into the usage row at log time. Powers
        # subscription-vs-API-key savings denomination. None when never
        # resolved (error paths, unknown provider) -> non-dollar fallback,
        # never a false dollar claim. Set at both credential-resolution
        # choke points; reset there per request to avoid stale carryover.
        self._last_upstream_credential_type: Optional[str] = None
        # Per-request memo of the authorized model-id set for this principal.
        # Computed once from the account inventory on first use so listing,
        # alias resolution, and default selection all consume the same set.
        self._authorized_model_ids_cache: Optional[frozenset[str]] = None

    def _resolve_runtime_session(self) -> Optional[str]:
        if self._resolved_runtime_session_attempted:
            return self._resolved_runtime_session_id

        self._resolved_runtime_session_attempted = True

        runtime_context = (
            (self.auth_context.api_key.context_data or {})
            if self.auth_context.api_key
            else {}
        )
        runtime_principal = runtime_context.get("runtime_principal") or {}
        runtime_session_id = runtime_context.get("runtime_session_id")

        if not runtime_session_id and runtime_principal:
            session_source_type = runtime_principal.get("type")
            session_source_id = runtime_principal.get("id")
            # A static custom-agent credential reuses one source id for every
            # request, collapsing per-run ROI into one eternal session. When the
            # client supplies a per-run id via X-Preloop-Session-Id, fold it into
            # the source id so each distinct run gets its own session row. Absent
            # or malformed headers leave the source id untouched (no regression).
            if session_source_type and session_source_id and self._client_session_id:
                session_source_id = f"{session_source_id}:{self._client_session_id}"
            elif session_source_type and session_source_id:
                # Plugin agents (Hermes, OpenClaw, Claude Code, ...) send gateway
                # traffic on a durable credential that carries no per-run id, so
                # plain source keying piles every run onto one base session and
                # the per-run sessions the runtime lifecycle creates (session
                # token mint) stay empty. Attribute usage to the principal's
                # current run session instead, so per-run ROI is real. We only
                # adopt a *suffixed* (per-run) session, never the base row, and
                # only while it is open; otherwise we fall through to source
                # keying below (unchanged behavior, e.g. custom agents that have
                # not minted a per-run session). Custom agents that pass
                # X-Preloop-Session-Id took the branch above and skip this.
                try:
                    latest_run = crud_runtime_session.get_latest_by_principal(
                        self.db,
                        account_id=str(self.auth_context.user.account_id),
                        principal_type=session_source_type,
                        principal_id=session_source_id,
                    )
                    if (
                        latest_run is not None
                        and latest_run.ended_at is None
                        and latest_run.session_source_id != session_source_id
                    ):
                        runtime_session_id = str(latest_run.id)
                except Exception:
                    logger.debug(
                        "Failed to resolve latest run session for principal",
                        exc_info=True,
                    )
            if not runtime_session_id and session_source_type and session_source_id:
                try:
                    from datetime import datetime, timezone

                    rs = crud_runtime_session.get_by_source(
                        self.db,
                        account_id=str(self.auth_context.user.account_id),
                        session_source_type=session_source_type,
                        session_source_id=session_source_id,
                    )
                    if rs is None or rs.ended_at is not None:
                        observed_at = datetime.now(timezone.utc)
                        rs = crud_runtime_session.upsert_by_source(
                            self.db,
                            account_id=str(self.auth_context.user.account_id),
                            session_source_type=session_source_type,
                            session_source_id=session_source_id,
                            runtime_principal_type=session_source_type,
                            runtime_principal_id=session_source_id,
                            runtime_principal_name=runtime_principal.get("name"),
                            started_at=observed_at,
                            last_activity_at=observed_at,
                            reopen_if_ended=True,
                        )
                    runtime_session_id = str(rs.id)
                except SQLAlchemyError as e:
                    self.db.rollback()
                    logger.warning(
                        "Failed to resolve runtime session for gateway request",
                        exc_info=True,
                    )
                except Exception as e:
                    logger.debug(
                        f"Failed to auto-upsert runtime session for gateway request: {e}",
                        exc_info=True,
                    )

        self._resolved_runtime_session_id = runtime_session_id
        return runtime_session_id

    def _resolve_managed_agent_id(self) -> Optional[str]:
        return resolve_managed_agent_id_for_context(self.db, self.auth_context)

    def _emit_gateway_request_started(
        self,
        ai_model: AIModel,
        requested_model: Optional[str],
        request_payload: Dict[str, Any],
        endpoint_kind: str,
    ) -> None:
        from datetime import datetime, timezone
        from preloop.services.model_gateway_events import build_account_event
        from preloop.services.account_realtime import (
            emit_account_event,
            ACCOUNT_TOPIC_GATEWAY_ACTIVITY,
        )

        runtime_session_id = self._resolve_runtime_session()
        managed_agent_id = self._resolve_managed_agent_id()

        emit_account_event(
            build_account_event(
                account_id=str(self.auth_context.user.account_id),
                topic=ACCOUNT_TOPIC_GATEWAY_ACTIVITY,
                event_type="model_gateway_request_started",
                payload={
                    "status_code": 202,  # accepted, waiting
                    "outcome": "pending",
                    "duration": 0,
                    "estimated_cost": 0,
                    "model_alias": requested_model,
                    "managed_agent_id": managed_agent_id,
                    "total_tokens": 0,
                    "meta_data": {
                        "endpoint_kind": endpoint_kind,
                        "requested_model": requested_model,
                    },
                    "request": request_payload,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                runtime_session_id=runtime_session_id,
                execution_id=None,
                flow_id=None,
            )
        )

    def list_models(self) -> Dict[str, Any]:
        """List gateway-enabled models available to this gateway principal."""
        data = []
        account_models = self._get_account_models()
        authorized_ids = self._authorized_model_ids(account_models)
        for ai_model in account_models:
            if str(ai_model.id) not in authorized_ids:
                continue
            runtime = resolve_ai_model_runtime(ai_model)
            if not runtime.model_gateway_enabled:
                continue
            data.append(
                {
                    "id": runtime.model_gateway_model_alias,
                    "object": "model",
                    "created": int(ai_model.created_at.timestamp())
                    if ai_model.created_at
                    else 0,
                    "owned_by": "preloop",
                }
            )

        # `data` is the OpenAI-standard field. Codex CLI's model-manager
        # deserializes this endpoint into a struct with a top-level `models`
        # array and errors ("missing field `models`") without it, so we mirror
        # the list under `models` too. Additive — standard clients read `data`.
        return {"object": "list", "data": data, "models": data}

    def create_chat_completion(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle OpenAI-compatible chat completions."""
        if payload.get("stream"):
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=400,
                message="Use stream_chat_completion for stream=true",
            )

        model = self._resolve_requested_model(payload.get("model"), provider="openai")
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=400,
                message="messages must be a non-empty list",
            )
        started_at = time.perf_counter()
        budget_result = self._check_budget(model, payload, gateway_provider="openai")
        if budget_result and budget_result.hard_limit_exceeded:
            detail = self._budget_denial_detail(budget_result)
            self._record_gateway_request(
                endpoint="/openai/v1/chat/completions",
                method="POST",
                status_code=403,
                duration=time.perf_counter() - started_at,
                ai_model=model,
                requested_model=payload.get("model"),
                response_payload=None,
                upstream_response=None,
                endpoint_kind="chat_completions",
                budget_result=budget_result,
                error_detail=detail,
                request_payload=payload,
            )
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=403,
                message=detail,
            )

        try:
            self._emit_gateway_request_started(
                ai_model=model,
                requested_model=payload.get("model"),
                request_payload=payload,
                endpoint_kind="chat_completions",
            )
            if self._is_openai_codex_model(model):
                # Codex bypasses _call_litellm, so capture tools_meta here too
                # (T11 finding). Codex tools are not governance-stripped.
                self._capture_tools_meta(payload.get("tools"))
                raw_codex_response = self._create_openai_codex_response(
                    model,
                    self._build_openai_codex_payload_from_chat_completion(
                        payload=payload,
                        messages=messages,
                        ai_model=model,
                    ),
                )
                response_dict = self._codex_response_to_chat_completion_dict(
                    raw_codex_response
                )
            else:
                response = self._call_litellm(
                    model,
                    messages=messages,
                    payload=payload,
                    provider="openai",
                )
                response_dict = self._response_to_dict(response)
            assistant_content = self._extract_assistant_text(response_dict)
            usage = self._normalize_usage(
                response_dict.get("usage"),
                prompt_key="prompt_tokens",
                completion_key="completion_tokens",
            )
            assistant_message = {
                "role": "assistant",
                "content": assistant_content,
            }
            tool_calls = self._extract_tool_calls(response_dict)
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            response_payload = {
                "id": response_dict.get("id", f"chatcmpl_{int(time.time())}"),
                "object": "chat.completion",
                "created": response_dict.get("created", int(time.time())),
                "model": payload.get("model")
                or resolve_ai_model_runtime(model).model_gateway_model_alias,
                "choices": [
                    {
                        "index": 0,
                        "message": assistant_message,
                        "finish_reason": self._extract_finish_reason(response_dict),
                    }
                ],
                "usage": usage,
            }
            self._record_gateway_request(
                endpoint="/openai/v1/chat/completions",
                method="POST",
                status_code=200,
                duration=time.perf_counter() - started_at,
                ai_model=model,
                requested_model=payload.get("model"),
                response_payload=response_payload,
                upstream_response=response_dict,
                endpoint_kind="chat_completions",
                budget_result=budget_result,
                request_payload=payload,
            )
            return response_payload
        except ModelGatewayAPIError as exc:
            self._record_gateway_request(
                endpoint="/openai/v1/chat/completions",
                method="POST",
                status_code=exc.status_code,
                duration=time.perf_counter() - started_at,
                ai_model=model,
                requested_model=payload.get("model"),
                response_payload=None,
                upstream_response=None,
                endpoint_kind="chat_completions",
                error_detail=exc.message,
                budget_result=budget_result,
                request_payload=payload,
            )
            raise

    def create_response(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle OpenAI Responses API-compatible requests."""
        if payload.get("stream"):
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=400,
                message="Use stream_response for stream=true",
            )

        model = self._resolve_requested_model(payload.get("model"), provider="openai")
        messages = self._normalize_responses_input(payload)
        started_at = time.perf_counter()
        budget_result = self._check_budget(model, payload, gateway_provider="openai")
        if budget_result and budget_result.hard_limit_exceeded:
            detail = self._budget_denial_detail(budget_result)
            self._record_gateway_request(
                endpoint="/openai/v1/responses",
                method="POST",
                status_code=403,
                duration=time.perf_counter() - started_at,
                ai_model=model,
                requested_model=payload.get("model"),
                response_payload=None,
                upstream_response=None,
                endpoint_kind="responses",
                budget_result=budget_result,
                error_detail=detail,
                request_payload=payload,
            )
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=403,
                message=detail,
            )
        try:
            self._emit_gateway_request_started(
                ai_model=model,
                requested_model=payload.get("model"),
                request_payload=payload,
                endpoint_kind="responses",
            )
            if self._is_openai_codex_model(model):
                # Codex bypasses _call_litellm (T11 finding); attribute here.
                self._capture_tools_meta(payload.get("tools"))
                response_dict = self._create_openai_codex_response(model, payload)
            else:
                response = self._call_litellm(
                    model,
                    messages=messages,
                    payload=payload,
                    provider="openai",
                )
                response_dict = self._response_to_dict(response)
            response_payload = self._build_responses_api_payload(
                ai_model=model,
                requested_model=(
                    payload.get("model")
                    or resolve_ai_model_runtime(model).model_gateway_model_alias
                ),
                response_dict=response_dict,
            )
            self._record_gateway_request(
                endpoint="/openai/v1/responses",
                method="POST",
                status_code=200,
                duration=time.perf_counter() - started_at,
                ai_model=model,
                requested_model=payload.get("model"),
                response_payload=response_payload,
                upstream_response=response_dict,
                endpoint_kind="responses",
                budget_result=budget_result,
                request_payload=payload,
            )
            return response_payload
        except ModelGatewayAPIError as exc:
            self._record_gateway_request(
                endpoint="/openai/v1/responses",
                method="POST",
                status_code=exc.status_code,
                duration=time.perf_counter() - started_at,
                ai_model=model,
                requested_model=payload.get("model"),
                response_payload=None,
                upstream_response=None,
                endpoint_kind="responses",
                error_detail=exc.message,
                budget_result=budget_result,
                request_payload=payload,
            )
            raise

    def create_message(
        self,
        payload: Dict[str, Any],
        *,
        anthropic_version: Optional[str] = None,
        anthropic_beta: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Handle Anthropic Messages API-compatible requests."""
        if payload.get("stream"):
            raise ModelGatewayAPIError(
                provider="anthropic",
                status_code=400,
                message="Use stream_message for stream=true",
            )

        model = self._resolve_requested_model(
            payload.get("model"), provider="anthropic"
        )
        messages = self._normalize_anthropic_messages_input(payload)
        started_at = time.perf_counter()
        budget_result = self._check_budget(
            model, {**payload, "messages": messages}, gateway_provider="anthropic"
        )
        if budget_result and budget_result.hard_limit_exceeded:
            detail = self._budget_denial_detail(budget_result)
            self._record_gateway_request(
                endpoint="/anthropic/v1/messages",
                method="POST",
                status_code=403,
                duration=time.perf_counter() - started_at,
                ai_model=model,
                requested_model=payload.get("model"),
                response_payload=None,
                upstream_response=None,
                endpoint_kind="anthropic_messages",
                budget_result=budget_result,
                error_detail=detail,
                request_payload=payload,
            )
            raise ModelGatewayAPIError(
                provider="anthropic",
                status_code=403,
                message=detail,
            )

        try:
            self._emit_gateway_request_started(
                ai_model=model,
                requested_model=payload.get("model"),
                request_payload=payload,
                endpoint_kind="anthropic_messages",
            )
            oauth_token = self._anthropic_oauth_passthrough_token(model)
            if oauth_token is not None:
                # Subscription-OAuth: forward the client's Anthropic-native
                # payload verbatim (system blocks, cache_control, betas) —
                # the litellm transcode destroys the structure Anthropic
                # validates on OAuth traffic. See the passthrough section.
                url, headers, body = self._prepare_anthropic_passthrough(
                    ai_model=model,
                    payload=payload,
                    oauth_token=oauth_token,
                    anthropic_version=anthropic_version,
                    anthropic_beta=anthropic_beta,
                    stream=False,
                )
                response_payload = self._anthropic_oauth_passthrough_complete(
                    url=url, headers=headers, body=body
                )
                upstream_usage = (
                    response_payload.get("usage")
                    if isinstance(response_payload.get("usage"), dict)
                    else {}
                )
                response_dict = {
                    "id": response_payload.get("id"),
                    "choices": [{"finish_reason": response_payload.get("stop_reason")}],
                    "usage": upstream_usage,
                }
            else:
                response = self._call_litellm(
                    model,
                    messages=messages,
                    payload=payload,
                    provider="anthropic",
                )
                response_dict = self._response_to_dict(response)
                assistant_text = self._extract_assistant_text(response_dict)
                usage = self._normalize_usage(
                    response_dict.get("usage"),
                    prompt_key="prompt_tokens",
                    completion_key="completion_tokens",
                    output_names=("completion_tokens", "output_tokens"),
                )
                response_payload = self._build_anthropic_message_payload(
                    response_id=response_dict.get("id", f"msg_{int(time.time())}"),
                    model_name=payload.get("model")
                    or resolve_ai_model_runtime(model).model_gateway_model_alias,
                    assistant_text=assistant_text,
                    stop_reason=self._to_anthropic_stop_reason(
                        self._extract_finish_reason(response_dict)
                    ),
                    usage=usage,
                )
            self._record_gateway_request(
                endpoint="/anthropic/v1/messages",
                method="POST",
                status_code=200,
                duration=time.perf_counter() - started_at,
                ai_model=model,
                requested_model=payload.get("model"),
                response_payload=response_payload,
                upstream_response=response_dict,
                endpoint_kind="anthropic_messages",
                budget_result=budget_result,
                request_payload=payload,
            )
            return response_payload
        except ModelGatewayAPIError as exc:
            self._record_gateway_request(
                endpoint="/anthropic/v1/messages",
                method="POST",
                status_code=exc.status_code,
                duration=time.perf_counter() - started_at,
                ai_model=model,
                requested_model=payload.get("model"),
                response_payload=None,
                upstream_response=None,
                endpoint_kind="anthropic_messages",
                error_detail=exc.message,
                budget_result=budget_result,
                request_payload=payload,
            )
            raise

    def stream_message(
        self,
        payload: Dict[str, Any],
        *,
        anthropic_version: Optional[str] = None,
        anthropic_beta: Optional[str] = None,
    ) -> Iterator[str]:
        """Handle streaming Anthropic Messages API-compatible requests."""
        model = self._resolve_requested_model(
            payload.get("model"), provider="anthropic"
        )
        messages = self._normalize_anthropic_messages_input(payload)
        budget_payload = {**payload, "messages": messages}
        started_at = time.perf_counter()
        budget_result = self._check_budget(
            model, budget_payload, gateway_provider="anthropic"
        )
        if budget_result and budget_result.hard_limit_exceeded:
            detail = self._budget_denial_detail(budget_result)
            self._record_gateway_request(
                endpoint="/anthropic/v1/messages",
                method="POST",
                status_code=403,
                duration=time.perf_counter() - started_at,
                ai_model=model,
                requested_model=payload.get("model"),
                response_payload=None,
                upstream_response=None,
                endpoint_kind="anthropic_messages_stream",
                budget_result=budget_result,
                error_detail=detail,
                request_payload=payload,
            )
            raise ModelGatewayAPIError(
                provider="anthropic",
                status_code=403,
                message=detail,
            )

        passthrough_connection: Optional[tuple[httpx.Client, httpx.Response]] = None
        try:
            self._emit_gateway_request_started(
                ai_model=model,
                requested_model=payload.get("model"),
                request_payload=payload,
                endpoint_kind="anthropic_messages_stream",
            )
            oauth_token = self._anthropic_oauth_passthrough_token(model)
            if oauth_token is not None:
                # Subscription-OAuth: relay the Anthropic-native SSE stream
                # verbatim; see the passthrough section for rationale.
                url, headers, body = self._prepare_anthropic_passthrough(
                    ai_model=model,
                    payload=payload,
                    oauth_token=oauth_token,
                    anthropic_version=anthropic_version,
                    anthropic_beta=anthropic_beta,
                    stream=True,
                )
                passthrough_connection = self._open_anthropic_oauth_passthrough_stream(
                    url=url, headers=headers, body=body
                )
            else:
                upstream_stream = self._call_litellm(
                    model,
                    messages=messages,
                    payload=payload,
                    stream=True,
                    provider="anthropic",
                )
        except ModelGatewayAPIError as exc:
            self._record_gateway_request(
                endpoint="/anthropic/v1/messages",
                method="POST",
                status_code=exc.status_code,
                duration=time.perf_counter() - started_at,
                ai_model=model,
                requested_model=payload.get("model"),
                response_payload=None,
                upstream_response=None,
                endpoint_kind="anthropic_messages_stream",
                error_detail=exc.message,
                budget_result=budget_result,
                request_payload=payload,
            )
            raise

        if passthrough_connection is not None:
            upstream_client, upstream_response = passthrough_connection
            return self._anthropic_passthrough_event_stream(
                upstream_client,
                upstream_response,
                ai_model=model,
                payload=payload,
                budget_result=budget_result,
                started_at=started_at,
            )

        requested_model = (
            payload.get("model")
            or resolve_ai_model_runtime(model).model_gateway_model_alias
        )

        def event_stream() -> Iterator[str]:
            assistant_parts: List[str] = []
            final_usage = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
            final_usage_details: Dict[str, Any] = {}
            last_finish_reason: Optional[str] = None
            response_id: Optional[str] = None
            emitted_text_start = False
            emitted_text_stop = False
            recorded = False
            tool_call_states: Dict[int, Dict[str, Any]] = {}
            content_index = 0

            try:
                for chunk in upstream_stream:
                    chunk_dict = self._response_to_dict(chunk)
                    response_id = response_id or chunk_dict.get(
                        "id", f"msg_{int(time.time())}"
                    )
                    if chunk_dict.get("usage") is not None:
                        final_usage_details = self._merge_usage_dicts(
                            final_usage_details, chunk_dict.get("usage")
                        )
                        final_usage = self._normalize_usage(
                            final_usage_details,
                            prompt_key="prompt_tokens",
                            completion_key="completion_tokens",
                            output_names=("completion_tokens", "output_tokens"),
                        )
                    delta_text = self._extract_stream_delta_text(chunk_dict)
                    if delta_text:
                        if not emitted_text_start:
                            yield self._anthropic_sse_event(
                                "message_start",
                                {
                                    "type": "message_start",
                                    "message": self._build_anthropic_message_payload(
                                        response_id=response_id,
                                        model_name=requested_model,
                                        assistant_text="",
                                        stop_reason=None,
                                        usage=final_usage,
                                    ),
                                },
                            )
                            yield self._anthropic_sse_event(
                                "content_block_start",
                                {
                                    "type": "content_block_start",
                                    "index": content_index,
                                    "content_block": {"type": "text", "text": ""},
                                },
                            )
                            emitted_text_start = True

                        assistant_parts.append(delta_text)
                        yield self._anthropic_sse_event(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": content_index,
                                "delta": {"type": "text_delta", "text": delta_text},
                            },
                        )

                    for tool_delta in self._extract_stream_tool_call_deltas(chunk_dict):
                        if emitted_text_start and not emitted_text_stop:
                            yield self._anthropic_sse_event(
                                "content_block_stop",
                                {"type": "content_block_stop", "index": content_index},
                            )
                            content_index += 1
                            emitted_text_stop = True

                        index = int(tool_delta.get("index", 0) or 0)
                        state = tool_call_states.get(index)
                        if state is None:
                            if not emitted_text_start and not emitted_text_stop:
                                yield self._anthropic_sse_event(
                                    "message_start",
                                    {
                                        "type": "message_start",
                                        "message": self._build_anthropic_message_payload(
                                            response_id=response_id,
                                            model_name=requested_model,
                                            assistant_text="",
                                            stop_reason=None,
                                            usage=final_usage,
                                        ),
                                    },
                                )
                                emitted_text_start = True
                                emitted_text_stop = True

                            call_id = (
                                tool_delta.get("id") or f"call_{response_id}_{index}"
                            )
                            function_payload = tool_delta.get("function") or {}
                            state = {
                                "id": call_id,
                                "function": {
                                    "name": function_payload.get("name", ""),
                                    "arguments": "",
                                },
                                "content_index": content_index,
                            }
                            tool_call_states[index] = state
                            yield self._anthropic_sse_event(
                                "content_block_start",
                                {
                                    "type": "content_block_start",
                                    "index": content_index,
                                    "content_block": {
                                        "type": "tool_use",
                                        "id": call_id,
                                        "name": state["function"]["name"],
                                        "input": {},
                                    },
                                },
                            )
                            content_index += 1

                        function_payload = tool_delta.get("function") or {}
                        if function_payload.get("name"):
                            state["function"]["name"] = function_payload["name"]
                        arguments_delta = function_payload.get("arguments")
                        if arguments_delta:
                            if isinstance(arguments_delta, dict):
                                arguments_delta = json.dumps(
                                    arguments_delta, ensure_ascii=False
                                )
                            elif isinstance(arguments_delta, str):
                                # LiteLLM sometimes calls str() on dictionary objects.
                                # Try to detect and fix this so we don't stream invalid JSON with single quotes.
                                try:
                                    import ast

                                    parsed = ast.literal_eval(arguments_delta)
                                    if isinstance(parsed, dict):
                                        arguments_delta = json.dumps(
                                            parsed, ensure_ascii=False
                                        )
                                except Exception:
                                    # Keep streaming if argument delta is not Python-literal JSON.
                                    pass

                            state["function"]["arguments"] += arguments_delta
                            yield self._anthropic_sse_event(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": state["content_index"],
                                    "delta": {
                                        "type": "input_json_delta",
                                        "partial_json": arguments_delta,
                                    },
                                },
                            )

                    last_finish_reason = (
                        self._extract_finish_reason(chunk_dict) or last_finish_reason
                    )

                response_id = response_id or f"msg_{int(time.time())}"
                if not emitted_text_start:
                    yield self._anthropic_sse_event(
                        "message_start",
                        {
                            "type": "message_start",
                            "message": self._build_anthropic_message_payload(
                                response_id=response_id,
                                model_name=requested_model,
                                assistant_text="",
                                stop_reason=None,
                                usage=final_usage,
                            ),
                        },
                    )
                    yield self._anthropic_sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": content_index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )

                if not emitted_text_stop and not tool_call_states:
                    yield self._anthropic_sse_event(
                        "content_block_stop",
                        {"type": "content_block_stop", "index": content_index},
                    )

                for state in sorted(
                    tool_call_states.values(), key=lambda item: item["content_index"]
                ):
                    yield self._anthropic_sse_event(
                        "content_block_stop",
                        {
                            "type": "content_block_stop",
                            "index": state["content_index"],
                        },
                    )

                stop_reason = self._to_anthropic_stop_reason(last_finish_reason)

                final_tool_calls_payload = []
                for _, state in sorted(tool_call_states.items()):
                    final_tool_calls_payload.append(state)

                response_payload = self._build_anthropic_message_payload(
                    response_id=response_id,
                    model_name=requested_model,
                    assistant_text="".join(assistant_parts),
                    stop_reason=stop_reason,
                    usage=final_usage,
                    tool_calls=final_tool_calls_payload,
                )
                yield self._anthropic_sse_event(
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": stop_reason,
                            "stop_sequence": None,
                        },
                        "usage": {
                            "output_tokens": final_usage["completion_tokens"],
                        },
                    },
                )
                self._record_gateway_request(
                    endpoint="/anthropic/v1/messages",
                    method="POST",
                    status_code=200,
                    duration=time.perf_counter() - started_at,
                    ai_model=model,
                    requested_model=payload.get("model"),
                    response_payload=response_payload,
                    upstream_response={
                        "id": response_id,
                        "choices": [{"finish_reason": last_finish_reason}],
                        "usage": final_usage_details,
                    },
                    endpoint_kind="anthropic_messages_stream",
                    budget_result=budget_result,
                    request_payload=payload,
                    accumulated_output_text="".join(assistant_parts),
                )
                recorded = True
                yield self._anthropic_sse_event(
                    "message_stop",
                    {"type": "message_stop"},
                )
            except Exception as exc:
                if not recorded:
                    self._record_gateway_request(
                        endpoint="/anthropic/v1/messages",
                        method="POST",
                        status_code=502,
                        duration=time.perf_counter() - started_at,
                        ai_model=model,
                        requested_model=payload.get("model"),
                        response_payload=None,
                        upstream_response=None,
                        endpoint_kind="anthropic_messages_stream",
                        budget_result=budget_result,
                        error_detail=str(exc),
                        request_payload=payload,
                    )
                    recorded = True
                raise
            finally:
                # Client disconnect raises GeneratorExit (a BaseException) at the
                # paused yield, which the except above does not catch — so
                # already-consumed upstream tokens would go unbilled and let
                # cumulative budgets drift. Record a best-effort row here.
                if not recorded:
                    self._record_stream_abort(
                        endpoint="/anthropic/v1/messages",
                        endpoint_kind="anthropic_messages_stream",
                        started_at=started_at,
                        ai_model=model,
                        payload=payload,
                        usage_details=final_usage_details or final_usage,
                        budget_result=budget_result,
                        accumulated_output_text="".join(assistant_parts),
                    )

        return event_stream()

    def stream_chat_completion(self, payload: Dict[str, Any]) -> Iterator[str]:
        """Handle streaming OpenAI-compatible chat completions."""
        model = self._resolve_requested_model(payload.get("model"), provider="openai")
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=400,
                message="messages must be a non-empty list",
            )

        started_at = time.perf_counter()
        budget_result = self._check_budget(model, payload, gateway_provider="openai")
        if budget_result and budget_result.hard_limit_exceeded:
            detail = self._budget_denial_detail(budget_result)
            self._record_gateway_request(
                endpoint="/openai/v1/chat/completions",
                method="POST",
                status_code=403,
                duration=time.perf_counter() - started_at,
                ai_model=model,
                requested_model=payload.get("model"),
                response_payload=None,
                upstream_response=None,
                endpoint_kind="chat_completions_stream",
                budget_result=budget_result,
                error_detail=detail,
                request_payload=payload,
            )
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=403,
                message=detail,
            )

        try:
            self._emit_gateway_request_started(
                ai_model=model,
                requested_model=payload.get("model"),
                request_payload=payload,
                endpoint_kind="chat_completions_stream",
            )
            if self._is_openai_codex_model(model):
                # Codex bypasses _call_litellm (T11 finding); attribute here.
                self._capture_tools_meta(payload.get("tools"))
                return self._stream_openai_codex_chat_completion(
                    ai_model=model,
                    payload=payload,
                    messages=messages,
                    started_at=started_at,
                    budget_result=budget_result,
                )
            upstream_stream = self._call_litellm(
                model,
                messages=messages,
                payload=payload,
                stream=True,
                provider="openai",
            )
        except ModelGatewayAPIError as exc:
            self._record_gateway_request(
                endpoint="/openai/v1/chat/completions",
                method="POST",
                status_code=exc.status_code,
                duration=time.perf_counter() - started_at,
                ai_model=model,
                requested_model=payload.get("model"),
                response_payload=None,
                upstream_response=None,
                endpoint_kind="chat_completions_stream",
                error_detail=exc.message,
                budget_result=budget_result,
                request_payload=payload,
            )
            raise

        requested_model = (
            payload.get("model")
            or resolve_ai_model_runtime(model).model_gateway_model_alias
        )

        client_included_usage = bool(
            (payload.get("stream_options") or {}).get("include_usage")
        )

        def event_stream() -> Iterator[str]:
            assistant_parts: List[str] = []
            final_usage = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
            final_usage_details: Dict[str, Any] = {}
            last_finish_reason: Optional[str] = None
            response_id: Optional[str] = None
            created_at: Optional[int] = None
            recorded = False
            tool_call_states: Dict[int, Dict[str, Any]] = {}
            try:
                for chunk in upstream_stream:
                    chunk_dict = self._response_to_dict(chunk)
                    response_id = response_id or chunk_dict.get(
                        "id", f"chatcmpl_{int(time.time())}"
                    )
                    created_at = created_at or chunk_dict.get(
                        "created", int(time.time())
                    )
                    event_payload = self._normalize_chat_stream_chunk(
                        chunk_dict,
                        model_name=requested_model,
                        response_id=response_id,
                        created_at=created_at,
                    )
                    delta_text = self._extract_stream_delta_text(event_payload)
                    if delta_text:
                        assistant_parts.append(delta_text)
                    for tool_delta in self._extract_stream_tool_call_deltas(
                        event_payload
                    ):
                        index = int(tool_delta.get("index", 0) or 0)
                        state = tool_call_states.get(index)
                        if state is None:
                            state = {
                                "id": tool_delta.get("id")
                                or f"call_{response_id}_{index}",
                                "type": tool_delta.get("type") or "function",
                                "function": {"name": "", "arguments": ""},
                            }
                            tool_call_states[index] = state
                        elif tool_delta.get("id"):
                            state["id"] = tool_delta["id"]
                        if tool_delta.get("type"):
                            state["type"] = tool_delta["type"]

                        function_delta = tool_delta.get("function") or {}
                        if function_delta.get("name"):
                            state["function"]["name"] = function_delta["name"]
                        if function_delta.get("arguments"):
                            state["function"]["arguments"] += function_delta[
                                "arguments"
                            ]
                    last_finish_reason = (
                        self._extract_finish_reason(event_payload) or last_finish_reason
                    )
                    if chunk_dict.get("usage") is not None:
                        final_usage_details = self._merge_usage_dicts(
                            final_usage_details, chunk_dict.get("usage")
                        )
                        final_usage = self._normalize_usage(
                            final_usage_details,
                            prompt_key="prompt_tokens",
                            completion_key="completion_tokens",
                        )
                        # The gateway always asks litellm for the final usage
                        # chunk (accounting). Clients that did not opt in via
                        # stream_options.include_usage must not receive the
                        # synthetic usage-only chunk.
                        if not client_included_usage and not (
                            delta_text
                            or self._extract_stream_tool_call_deltas(event_payload)
                            or self._extract_finish_reason(event_payload)
                        ):
                            continue
                    yield self._sse_event(event_payload)

                assistant_message = {
                    "role": "assistant",
                    "content": "".join(assistant_parts),
                }
                tool_calls = [state for _, state in sorted(tool_call_states.items())]
                if tool_calls:
                    assistant_message["tool_calls"] = tool_calls
                response_payload = {
                    "id": response_id or f"chatcmpl_{int(time.time())}",
                    "object": "chat.completion",
                    "created": created_at or int(time.time()),
                    "model": requested_model,
                    "choices": [
                        {
                            "index": 0,
                            "message": assistant_message,
                            "finish_reason": last_finish_reason,
                        }
                    ],
                    "usage": final_usage,
                }
                self._record_gateway_request(
                    endpoint="/openai/v1/chat/completions",
                    method="POST",
                    status_code=200,
                    duration=time.perf_counter() - started_at,
                    ai_model=model,
                    requested_model=payload.get("model"),
                    response_payload=response_payload,
                    upstream_response={
                        **response_payload,
                        "usage": final_usage_details or response_payload.get("usage"),
                    },
                    endpoint_kind="chat_completions_stream",
                    budget_result=budget_result,
                    request_payload=payload,
                    accumulated_output_text="".join(assistant_parts),
                )
                recorded = True
                yield self._sse_done()
            except Exception as exc:
                if not recorded:
                    self._record_gateway_request(
                        endpoint="/openai/v1/chat/completions",
                        method="POST",
                        status_code=502,
                        duration=time.perf_counter() - started_at,
                        ai_model=model,
                        requested_model=payload.get("model"),
                        response_payload=None,
                        upstream_response=None,
                        endpoint_kind="chat_completions_stream",
                        budget_result=budget_result,
                        error_detail=str(exc),
                        request_payload=payload,
                    )
                    recorded = True
                raise
            finally:
                # See stream_message: catch the client-disconnect GeneratorExit
                # so consumed tokens are still accounted.
                if not recorded:
                    self._record_stream_abort(
                        endpoint="/openai/v1/chat/completions",
                        endpoint_kind="chat_completions_stream",
                        started_at=started_at,
                        ai_model=model,
                        payload=payload,
                        usage_details=final_usage_details or final_usage,
                        budget_result=budget_result,
                        accumulated_output_text="".join(assistant_parts),
                    )

        return event_stream()

    def stream_response(self, payload: Dict[str, Any]) -> Iterator[str]:
        """Handle streaming OpenAI Responses API-compatible requests."""
        model = self._resolve_requested_model(payload.get("model"), provider="openai")
        messages = self._normalize_responses_input(payload)
        started_at = time.perf_counter()
        budget_result = self._check_budget(model, payload, gateway_provider="openai")
        if budget_result and budget_result.hard_limit_exceeded:
            detail = self._budget_denial_detail(budget_result)
            self._record_gateway_request(
                endpoint="/openai/v1/responses",
                method="POST",
                status_code=403,
                duration=time.perf_counter() - started_at,
                ai_model=model,
                requested_model=payload.get("model"),
                response_payload=None,
                upstream_response=None,
                endpoint_kind="responses_stream",
                budget_result=budget_result,
                error_detail=detail,
                request_payload=payload,
            )
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=403,
                message=detail,
            )

        try:
            self._emit_gateway_request_started(
                ai_model=model,
                requested_model=payload.get("model"),
                request_payload=payload,
                endpoint_kind="responses_stream",
            )
            if self._is_openai_codex_model(model):
                # Codex bypasses _call_litellm (T11 finding); attribute here.
                self._capture_tools_meta(payload.get("tools"))
                return self._stream_openai_codex_response(
                    ai_model=model,
                    payload=payload,
                    started_at=started_at,
                    budget_result=budget_result,
                )
            upstream_stream = self._call_litellm(
                model,
                messages=messages,
                payload=payload,
                stream=True,
                provider="openai",
            )
        except ModelGatewayAPIError as exc:
            self._record_gateway_request(
                endpoint="/openai/v1/responses",
                method="POST",
                status_code=exc.status_code,
                duration=time.perf_counter() - started_at,
                ai_model=model,
                requested_model=payload.get("model"),
                response_payload=None,
                upstream_response=None,
                endpoint_kind="responses_stream",
                error_detail=exc.message,
                budget_result=budget_result,
                request_payload=payload,
            )
            raise

        requested_model = (
            payload.get("model")
            or resolve_ai_model_runtime(model).model_gateway_model_alias
        )

        def event_stream() -> Iterator[str]:
            response_id = f"resp_{int(time.time())}"
            created_at = int(time.time())
            text_item_id = f"msg_{response_id}"
            assistant_parts: List[str] = []
            final_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            final_usage_details: Dict[str, Any] = {}
            recorded = False
            text_output_index: Optional[int] = None
            output_items: List[Dict[str, Any]] = []
            tool_call_states: Dict[int, Dict[str, Any]] = {}
            try:
                yield self._sse_event(
                    {
                        "type": "response.created",
                        "response": {
                            "id": response_id,
                            "object": "response",
                            "created_at": created_at,
                            "model": requested_model,
                            "status": "in_progress",
                        },
                    }
                )

                for chunk in upstream_stream:
                    chunk_dict = self._response_to_dict(chunk)
                    delta_text = self._extract_stream_delta_text(chunk_dict)
                    if delta_text:
                        if text_output_index is None:
                            text_output_index = len(output_items)
                            output_items.append(
                                {
                                    "id": text_item_id,
                                    "type": "message",
                                    "status": "in_progress",
                                    "role": "assistant",
                                    "content": [],
                                }
                            )
                            yield self._sse_event(
                                {
                                    "type": "response.output_item.added",
                                    "response_id": response_id,
                                    "output_index": text_output_index,
                                    "item": output_items[text_output_index],
                                }
                            )
                            yield self._sse_event(
                                {
                                    "type": "response.content_part.added",
                                    "item_id": text_item_id,
                                    "output_index": text_output_index,
                                    "content_index": 0,
                                    "part": {"type": "output_text", "text": ""},
                                }
                            )
                        assistant_parts.append(delta_text)
                        yield self._sse_event(
                            {
                                "type": "response.output_text.delta",
                                "item_id": text_item_id,
                                "output_index": text_output_index,
                                "content_index": 0,
                                "delta": delta_text,
                            }
                        )
                    for tool_delta in self._extract_stream_tool_call_deltas(chunk_dict):
                        index = int(tool_delta.get("index", 0) or 0)
                        state = tool_call_states.get(index)
                        if state is None:
                            call_id = (
                                tool_delta.get("id") or f"call_{response_id}_{index}"
                            )
                            item_id = f"fc_{response_id}_{index}"
                            state = {
                                "item": {
                                    "id": item_id,
                                    "type": "function_call",
                                    "status": "in_progress",
                                    "call_id": call_id,
                                    "name": "",
                                    "arguments": "",
                                },
                                "output_index": len(output_items),
                            }
                            tool_call_states[index] = state
                            output_items.append(state["item"])
                            yield self._sse_event(
                                {
                                    "type": "response.output_item.added",
                                    "response_id": response_id,
                                    "output_index": state["output_index"],
                                    "item": state["item"],
                                }
                            )
                        function_payload = tool_delta.get("function") or {}
                        if function_payload.get("name"):
                            state["item"]["name"] = function_payload["name"]
                        arguments_delta = function_payload.get("arguments")
                        if arguments_delta:
                            state["item"]["arguments"] += arguments_delta
                            yield self._sse_event(
                                {
                                    "type": "response.function_call_arguments.delta",
                                    "item_id": state["item"]["id"],
                                    "output_index": state["output_index"],
                                    "delta": arguments_delta,
                                }
                            )
                    if chunk_dict.get("usage") is not None:
                        final_usage_details = self._merge_usage_dicts(
                            final_usage_details, chunk_dict.get("usage")
                        )
                        usage = self._normalize_usage(
                            final_usage_details,
                            prompt_key="prompt_tokens",
                            completion_key="completion_tokens",
                            output_names=("completion_tokens", "output_tokens"),
                        )
                        final_usage = {
                            "input_tokens": usage["prompt_tokens"],
                            "output_tokens": usage["completion_tokens"],
                            "total_tokens": usage["total_tokens"],
                        }

                full_text = "".join(assistant_parts)
                if text_output_index is not None:
                    output_items[text_output_index] = {
                        "id": text_item_id,
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": full_text}],
                    }
                    yield self._sse_event(
                        {
                            "type": "response.output_text.done",
                            "item_id": text_item_id,
                            "output_index": text_output_index,
                            "content_index": 0,
                            "text": full_text,
                        }
                    )
                    yield self._sse_event(
                        {
                            "type": "response.content_part.done",
                            "item_id": text_item_id,
                            "output_index": text_output_index,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": full_text},
                        }
                    )
                    yield self._sse_event(
                        {
                            "type": "response.output_item.done",
                            "output_index": text_output_index,
                            "item": output_items[text_output_index],
                        }
                    )
                for state in sorted(
                    tool_call_states.values(), key=lambda item: item["output_index"]
                ):
                    state["item"]["status"] = "completed"
                    yield self._sse_event(
                        {
                            "type": "response.function_call_arguments.done",
                            "item_id": state["item"]["id"],
                            "output_index": state["output_index"],
                            "arguments": state["item"]["arguments"],
                        }
                    )
                    yield self._sse_event(
                        {
                            "type": "response.output_item.done",
                            "output_index": state["output_index"],
                            "item": state["item"],
                        }
                    )
                response_payload = {
                    "id": response_id,
                    "object": "response",
                    "created_at": created_at,
                    "model": requested_model,
                    "status": "completed",
                    "output": output_items,
                    "output_text": full_text,
                    "usage": final_usage,
                }
                yield self._sse_event(
                    {
                        "type": "response.completed",
                        "response": response_payload,
                    }
                )
                self._record_gateway_request(
                    endpoint="/openai/v1/responses",
                    method="POST",
                    status_code=200,
                    duration=time.perf_counter() - started_at,
                    ai_model=model,
                    requested_model=payload.get("model"),
                    response_payload=response_payload,
                    upstream_response={
                        **response_payload,
                        "usage": final_usage_details or response_payload.get("usage"),
                    },
                    endpoint_kind="responses_stream",
                    budget_result=budget_result,
                    request_payload=payload,
                    accumulated_output_text="".join(assistant_parts),
                )
                recorded = True
                yield self._sse_done()
            except Exception as exc:
                if not recorded:
                    self._record_gateway_request(
                        endpoint="/openai/v1/responses",
                        method="POST",
                        status_code=502,
                        duration=time.perf_counter() - started_at,
                        ai_model=model,
                        requested_model=payload.get("model"),
                        response_payload=None,
                        upstream_response=None,
                        endpoint_kind="responses_stream",
                        budget_result=budget_result,
                        error_detail=str(exc),
                        request_payload=payload,
                    )
                    recorded = True
                raise
            finally:
                # See stream_message: account for tokens consumed before a
                # client disconnect (GeneratorExit).
                if not recorded:
                    self._record_stream_abort(
                        endpoint="/openai/v1/responses",
                        endpoint_kind="responses_stream",
                        started_at=started_at,
                        ai_model=model,
                        payload=payload,
                        usage_details=final_usage_details or final_usage,
                        budget_result=budget_result,
                        accumulated_output_text="".join(assistant_parts),
                    )

        return event_stream()

    def _get_account_models(self) -> List[AIModel]:
        account_id = self.auth_context.user.account_id
        from preloop.models.crud.ai_model import ai_model as crud_ai_model

        return crud_ai_model.get_all_for_account(self.db, account_id=account_id)

    def _authorized_model_ids(self, account_models: List[AIModel]) -> frozenset[str]:
        """Return the model-id set this principal may use (memoized per request).

        Args:
            account_models: Full account model inventory.

        Returns:
            Frozen set of authorized ``AIModel`` id strings computed once per
            service instance so every surface (listing, alias resolution,
            default selection) consumes the same set.
        """
        if self._authorized_model_ids_cache is None:
            self._authorized_model_ids_cache = compute_authorized_model_ids(
                self.db, self.auth_context, account_models
            )
        return self._authorized_model_ids_cache

    def _resolve_requested_model(
        self, requested_model: Optional[str], *, provider: GatewayProvider
    ) -> AIModel:
        models = self._get_account_models()
        authorized_ids = self._authorized_model_ids(models)
        gateway_enabled_models: List[tuple[AIModel, str]] = []
        unauthorized_gateway_models: List[tuple[AIModel, str]] = []
        default_gateway_model: Optional[AIModel] = None
        for ai_model in models:
            runtime = resolve_ai_model_runtime(ai_model)
            if runtime.model_gateway_enabled and runtime.model_gateway_model_alias:
                if str(ai_model.id) not in authorized_ids:
                    # Principal-bound models outside this credential's
                    # authorized set never match or become the default; they
                    # are kept only to distinguish 400 (bound to another
                    # agent) from 404 (unknown model) below.
                    unauthorized_gateway_models.append(
                        (ai_model, runtime.model_gateway_model_alias)
                    )
                    continue
                gateway_enabled_models.append(
                    (ai_model, runtime.model_gateway_model_alias)
                )
                if ai_model.is_default:
                    default_gateway_model = ai_model

        if requested_model:
            # Resolution must be deterministic: the resolved row decides which
            # ai_model_id the request is billed and priced against. Two rules,
            # in order:
            #   1. An exact alias match always wins, regardless of position.
            #   2. Otherwise the first provider-suffix match wins, in the stable
            #      order given by get_all_for_account (account models before
            #      system defaults, then oldest-first).
            # Without (1) a suffix match on an earlier row would beat an exact
            # match on a later one; without the stable ordering behind (2) a bare
            # "claude-sonnet-4-5" could resolve to anthropic/... on one request
            # and bedrock/... on the next, silently changing the price.
            # Context-window variant markers ("claude-fable-5[1m]") address
            # the same registry row as their base id; the variant selector
            # itself is preserved in the request payload and forwarded
            # verbatim on the Anthropic OAuth passthrough, so the user's 1M
            # selection keeps working while authorization and pricing key on
            # the base model.
            normalized_requested = self._strip_claude_variant_marker(
                str(requested_model)
            )
            suffix_match: Optional[AIModel] = None
            for ai_model, alias in gateway_enabled_models:
                if alias == requested_model or alias == normalized_requested:
                    return ai_model
                if suffix_match is None and (
                    alias.endswith(f"/{requested_model}")
                    or alias.endswith(f"/{normalized_requested}")
                ):
                    suffix_match = ai_model
            if suffix_match is not None:
                return suffix_match
            for _, alias in unauthorized_gateway_models:
                if alias == requested_model or alias.endswith(f"/{requested_model}"):
                    available = (
                        ", ".join(
                            authorized_alias
                            for _, authorized_alias in gateway_enabled_models
                        )
                        or "none"
                    )
                    raise ModelGatewayAPIError(
                        provider=provider,
                        status_code=400,
                        message=(
                            f"Model '{requested_model}' is bound to another "
                            "agent's subscription credentials and can't serve "
                            "this credential. Models available to this "
                            f"credential: {available}."
                        ),
                        code="model_not_authorized",
                    )
            autoregistered = self._maybe_autoregister_claude_family_model(
                requested_model,
                provider=provider,
                gateway_enabled_models=gateway_enabled_models,
            )
            if autoregistered is not None:
                return autoregistered
            raise ModelGatewayAPIError(
                provider=provider,
                status_code=404,
                message="Requested model not found",
            )

        if default_gateway_model:
            return default_gateway_model

        raise ModelGatewayAPIError(
            provider=provider,
            status_code=404,
            message="No gateway-enabled default model configured",
        )

    @staticmethod
    def _strip_claude_variant_marker(model_ref: str) -> str:
        """Strip a trailing bracketed context-window marker.

        Claude Code addresses context-window variants as ``<model>[1m]``. The
        variant is a real upstream selector (forwarded verbatim on the OAuth
        passthrough) but registry rows and pricing keys use the base id.
        """
        trimmed = (model_ref or "").strip()
        open_idx = trimmed.rfind("[")
        if open_idx > 0 and trimmed.endswith("]"):
            base = trimmed[:open_idx].strip()
            if base:
                return base
        return trimmed

    def _maybe_autoregister_claude_family_model(
        self,
        requested_model: Optional[str],
        *,
        provider: GatewayProvider,
        gateway_enabled_models: List[tuple[AIModel, str]],
    ) -> Optional[AIModel]:
        """Auto-register an unknown ``claude-*`` model for subscription OAuth.

        Claude Code updates ship new built-in dated model identifiers (and the
        family env pins may reference a family the onboarding import missed).
        With a registry snapshotted at onboard time those requests would 404
        until the user re-onboards — even though Anthropic itself authorizes
        whatever the subscription may use. When this account already holds an
        authorized Anthropic subscription-OAuth model, an unknown ``claude-*``
        request lazily creates a sibling ``AIModel`` row sharing the same
        credential secret (one live OAuth token lineage — never a copy), binds
        it to the requesting managed agent so principal-bound authorization
        admits it, and serves the request. Mirrors the self-healing price
        lookup pattern: first request pays a small write, every later request
        resolves normally.

        Only the registry check is relaxed. Budget preflight, subject-scoped
        ``allowed_models``, attribution, and usage accounting run unchanged on
        the returned model.

        Args:
            requested_model: The client's requested model string.
            provider: Gateway protocol of the request.
            gateway_enabled_models: Authorized (model, alias) pairs for this
                principal, from the caller's resolution pass.

        Returns:
            The newly registered model, or ``None`` when preconditions fail
            (feature disabled, non-Anthropic protocol, non-claude identifier,
            or no subscription-OAuth template model to share credentials
            with) — the caller then raises its usual 404.
        """
        if not settings.model_gateway_claude_family_autoregister_enabled:
            return None
        if provider != "anthropic" or not requested_model:
            return None
        base_requested = self._strip_claude_variant_marker(str(requested_model))
        # Accept "anthropic/<id>" and bare "<id>" spellings; reject other
        # providers ("bedrock/...") — their identifiers are not reachable
        # through the shared Anthropic OAuth credential.
        prefix, separator, tail = base_requested.partition("/")
        if separator:
            if prefix.strip().lower() != "anthropic":
                return None
            base_requested = tail.strip()
        if not base_requested.lower().startswith("claude-"):
            return None

        template: Optional[AIModel] = None
        for ai_model, _alias in gateway_enabled_models:
            if (
                (ai_model.provider_name or "").strip().lower() == "anthropic"
                and ai_model.credential_type
                == ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE
                and ai_model.credentials_secret_id is not None
            ):
                template = ai_model
                break
        if template is None:
            return None

        managed_agent_id = resolve_managed_agent_id_for_context(
            self.db, self.auth_context
        )
        if managed_agent_id is None:
            # Principal-bound OAuth models are only authorized through an
            # agent binding; without an agent to bind, the new row would be
            # unauthorized for this credential on the very next request.
            return None

        account_id = self.auth_context.user.account_id
        alias = f"anthropic/{base_requested}"
        template_meta = (
            template.meta_data if isinstance(template.meta_data, dict) else {}
        )
        template_gateway = (
            template_meta.get("gateway")
            if isinstance(template_meta.get("gateway"), dict)
            else {}
        )
        # Savepoint scope: a failure here must undo ONLY the auto-registration
        # writes. A session-level rollback would also discard unrelated pending
        # state from earlier in the request pipeline, so both rows flush inside
        # one nested transaction (commit=False keeps the CRUD layer from
        # committing the outer transaction mid-savepoint) and the final commit
        # happens only after the savepoint released cleanly.
        try:
            with self.db.begin_nested():
                created = crud_ai_model.create_with_account(
                    self.db,
                    obj_in={
                        "name": f"Claude Code {alias}",
                        "description": (
                            "Auto-registered by the model gateway for a Claude "
                            "Code subscription-OAuth request."
                        ),
                        "provider_name": "anthropic",
                        "model_identifier": base_requested,
                        "api_endpoint": template.api_endpoint,
                        "credentials_secret_id": template.credentials_secret_id,
                        "meta_data": {
                            "gateway": {
                                "enabled": True,
                                "url": template_gateway.get("url"),
                                "provider_adapter": template_gateway.get(
                                    "provider_adapter", "preloop"
                                ),
                                "model_alias": alias,
                            },
                            "managed_by": ("model-gateway claude-family autoregister"),
                            "source_agent": "claude_code",
                            "managed_agent_id": managed_agent_id,
                            "autoregistered_from_ai_model_id": str(template.id),
                        },
                    },
                    account_id=account_id,
                    commit=False,
                )
                from datetime import timezone as _tz

                now = datetime.now(_tz.utc)
                crud_managed_agent_ai_model_binding.create(
                    self.db,
                    obj_in={
                        "account_id": account_id,
                        "managed_agent_id": managed_agent_id,
                        "ai_model_id": created.id,
                        "binding_type": "configured",
                        "config_key": f"gateway.autoregister.{base_requested}",
                        "gateway_alias": alias,
                        "is_primary": False,
                        "status": "gateway_ready",
                        "first_seen_at": now,
                        "last_seen_at": now,
                    },
                    commit=False,
                )
            self.db.commit()
        except (SQLAlchemyError, ValueError):
            # begin_nested already rolled back to the savepoint; unrelated
            # pending session state from earlier in the pipeline survives.
            logger.warning(
                "Claude family auto-registration failed for %s",
                requested_model,
                exc_info=True,
            )
            return None
        # The memoized authorized-id set predates the new row and binding.
        self._authorized_model_ids_cache = None
        logger.info(
            "Auto-registered Claude family model %s for managed agent %s",
            alias,
            managed_agent_id,
        )
        return created

    def _is_openai_codex_model(self, ai_model: AIModel) -> bool:
        return (ai_model.provider_name or "").strip().lower() == "openai-codex"

    def _resolve_openai_codex_credentials(
        self, ai_model: AIModel
    ) -> ResolvedModelCredentials:
        # Reset per request (mirrors _build_completion_kwargs) so an errored
        # resolution never leaves a stale value on the usage row.
        self._last_upstream_credential_type = None
        try:
            resolved = get_secret_service().resolve_ai_model_credentials(
                ai_model,
                db=self.db,
                allow_refresh=True,
            )
        except CredentialRefreshError as exc:
            # A failed subscription-OAuth refresh (e.g. refresh_token_reused
            # after the ChatGPT session rotated elsewhere) means the stored
            # credential needs re-authorization — an auth error, not a 500.
            status_code = 401
            if exc.status_code is not None and exc.status_code >= 500:
                status_code = 502
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=status_code,
                message=(
                    "OpenAI Codex OAuth credentials could not be refreshed. "
                    "Run `codex login` on the agent host and rerun onboarding "
                    "to reconnect the model gateway."
                ),
                code=exc.code,
            ) from exc
        if (
            not resolved
            or resolved.credential_type != OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE
        ):
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=400,
                message="OpenAI Codex OAuth credentials are not configured",
            )
        # Codex always authenticates upstream via subscription OAuth: no
        # marginal dollar cost, so savings denominate as rate-limit window.
        self._last_upstream_credential_type = "oauth"
        payload = resolved.payload or {}
        if not resolved.value or not payload.get("account_id"):
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=400,
                message="OpenAI Codex OAuth credentials are incomplete",
            )
        return resolved

    def _build_openai_codex_payload(
        self, ai_model: AIModel, payload: Dict[str, Any], *, stream: bool = False
    ) -> Dict[str, Any]:
        upstream_payload = json.loads(json.dumps(payload))
        upstream_payload["model"] = ai_model.model_identifier
        if stream:
            upstream_payload["stream"] = True
        return upstream_payload

    # Default ``instructions`` used when a chat-completions request targets a
    # Codex OAuth model but provides no system message. Codex requires the
    # ``instructions`` field to be a non-empty string, so we always supply one.
    _DEFAULT_CODEX_INSTRUCTIONS = (
        "You are a helpful assistant operating through the Preloop model "
        "gateway. Follow the user's instructions carefully and use the "
        "provided tools when appropriate."
    )

    def _build_openai_codex_payload_from_chat_completion(
        self,
        *,
        payload: Dict[str, Any],
        messages: List[Dict[str, Any]],
        ai_model: Optional[AIModel] = None,
    ) -> Dict[str, Any]:
        """Translate a chat-completions payload into a Codex Responses-API one.

        The OpenAI Codex backend exposes a Responses-API style endpoint, which
        differs from chat-completions in three important ways:

        1. System prompts are passed via the top-level ``instructions`` field,
           not as a ``role: system`` message inside ``input``. The endpoint
           rejects requests without ``instructions`` (HTTP 400 "Instructions
           are required").
        2. Tool calls produced by the assistant must be encoded as
           ``function_call`` items, and tool results as ``function_call_output``
           items, rather than as ``role: assistant``/``role: tool`` messages.
        3. Assistant text must use the ``output_text`` content type while user
           text uses ``input_text``.

        This helper performs the lossless translation so chat-completions
        clients (e.g. Hermes via ``provider: custom``) can transparently target
        a Codex OAuth model, including across multi-turn tool conversations.
        """

        instructions_parts: List[str] = []
        input_items: List[Dict[str, Any]] = []

        for message in messages:
            role = message.get("role")
            text = self._content_to_text(message.get("content", ""))

            if role == "system":
                if text:
                    instructions_parts.append(text)
                continue

            if role == "tool":
                call_id = message.get("tool_call_id")
                if not call_id:
                    # Without a call_id we cannot link the result back to a
                    # function_call, so drop it rather than send an item
                    # Codex would reject.
                    continue
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(call_id),
                        "output": text,
                    }
                )
                continue

            if role == "assistant":
                tool_calls = message.get("tool_calls") or []
                if text:
                    input_items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": text},
                            ],
                        }
                    )
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function") or {}
                    call_id = tool_call.get("id") or function.get("id")
                    if not call_id:
                        continue
                    arguments = function.get("arguments", "")
                    if not isinstance(arguments, str):
                        try:
                            arguments = json.dumps(arguments)
                        except (TypeError, ValueError):
                            arguments = str(arguments)
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": str(call_id),
                            "name": function.get("name", ""),
                            "arguments": arguments,
                        }
                    )
                continue

            # user / developer / function / unknown roles → input message
            input_items.append(
                {
                    "type": "message",
                    "role": role or "user",
                    "content": [
                        {"type": "input_text", "text": text},
                    ],
                }
            )

        # ``instructions`` is required by the Codex Responses endpoint. Prefer
        # any explicit value passed through, then any synthesised system
        # messages, and finally fall back to a generic default.
        explicit_instructions = payload.get("instructions")
        if isinstance(explicit_instructions, str) and explicit_instructions.strip():
            instructions = explicit_instructions
        elif instructions_parts:
            instructions = "\n\n".join(instructions_parts)
        else:
            instructions = self._DEFAULT_CODEX_INSTRUCTIONS

        upstream_payload: Dict[str, Any] = {
            # The Codex Responses backend identifies models by the upstream
            # provider's identifier (e.g. ``gpt-5-codex``), not the gateway
            # alias the chat-completions client passed in.
            "model": (
                ai_model.model_identifier
                if ai_model is not None
                else payload.get("model")
            ),
            "input": input_items,
            "instructions": instructions,
            # Codex rejects requests without ``store: false`` (HTTP 400 "Store
            # must be set to false"). The native codex-cli always sends this
            # flag; we must replicate it for chat-completion clients too.
            "store": False,
        }
        for key in (
            "temperature",
            "top_p",
            "max_output_tokens",
            "max_completion_tokens",
            "parallel_tool_calls",
        ):
            if payload.get(key) is not None:
                upstream_payload[key] = payload[key]

        # Tools and tool_choice need shape translation: chat-completions nests
        # the function spec under a ``function`` key, while the Codex Responses
        # API expects the function fields (``name``, ``description``,
        # ``parameters``, ``strict``) to be flattened onto the tool entry
        # itself. Sending the chat-completions shape unchanged triggers
        # ``HTTP 400: Missing required parameter: 'tools[0].name'``.
        translated_tools = self._translate_chat_tools_to_codex(payload.get("tools"))
        if translated_tools is not None:
            upstream_payload["tools"] = translated_tools
        translated_tool_choice = self._translate_chat_tool_choice_to_codex(
            payload.get("tool_choice")
        )
        if translated_tool_choice is not None:
            upstream_payload["tool_choice"] = translated_tool_choice

        return upstream_payload

    @staticmethod
    def _translate_chat_tools_to_codex(
        tools: Any,
    ) -> Optional[List[Dict[str, Any]]]:
        """Convert chat-completions ``tools`` into Codex Responses-API form.

        Chat-completions tools look like::

            {"type": "function",
             "function": {"name": ..., "description": ..., "parameters": ...}}

        Codex (and the OpenAI Responses API in general) expects the function
        spec to be flattened onto the tool entry itself::

            {"type": "function", "name": ..., "description": ...,
             "parameters": ..., "strict": ...}

        Non-function tools (already in Responses-API form, or any other
        ``type``) are passed through verbatim so we don't break any
        future-Codex tool kind we don't yet know about.
        """
        if tools is None:
            return None
        if not isinstance(tools, list):
            return tools  # type: ignore[return-value]

        translated: List[Dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                translated.append(tool)
                continue
            function = tool.get("function")
            if tool.get("type") == "function" and isinstance(function, dict):
                flattened: Dict[str, Any] = {"type": "function"}
                if "name" in function:
                    flattened["name"] = function["name"]
                if "description" in function:
                    flattened["description"] = function["description"]
                if "parameters" in function:
                    flattened["parameters"] = function["parameters"]
                if "strict" in function:
                    flattened["strict"] = function["strict"]
                # Preserve any additional top-level fields the caller set
                # directly on the tool entry (e.g. ``strict`` at the top
                # level), but never let ``function`` leak through.
                for key, value in tool.items():
                    if key in {"function", "type"}:
                        continue
                    flattened.setdefault(key, value)
                translated.append(flattened)
            else:
                translated.append(tool)
        return translated

    @staticmethod
    def _translate_chat_tool_choice_to_codex(tool_choice: Any) -> Any:
        """Convert chat-completions ``tool_choice`` into Codex/Responses form.

        Chat-completions encodes a forced function call as
        ``{"type": "function", "function": {"name": "foo"}}``. The Codex
        Responses API expects ``{"type": "function", "name": "foo"}``. Plain
        string values (``"auto"``, ``"none"``, ``"required"``) and any unknown
        shapes are returned untouched.
        """
        if tool_choice is None:
            return None
        if not isinstance(tool_choice, dict):
            return tool_choice
        function = tool_choice.get("function")
        if tool_choice.get("type") == "function" and isinstance(function, dict):
            flattened: Dict[str, Any] = {"type": "function"}
            if "name" in function:
                flattened["name"] = function["name"]
            return flattened
        return tool_choice

    def _codex_response_to_chat_completion_dict(
        self, response_dict: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Convert a Codex/Responses-API response into chat-completion shape.

        The OpenAI Codex backend returns Responses-API style payloads regardless
        of the gateway endpoint we surface to clients. When a chat-completions
        client (e.g. Hermes via ``provider: custom``) targets a Codex OAuth
        model, we still need to emit an OpenAI chat-completions response. This
        helper performs the lossless transcoding so downstream extractors that
        expect ``choices[0].message`` continue to work.
        """

        assistant_text_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        output_items = response_dict.get("output") or []
        if isinstance(output_items, list):
            for item in output_items:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type == "message" and item.get("role") == "assistant":
                    for part in item.get("content") or []:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") in {"output_text", "text"}:
                            assistant_text_parts.append(str(part.get("text", "")))
                elif item_type == "function_call":
                    call_id = (
                        item.get("call_id")
                        or item.get("id")
                        or f"call_{len(tool_calls)}"
                    )
                    tool_calls.append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": item.get("name", ""),
                                "arguments": item.get("arguments", ""),
                            },
                        }
                    )

        assistant_text = "".join(assistant_text_parts)
        if not assistant_text:
            fallback = response_dict.get("output_text")
            if fallback:
                assistant_text = str(fallback).strip()

        message: Dict[str, Any] = {"role": "assistant", "content": assistant_text}
        if tool_calls:
            message["tool_calls"] = tool_calls

        finish_reason = "tool_calls" if tool_calls and not assistant_text else "stop"

        return {
            "id": response_dict.get("id", f"chatcmpl_{int(time.time())}"),
            "object": "chat.completion",
            "created": response_dict.get(
                "created", response_dict.get("created_at", int(time.time()))
            ),
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": response_dict.get("usage", {}),
        }

    def _create_openai_codex_response(
        self, ai_model: AIModel, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Call the Codex Responses backend and return the final response dict.

        The Codex Responses backend now rejects non-streaming requests with
        ``HTTP 400: Stream must be set to true``. We therefore always send
        ``stream: true`` and consume the SSE stream until the terminal
        ``response.completed`` event, whose ``response`` field contains the
        fully assembled response object. Callers continue to receive a single
        Responses-API style ``dict`` so the surrounding code is unchanged.
        """
        credentials = self._resolve_openai_codex_credentials(ai_model)
        upstream_payload = self._build_openai_codex_payload(
            ai_model, payload, stream=True
        )
        headers = {
            "Authorization": f"Bearer {credentials.value}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "OpenAI-Beta": "responses=experimental",
            "chatgpt-account-id": str(credentials.payload.get("account_id")),
            "originator": "preloop",
            "User-Agent": "Preloop/1.0",
        }
        req = urllib_request.Request(
            "https://chatgpt.com/backend-api/codex/responses",
            data=json.dumps(upstream_payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=600) as response:
                return self._aggregate_codex_sse_stream(response)
        except urllib_error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=exc.code,
                message=detail or "OpenAI Codex upstream request failed",
            ) from exc
        except urllib_error.URLError as exc:
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=502,
                message=f"OpenAI Codex upstream request failed: {exc.reason}",
            ) from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=502,
                message="OpenAI Codex upstream returned invalid JSON",
            ) from exc

    def _aggregate_codex_sse_stream(self, response: Any) -> Dict[str, Any]:
        """Aggregate a Codex Responses SSE stream into a final response dict.

        Codex (``chatgpt.com/backend-api/codex/responses``) emits typed SSE
        events. We deliberately avoid trusting the terminal
        ``response.completed`` event as the source of truth: that event is
        known to embed the full ~30 KB system prompt and is therefore both
        slow and prone to truncation across SSE buffers (see vercel/ai#14473
        and the OpenAI Codex CLI's own SSE strategy of *skipping* truncated
        ``response.*`` status events). Instead we incrementally build the
        output from the small, reliable streaming events:

        * ``response.output_item.added`` / ``response.output_item.done``
          announce ``message`` and ``function_call`` items.
        * ``response.output_text.delta`` / ``response.output_text.done``
          carry assistant text per item.
        * ``response.function_call_arguments.delta`` /
          ``response.function_call_arguments.done`` carry the arguments JSON
          per ``function_call`` item.
        * ``response.failed`` / ``response.error`` carry upstream errors.

        This matches the official Codex CLI behaviour and means tool-only
        turns (which produce zero text deltas, only function-call argument
        deltas) still surface their tool calls — the original failure mode
        Hermes hit when asking ``pay $6 to Joe``.
        """
        items_by_id: Dict[str, Dict[str, Any]] = {}
        item_order: List[str] = []
        text_by_item: Dict[str, List[str]] = {}
        args_by_item: Dict[str, List[str]] = {}
        last_response_id: Optional[str] = None
        usage: Optional[Dict[str, Any]] = None
        completed_response: Optional[Dict[str, Any]] = None
        upstream_error: Optional[Dict[str, Any]] = None

        def _ensure_item(item_id: Optional[str], default: Dict[str, Any]) -> str:
            key = item_id or f"_synthetic_{len(item_order)}"
            if key not in items_by_id:
                items_by_id[key] = default
                item_order.append(key)
            return key

        for event in self._iter_sse_events(response):
            event_type = event.get("type")

            if event_type in {"response.created", "response.in_progress"}:
                resp = event.get("response")
                if isinstance(resp, dict):
                    last_response_id = resp.get("id") or last_response_id
                    maybe_usage = resp.get("usage")
                    if isinstance(maybe_usage, dict):
                        usage = maybe_usage
                continue

            if event_type in {
                "response.output_item.added",
                "response.output_item.done",
            }:
                item = event.get("item")
                if not isinstance(item, dict):
                    continue
                item_id = item.get("id")
                key = _ensure_item(item_id, item.copy())
                # On ``done`` the item carries the complete state (e.g. final
                # arguments string for a ``function_call``); merge it in.
                if event_type == "response.output_item.done":
                    items_by_id[key] = {**items_by_id[key], **item}
                continue

            if event_type == "response.output_text.delta":
                item_id = event.get("item_id")
                if not item_id:
                    continue
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    text_by_item.setdefault(item_id, []).append(delta)
                continue

            if event_type == "response.output_text.done":
                item_id = event.get("item_id")
                final_text = event.get("text")
                if item_id and isinstance(final_text, str):
                    # Replace any deltas with the authoritative final text.
                    text_by_item[item_id] = [final_text]
                continue

            if event_type == "response.function_call_arguments.delta":
                item_id = event.get("item_id")
                if not item_id:
                    continue
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    args_by_item.setdefault(item_id, []).append(delta)
                continue

            if event_type == "response.function_call_arguments.done":
                item_id = event.get("item_id")
                final_args = event.get("arguments")
                if item_id and isinstance(final_args, str):
                    args_by_item[item_id] = [final_args]
                continue

            if event_type == "response.completed":
                resp = event.get("response")
                if isinstance(resp, dict):
                    completed_response = resp
                    last_response_id = resp.get("id") or last_response_id
                    maybe_usage = resp.get("usage")
                    if isinstance(maybe_usage, dict):
                        usage = maybe_usage
                continue

            if event_type in {"response.failed", "response.error", "error"}:
                resp = event.get("response") or event
                if isinstance(resp, dict):
                    upstream_error = resp.get("error") or resp
                continue

            # Unknown event type — ignore to stay forward-compatible with
            # future Codex event kinds (reasoning summaries, web search, …).

        if upstream_error is not None:
            message = "Codex upstream returned an error"
            if isinstance(upstream_error, dict):
                message = (
                    upstream_error.get("message")
                    or upstream_error.get("detail")
                    or message
                )
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=502,
                message=str(message),
            )

        # Materialise final ``output`` items from the per-item buffers.
        output: List[Dict[str, Any]] = []
        aggregate_text_parts: List[str] = []
        for key in item_order:
            item = dict(items_by_id[key])
            item_type = item.get("type")
            if item_type == "message":
                text = "".join(text_by_item.get(item.get("id") or key, []))
                if text:
                    item["content"] = [{"type": "output_text", "text": text}]
                    aggregate_text_parts.append(text)
                else:
                    item.setdefault("content", item.get("content") or [])
                output.append(item)
            elif item_type == "function_call":
                args = "".join(args_by_item.get(item.get("id") or key, []))
                if args or not item.get("arguments"):
                    item["arguments"] = args
                output.append(item)
            else:
                output.append(item)

        # If we somehow never observed any items but ``response.completed``
        # gave us a populated payload, prefer that as a last-resort fallback.
        if not output and completed_response is not None:
            return completed_response

        aggregate_text = "".join(aggregate_text_parts)
        return {
            "id": last_response_id
            or (completed_response or {}).get("id")
            or f"resp_{int(time.time())}",
            "output": output,
            "output_text": aggregate_text,
            "usage": usage or (completed_response or {}).get("usage") or {},
        }

    @staticmethod
    def _iter_sse_events(response: Any) -> Iterator[Dict[str, Any]]:
        """Yield decoded JSON event payloads from a Codex SSE stream.

        Codex emits standard ``text/event-stream`` records: blank-line
        delimited blocks of ``event:`` and ``data:`` fields. For our purposes
        we only care about the JSON ``data:`` payload (the ``event:`` line
        duplicates ``data.type`` so we read the type from the JSON).

        Per the official Codex CLI strategy, JSON parse failures on individual
        events are *skipped* rather than fatal — large ``response.completed``
        events occasionally arrive truncated and we must keep consuming the
        smaller delta events that follow.
        """
        data_lines: List[str] = []
        for raw_line in response:
            if isinstance(raw_line, bytes):
                line = raw_line.decode("utf-8", "ignore")
            else:
                line = str(raw_line)
            line = line.rstrip("\r\n")
            if not line:
                if data_lines:
                    payload_text = "\n".join(data_lines).strip()
                    data_lines = []
                    if payload_text and payload_text != "[DONE]":
                        try:
                            yield json.loads(payload_text)
                        except json.JSONDecodeError:
                            logger.debug(
                                "Skipping unparseable Codex SSE event (%d bytes)",
                                len(payload_text),
                            )
                            continue
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            # Ignore ``event:``, ``id:``, comments, etc. — we read the type
            # from the JSON payload itself.

        if data_lines:
            payload_text = "\n".join(data_lines).strip()
            if payload_text and payload_text != "[DONE]":
                try:
                    yield json.loads(payload_text)
                except json.JSONDecodeError:
                    logger.debug(
                        "Skipping unparseable trailing Codex SSE event (%d bytes)",
                        len(payload_text),
                    )
                    return

    def _stream_openai_codex_response(
        self,
        *,
        ai_model: AIModel,
        payload: Dict[str, Any],
        started_at: float,
        budget_result: Optional[BudgetCheckResult],
    ) -> Iterator[str]:
        requested_model = (
            payload.get("model")
            or resolve_ai_model_runtime(ai_model).model_gateway_model_alias
        )

        def event_stream() -> Iterator[str]:
            response_dict = self._create_openai_codex_response(ai_model, payload)
            response_payload = self._build_responses_api_payload(
                ai_model=ai_model,
                requested_model=requested_model,
                response_dict=response_dict,
            )
            response_id = response_payload["id"]
            created_at = response_payload["created_at"]
            output_items = response_payload["output"]
            assistant_text = response_payload["output_text"]
            text_item = next(
                (
                    item
                    for item in output_items
                    if item.get("type") == "message" and item.get("role") == "assistant"
                ),
                None,
            )
            text_output_index = output_items.index(text_item) if text_item else None
            try:
                yield self._sse_event(
                    {
                        "type": "response.created",
                        "response": {
                            "id": response_id,
                            "object": "response",
                            "created_at": created_at,
                            "model": requested_model,
                            "status": "in_progress",
                        },
                    }
                )
                for index, item in enumerate(output_items):
                    yield self._sse_event(
                        {
                            "type": "response.output_item.added",
                            "response_id": response_id,
                            "output_index": index,
                            "item": item,
                        }
                    )
                if text_item and text_output_index is not None:
                    item_id = text_item.get("id", f"msg_{response_id}")
                    yield self._sse_event(
                        {
                            "type": "response.content_part.added",
                            "item_id": item_id,
                            "output_index": text_output_index,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": ""},
                        }
                    )
                    if assistant_text:
                        yield self._sse_event(
                            {
                                "type": "response.output_text.delta",
                                "item_id": item_id,
                                "output_index": text_output_index,
                                "content_index": 0,
                                "delta": assistant_text,
                            }
                        )
                    yield self._sse_event(
                        {
                            "type": "response.output_text.done",
                            "item_id": item_id,
                            "output_index": text_output_index,
                            "content_index": 0,
                            "text": assistant_text,
                        }
                    )
                    yield self._sse_event(
                        {
                            "type": "response.content_part.done",
                            "item_id": item_id,
                            "output_index": text_output_index,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": assistant_text},
                        }
                    )
                for index, item in enumerate(output_items):
                    yield self._sse_event(
                        {
                            "type": "response.output_item.done",
                            "output_index": index,
                            "item": item,
                        }
                    )
                yield self._sse_event(
                    {
                        "type": "response.completed",
                        "response": response_payload,
                    }
                )
                self._record_gateway_request(
                    endpoint="/openai/v1/responses",
                    method="POST",
                    status_code=200,
                    duration=time.perf_counter() - started_at,
                    ai_model=ai_model,
                    requested_model=payload.get("model"),
                    response_payload=response_payload,
                    upstream_response=response_dict,
                    endpoint_kind="responses_stream",
                    budget_result=budget_result,
                    request_payload=payload,
                )
                yield "data: [DONE]\n\n"
            except Exception as exc:
                self._record_gateway_request(
                    endpoint="/openai/v1/responses",
                    method="POST",
                    status_code=502,
                    duration=time.perf_counter() - started_at,
                    ai_model=ai_model,
                    requested_model=payload.get("model"),
                    response_payload=None,
                    upstream_response=None,
                    endpoint_kind="responses_stream",
                    budget_result=budget_result,
                    error_detail=str(exc),
                    request_payload=payload,
                )
                raise

        return event_stream()

    def _stream_openai_codex_chat_completion(
        self,
        *,
        ai_model: AIModel,
        payload: Dict[str, Any],
        messages: List[Dict[str, Any]],
        started_at: float,
        budget_result: Optional[BudgetCheckResult],
    ) -> Iterator[str]:
        """Fake-stream a Codex OAuth response as chat-completion SSE chunks.

        The OpenAI Codex backend only exposes a synchronous Responses-style
        endpoint, so we materialize the full reply once and emit it as a small
        sequence of OpenAI-compatible ``chat.completion.chunk`` events. This
        keeps clients that opt into ``stream=true`` against ``/openai/v1/chat/
        completions`` (e.g. Hermes' ``provider: custom``) functional even when
        the bound model uses ChatGPT OAuth credentials.
        """

        requested_model = (
            payload.get("model")
            or resolve_ai_model_runtime(ai_model).model_gateway_model_alias
        )

        def event_stream() -> Iterator[str]:
            recorded = False
            try:
                upstream_payload = (
                    self._build_openai_codex_payload_from_chat_completion(
                        payload=payload,
                        messages=messages,
                        ai_model=ai_model,
                    )
                )
                raw_codex_response = self._create_openai_codex_response(
                    ai_model, upstream_payload
                )
                response_dict = self._codex_response_to_chat_completion_dict(
                    raw_codex_response
                )
                response_id = response_dict.get("id", f"chatcmpl_{int(time.time())}")
                created_at = int(response_dict.get("created", time.time()))
                message = (response_dict.get("choices") or [{}])[0].get("message") or {}
                assistant_text = self._content_to_text(message.get("content", ""))
                tool_calls = message.get("tool_calls") or []
                finish_reason = self._extract_finish_reason(response_dict) or "stop"
                usage = self._normalize_usage(
                    response_dict.get("usage"),
                    prompt_key="prompt_tokens",
                    completion_key="completion_tokens",
                    output_names=("completion_tokens", "output_tokens"),
                )

                yield self._sse_event(
                    {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": created_at,
                        "model": requested_model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": ""},
                                "finish_reason": None,
                            }
                        ],
                    }
                )

                if assistant_text:
                    yield self._sse_event(
                        {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": created_at,
                            "model": requested_model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": assistant_text},
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )

                if tool_calls:
                    tool_call_deltas = []
                    for index, tool_call in enumerate(tool_calls):
                        function_payload = tool_call.get("function") or {}
                        tool_call_deltas.append(
                            {
                                "index": index,
                                "id": tool_call.get("id"),
                                "type": "function",
                                "function": {
                                    "name": function_payload.get("name", ""),
                                    "arguments": function_payload.get("arguments", ""),
                                },
                            }
                        )
                    yield self._sse_event(
                        {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": created_at,
                            "model": requested_model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"tool_calls": tool_call_deltas},
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )

                yield self._sse_event(
                    {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": created_at,
                        "model": requested_model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": finish_reason,
                            }
                        ],
                        "usage": usage,
                    }
                )

                response_payload = {
                    "id": response_id,
                    "object": "chat.completion",
                    "created": created_at,
                    "model": requested_model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": assistant_text,
                                **({"tool_calls": tool_calls} if tool_calls else {}),
                            },
                            "finish_reason": finish_reason,
                        }
                    ],
                    "usage": usage,
                }
                self._record_gateway_request(
                    endpoint="/openai/v1/chat/completions",
                    method="POST",
                    status_code=200,
                    duration=time.perf_counter() - started_at,
                    ai_model=ai_model,
                    requested_model=payload.get("model"),
                    response_payload=response_payload,
                    upstream_response=raw_codex_response,
                    endpoint_kind="chat_completions_stream",
                    budget_result=budget_result,
                    request_payload=payload,
                )
                recorded = True
                yield self._sse_done()
            except ModelGatewayAPIError as exc:
                if not recorded:
                    self._record_gateway_request(
                        endpoint="/openai/v1/chat/completions",
                        method="POST",
                        status_code=exc.status_code,
                        duration=time.perf_counter() - started_at,
                        ai_model=ai_model,
                        requested_model=payload.get("model"),
                        response_payload=None,
                        upstream_response=None,
                        endpoint_kind="chat_completions_stream",
                        budget_result=budget_result,
                        error_detail=exc.message,
                        request_payload=payload,
                    )
                raise
            except Exception as exc:
                if not recorded:
                    self._record_gateway_request(
                        endpoint="/openai/v1/chat/completions",
                        method="POST",
                        status_code=502,
                        duration=time.perf_counter() - started_at,
                        ai_model=ai_model,
                        requested_model=payload.get("model"),
                        response_payload=None,
                        upstream_response=None,
                        endpoint_kind="chat_completions_stream",
                        budget_result=budget_result,
                        error_detail=str(exc),
                        request_payload=payload,
                    )
                raise

        return event_stream()

    def _build_responses_api_payload(
        self,
        *,
        ai_model: AIModel,
        requested_model: str,
        response_dict: Dict[str, Any],
    ) -> Dict[str, Any]:
        output_items = self._build_response_output_items(response_dict)
        assistant_text = self._response_output_text(output_items)
        if not assistant_text:
            assistant_text = str(response_dict.get("output_text") or "").strip()
        if not assistant_text:
            assistant_text = self._extract_assistant_text(response_dict)
        usage = self._normalize_usage(
            response_dict.get("usage"),
            prompt_key="prompt_tokens",
            completion_key="completion_tokens",
            output_names=("completion_tokens", "output_tokens"),
        )
        return {
            "id": response_dict.get("id", f"resp_{int(time.time())}"),
            "object": "response",
            "created_at": response_dict.get(
                "created_at", response_dict.get("created", int(time.time()))
            ),
            "model": requested_model
            or resolve_ai_model_runtime(ai_model).model_gateway_model_alias,
            "status": "completed",
            "output": output_items,
            "output_text": assistant_text,
            "usage": {
                "input_tokens": usage["prompt_tokens"],
                "output_tokens": usage["completion_tokens"],
                "total_tokens": usage["total_tokens"],
            },
        }

    # ------------------------------------------------------------------
    # Anthropic subscription-OAuth passthrough.
    #
    # Rationale (0.12.2): Anthropic enforces that subscription-OAuth
    # requests carry the Claude Code sentinel as the *entire first system
    # block* (exact match) and rejects violations with a disguised 429
    # ``rate_limit_error``. The litellm path re-serializes the request
    # through OpenAI chat format: system blocks are joined with "\n",
    # ``cache_control`` markers are dropped, and the client's
    # ``anthropic-beta`` header is discarded — exactly the failing shape.
    # litellm's ``completion()`` cannot carry the Anthropic-native request
    # faithfully (its adapter rebuilds message/system blocks), so this
    # branch forwards the client's original JSON directly with httpx at
    # the one point where fidelity matters, while budget preflight,
    # governance tool-stripping, attribution, and usage recording keep
    # running exactly as on the litellm path.
    # ------------------------------------------------------------------
    def _anthropic_oauth_passthrough_token(self, ai_model: AIModel) -> Optional[str]:
        """Resolve the Claude Code subscription-OAuth token for ``ai_model``.

        Args:
            ai_model: The resolved gateway model for the request.

        Returns:
            The OAuth access token when the model authenticates upstream with
            the Claude Code subscription-OAuth credential type; ``None`` for
            API-key/ambient credentials (those keep using the litellm path).

        Raises:
            ModelGatewayAPIError: When the stored OAuth credential exists but
                could not be refreshed.
        """
        if (ai_model.provider_name or "").strip().lower() != "anthropic":
            return None
        self._last_upstream_credential_type = None
        try:
            resolved = get_secret_service().resolve_ai_model_credentials(
                ai_model,
                db=self.db,
                allow_refresh=True,
            )
        except CredentialRefreshError as exc:
            status_code = 401
            if exc.status_code is not None and exc.status_code >= 500:
                status_code = 502
            raise ModelGatewayAPIError(
                provider="anthropic",
                status_code=status_code,
                message=(
                    "Model credentials could not be refreshed. "
                    "Reconnect this managed agent or update the model credentials."
                ),
                code=exc.code,
            ) from exc
        if (
            resolved is not None
            and resolved.credential_type == ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE
            and bool(resolved.value)
        ):
            self._last_upstream_credential_type = "oauth"
            return str(resolved.value)
        return None

    def _strip_anthropic_passthrough_tools(
        self, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Apply governance tool-stripping without touching anything else.

        Only the ``tools`` array (and a dangling ``tool_choice``) may change;
        ``system`` blocks, message content, and every ``cache_control``
        marker are forwarded verbatim so the upstream's structural
        validation and prompt caching stay intact. Message-level context
        optimization is intentionally NOT applied on this path: rewriting
        blocks would break byte-fidelity and destroy the cache prefix.

        Args:
            payload: The original Anthropic-protocol request payload.

        Returns:
            The payload, replaced with a shallow-copied variant only when
            governance actually stripped a tool.
        """
        self._last_context_optimization = None
        if not self.auth_context.api_key:
            return payload
        try:
            raw_tools = payload.get("tools")
            if not isinstance(raw_tools, list) or not raw_tools:
                return payload
            meta_data = get_cached_account_meta_data(
                self.db, str(self.auth_context.user.account_id)
            )
            if meta_data is None:
                return payload
            subject_context = build_subject_context_from_api_key(
                self.auth_context.api_key
            )
            if not subject_governance_affects_gateway_context(
                meta_data,
                subject_context=subject_context,
                has_tools=True,
            ):
                return payload
            kept_tools, removed_names = strip_disabled_tools(
                raw_tools,
                meta_data=meta_data,
                subject_context=subject_context,
            )
            if not removed_names:
                return payload
            optimized: Dict[str, Any] = {**payload, "tools": kept_tools}
            if not kept_tools:
                optimized.pop("tools", None)
                optimized.pop("tool_choice", None)
            elif tool_choice_named_tool(optimized.get("tool_choice")) in set(
                removed_names
            ):
                # Anthropic shape: a forced {"type": "tool", "name": ...}
                # naming a stripped tool would 400 upstream; fall back to auto.
                optimized["tool_choice"] = {"type": "auto"}
            self._last_context_optimization = ContextOptimizationStats(
                stripped_tools=removed_names
            )
            return optimized
        except Exception:
            logger.warning(
                "Anthropic passthrough governance strip failed; "
                "forwarding request unchanged",
                exc_info=True,
            )
            self._last_context_optimization = None
            return payload

    def _passthrough_upstream_model_ref(
        self, ai_model: AIModel, requested_model: Any
    ) -> str:
        """Choose the upstream model string for the OAuth passthrough.

        Normally the account model's identifier. When the client requested a
        context-window variant of that same model ("claude-fable-5[1m]"),
        forward the variant verbatim: the bracket marker is a real Anthropic
        selector (the 1M-context form) that authorization and pricing key on
        the base id, but silently dropping it would downgrade the user's
        selected context window.
        """
        requested = str(requested_model or "").strip()
        base_identifier = (ai_model.model_identifier or "").strip()
        if requested and requested != base_identifier:
            requested_tail = requested.rpartition("/")[2]
            if (
                requested_tail != base_identifier
                and self._strip_claude_variant_marker(requested_tail) == base_identifier
            ):
                return requested_tail
        return base_identifier

    def _prepare_anthropic_passthrough(
        self,
        *,
        ai_model: AIModel,
        payload: Dict[str, Any],
        oauth_token: str,
        anthropic_version: Optional[str],
        anthropic_beta: Optional[str],
        stream: bool,
    ) -> tuple[str, Dict[str, str], Dict[str, Any]]:
        """Build the outbound URL, headers, and body for the OAuth passthrough.

        The body is a shallow copy of the client payload: ``system``,
        ``messages``, ``tools`` and all nested ``cache_control`` blocks are
        the client's own objects, forwarded untouched. Only ``model`` (the
        account model's upstream identifier) and ``stream`` are set by
        Preloop.

        Args:
            ai_model: The resolved gateway model.
            payload: Original Anthropic-protocol request payload
                (post governance tool-strip).
            oauth_token: The Claude Code subscription-OAuth access token.
            anthropic_version: The client's ``anthropic-version`` header.
            anthropic_beta: The client's ``anthropic-beta`` header, merged
                with the OAuth beta flag (client flags preserved so e.g.
                prompt-caching betas survive).
            stream: Whether the upstream call streams.

        Returns:
            Tuple of (url, headers, body).
        """
        original_tools = payload.get("tools")
        payload = self._strip_anthropic_passthrough_tools(payload)
        self._capture_tools_meta(original_tools)

        body: Dict[str, Any] = dict(payload)
        body["model"] = self._passthrough_upstream_model_ref(
            ai_model, payload.get("model")
        )
        body["stream"] = bool(stream)

        base_url = (
            str(ai_model.api_endpoint).rstrip("/")
            if ai_model.api_endpoint
            else ANTHROPIC_OAUTH_PASSTHROUGH_BASE_URL
        )
        url = f"{base_url}/v1/messages"

        beta_flags = [ANTHROPIC_OAUTH_BETA_FLAG]
        for flag in (anthropic_beta or "").split(","):
            flag = flag.strip()
            if flag and flag not in beta_flags:
                beta_flags.append(flag)
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream" if stream else "application/json",
            "authorization": f"Bearer {oauth_token}",
            "anthropic-version": (anthropic_version or "").strip()
            or ANTHROPIC_DEFAULT_API_VERSION,
            "anthropic-beta": ",".join(beta_flags),
            "anthropic-client-platform": "claude-code",
        }
        return url, headers, body

    @staticmethod
    def _anthropic_passthrough_upstream_error(
        status_code: int, body_text: str
    ) -> ModelGatewayAPIError:
        """Map an upstream Anthropic error body to a gateway error."""
        try:
            status_code = int(status_code)
        except (TypeError, ValueError):
            status_code = 502
        if status_code < 400 or status_code > 599:
            status_code = 502
        message = (body_text or "").strip() or "Anthropic upstream error"
        error_type: Optional[str] = None
        try:
            parsed = json.loads(body_text)
            error = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(error, dict):
                message = str(error.get("message") or message)
                raw_type = error.get("type")
                error_type = str(raw_type) if raw_type else None
        except (TypeError, ValueError):
            # Body is not JSON; fall back to the generic upstream message.
            pass
        return ModelGatewayAPIError(
            provider="anthropic",
            status_code=status_code,
            message=message,
            error_type=error_type,
        )

    def _anthropic_oauth_passthrough_complete(
        self, *, url: str, headers: Dict[str, str], body: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute a non-streaming passthrough request.

        Args:
            url: Upstream messages URL.
            headers: Outbound headers (auth + merged beta flags).
            body: The faithful Anthropic-native request body.

        Returns:
            The upstream response JSON, returned to the client verbatim.

        Raises:
            ModelGatewayAPIError: On transport failure or upstream >=400.
        """
        try:
            response = httpx.post(
                url,
                headers=headers,
                json=body,
                timeout=_ANTHROPIC_PASSTHROUGH_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise ModelGatewayAPIError(
                provider="anthropic",
                status_code=502,
                message=f"Gateway upstream error: {exc}",
            ) from exc
        if response.status_code >= 400:
            raise self._anthropic_passthrough_upstream_error(
                response.status_code, response.text
            )
        try:
            response_payload = response.json()
        except ValueError as exc:
            raise ModelGatewayAPIError(
                provider="anthropic",
                status_code=502,
                message="Gateway upstream error: invalid JSON from upstream",
            ) from exc
        if not isinstance(response_payload, dict):
            raise ModelGatewayAPIError(
                provider="anthropic",
                status_code=502,
                message="Gateway upstream error: unexpected upstream response shape",
            )
        return response_payload

    def _open_anthropic_oauth_passthrough_stream(
        self, *, url: str, headers: Dict[str, str], body: Dict[str, Any]
    ) -> tuple[httpx.Client, httpx.Response]:
        """Open a streaming passthrough request, eagerly checking the status.

        The connection is opened before the response generator is handed to
        the ASGI layer so upstream auth/validation errors surface as normal
        gateway errors (and get recorded) instead of dying mid-stream.

        Returns:
            Tuple of (client, response); the caller owns closing both.

        Raises:
            ModelGatewayAPIError: On transport failure or upstream >=400.
        """
        client = httpx.Client(timeout=_ANTHROPIC_PASSTHROUGH_TIMEOUT_SECONDS)
        try:
            request = client.build_request("POST", url, headers=headers, json=body)
            response = client.send(request, stream=True)
        except httpx.HTTPError as exc:
            client.close()
            raise ModelGatewayAPIError(
                provider="anthropic",
                status_code=502,
                message=f"Gateway upstream error: {exc}",
            ) from exc
        if response.status_code >= 400:
            try:
                body_text = response.read().decode("utf-8", errors="replace")
            except Exception:
                body_text = ""
            finally:
                response.close()
                client.close()
            raise self._anthropic_passthrough_upstream_error(
                response.status_code, body_text
            )
        return client, response

    def _anthropic_passthrough_event_stream(
        self,
        upstream_client: httpx.Client,
        upstream_response: httpx.Response,
        *,
        ai_model: AIModel,
        payload: Dict[str, Any],
        budget_result: Optional[BudgetCheckResult],
        started_at: float,
    ) -> Iterator[str]:
        """Relay upstream SSE verbatim while accumulating usage for accounting.

        Chunks are yielded exactly as received (Claude Code consumes
        Anthropic-native SSE directly). A line parser watches the ``data:``
        events on the side to collect the response id, usage (including
        cache token breakdown), stop reason, and assistant text for the
        usage record — mirroring what the litellm stream path records.
        """
        requested_model = payload.get("model")

        def _consume_sse_line(
            line: str,
            state: Dict[str, Any],
        ) -> None:
            line = line.strip()
            if not line.startswith("data:"):
                return
            try:
                event = json.loads(line[len("data:") :].strip())
            except ValueError:
                return
            if not isinstance(event, dict):
                return
            event_type = event.get("type")
            if event_type == "message_start":
                message = event.get("message")
                if isinstance(message, dict):
                    if message.get("id"):
                        state["response_id"] = message["id"]
                    if isinstance(message.get("usage"), dict):
                        state["usage"] = self._merge_usage_dicts(
                            state["usage"], message["usage"]
                        )
            elif event_type == "message_delta":
                delta = event.get("delta")
                if isinstance(delta, dict) and delta.get("stop_reason"):
                    state["stop_reason"] = delta["stop_reason"]
                if isinstance(event.get("usage"), dict):
                    state["usage"] = self._merge_usage_dicts(
                        state["usage"], event["usage"]
                    )
            elif event_type == "content_block_delta":
                delta = event.get("delta")
                if (
                    isinstance(delta, dict)
                    and delta.get("type") == "text_delta"
                    and isinstance(delta.get("text"), str)
                ):
                    state["text_parts"].append(delta["text"])

        def event_stream() -> Iterator[str]:
            state: Dict[str, Any] = {
                "response_id": None,
                "stop_reason": None,
                "usage": {},
                "text_parts": [],
            }
            buffer = ""
            recorded = False
            try:
                for chunk in upstream_response.iter_text():
                    if not chunk:
                        continue
                    yield chunk
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        _consume_sse_line(line, state)
                if buffer:
                    _consume_sse_line(buffer, state)

                response_id = state["response_id"] or f"msg_{int(time.time())}"
                normalized_usage = self._normalize_usage(
                    state["usage"],
                    prompt_key="input_tokens",
                    completion_key="output_tokens",
                    output_names=("output_tokens", "completion_tokens"),
                )
                accumulated_text = "".join(state["text_parts"])
                response_payload = self._build_anthropic_message_payload(
                    response_id=response_id,
                    model_name=requested_model,
                    assistant_text=accumulated_text,
                    stop_reason=state["stop_reason"],
                    usage=normalized_usage,
                )
                self._record_gateway_request(
                    endpoint="/anthropic/v1/messages",
                    method="POST",
                    status_code=200,
                    duration=time.perf_counter() - started_at,
                    ai_model=ai_model,
                    requested_model=requested_model,
                    response_payload=response_payload,
                    upstream_response={
                        "id": response_id,
                        "choices": [{"finish_reason": state["stop_reason"]}],
                        "usage": state["usage"],
                    },
                    endpoint_kind="anthropic_messages_stream",
                    budget_result=budget_result,
                    request_payload=payload,
                    accumulated_output_text=accumulated_text,
                )
                recorded = True
            except Exception as exc:
                if not recorded:
                    self._record_gateway_request(
                        endpoint="/anthropic/v1/messages",
                        method="POST",
                        status_code=502,
                        duration=time.perf_counter() - started_at,
                        ai_model=ai_model,
                        requested_model=requested_model,
                        response_payload=None,
                        upstream_response=None,
                        endpoint_kind="anthropic_messages_stream",
                        budget_result=budget_result,
                        error_detail=str(exc),
                        request_payload=payload,
                    )
                    recorded = True
                raise
            finally:
                # GeneratorExit (client disconnect) bypasses the except above;
                # bill already-consumed upstream tokens best-effort.
                if not recorded:
                    self._record_stream_abort(
                        endpoint="/anthropic/v1/messages",
                        endpoint_kind="anthropic_messages_stream",
                        started_at=started_at,
                        ai_model=ai_model,
                        payload=payload,
                        usage_details=state["usage"],
                        budget_result=budget_result,
                        accumulated_output_text="".join(state["text_parts"]),
                    )
                upstream_response.close()
                upstream_client.close()

        return event_stream()

    def _build_completion_kwargs(
        self,
        ai_model: AIModel,
        *,
        messages: List[Dict[str, Any]],
        payload: Dict[str, Any],
        stream: bool,
        provider: GatewayProvider,
    ) -> Dict[str, Any]:
        # Reset per request so a prior request's value never leaks if
        # resolution below raises before the credential type is determined.
        self._last_upstream_credential_type = None
        try:
            resolved_credentials = get_secret_service().resolve_ai_model_credentials(
                ai_model,
                db=self.db,
                allow_refresh=True,
            )
        except CredentialRefreshError as exc:
            status_code = 401
            if exc.status_code is not None and exc.status_code >= 500:
                status_code = 502
            raise ModelGatewayAPIError(
                provider=provider,
                status_code=status_code,
                message=(
                    "Model credentials could not be refreshed. "
                    "Reconnect this managed agent or update the model credentials."
                ),
                code=exc.code,
            ) from exc
        supports_ambient = _supports_ambient_provider_credentials(ai_model)
        supports_oauth = (
            provider == "anthropic"
            and resolved_credentials is not None
            and resolved_credentials.credential_type
            == ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE
            and bool(resolved_credentials.value)
        )
        supports_api_key = (
            resolved_credentials is not None
            and resolved_credentials.credential_type == "api_key"
            and bool(resolved_credentials.value)
        )
        if not (supports_api_key or supports_oauth or supports_ambient):
            raise ModelGatewayAPIError(
                provider=provider,
                status_code=400,
                message="Model credentials are not configured",
            )
        # Record the upstream credential type for savings denomination. oauth
        # (e.g. Claude Code subscription) has no marginal dollar cost, so its
        # savings are shown as a share of the rate-limit window, not dollars.
        if supports_oauth:
            self._last_upstream_credential_type = "oauth"
        elif supports_api_key:
            self._last_upstream_credential_type = "api_key"
        elif supports_ambient:
            self._last_upstream_credential_type = "ambient"

        kwargs: Dict[str, Any] = {
            "model": self._to_litellm_model(ai_model),
            "messages": messages,
            "timeout": 600,  # 10 minute timeout for massive concurrent prompts (PR Reviews)
        }
        if resolved_credentials and supports_ambient:
            kwargs.update(_bedrock_credential_kwargs(resolved_credentials.value or ""))
        if supports_oauth:
            kwargs["_preloop_anthropic_auth_token"] = resolved_credentials.value
            kwargs["extra_headers"] = {
                "anthropic-beta": "oauth-2025-04-20",
                "anthropic-client-platform": "claude-code",
            }
        if (
            supports_api_key
            and "api_key" not in kwargs
            and "aws_access_key_id" not in kwargs
        ):
            kwargs["api_key"] = resolved_credentials.value
        if region := _bedrock_region(ai_model):
            kwargs.setdefault("aws_region_name", region)
        if stream:
            kwargs["stream"] = True
            # Always request the final usage chunk from litellm. Without it,
            # litellm only exposes streaming usage via _hidden_params (which
            # the recording path never sees), so streamed requests were logged
            # with 0 tokens. litellm consumes stream_options itself and does
            # not forward it to providers that lack the parameter (verified:
            # Anthropic request bodies stay clean). The synthetic usage chunk
            # is stripped from the client-facing stream unless the client
            # opted in via its own stream_options.
            client_stream_options = payload.get("stream_options") or {}
            kwargs["stream_options"] = {
                **client_stream_options,
                "include_usage": True,
            }
        if ai_model.api_endpoint:
            kwargs["api_base"] = ai_model.api_endpoint
        if payload.get("tools") is not None:
            if provider == "anthropic":
                kwargs["tools"] = self._normalize_anthropic_tools(payload["tools"])
            else:
                kwargs["tools"] = self._normalize_openai_tools(payload["tools"])
        if payload.get("tool_choice") is not None:
            kwargs["tool_choice"] = self._normalize_openai_tool_choice(
                payload["tool_choice"]
            )
        if payload.get("parallel_tool_calls") is not None:
            kwargs["parallel_tool_calls"] = payload["parallel_tool_calls"]

        for source_key, target_key in (
            ("temperature", "temperature"),
            ("max_tokens", "max_tokens"),
            ("max_completion_tokens", "max_tokens"),
            ("top_p", "top_p"),
        ):
            if payload.get(source_key) is not None and target_key not in kwargs:
                kwargs[target_key] = payload[source_key]
        if payload.get("stop") is not None:
            kwargs["stop"] = payload["stop"]

        return kwargs

    # ------------------------------------------------------------------
    # T11 entry-path audit (gateway choke points)
    #
    # Every gateway entry path resolves the runtime session (T2) and logs
    # usage via ``_record_gateway_request`` -> ``_resolve_runtime_session``,
    # so T2 per-run sessions cover ALL paths uniformly. Verified paths:
    #   - OpenAI chat/completions      (create_chat_completion / stream_*)
    #   - OpenAI responses             (create_response / stream_response)
    #   - Anthropic messages + stream  (create_message / stream_message,
    #     served by this same OpenAIGatewayService)
    #   - Gemini generateContent + stream (GeminiGatewayService delegates to
    #     super().create_response / super().stream_response)
    #
    # Tool attribution (T1) is captured in ``_capture_tools_meta``. Most paths
    # reach it via ``_call_litellm``. EXCEPTION: the OpenAI-Codex provider
    # (provider_name == "openai-codex") bypasses ``_call_litellm`` and calls
    # ``_create_openai_codex_response`` / ``_stream_openai_codex_*`` directly.
    # Those four codex branches now call ``_capture_tools_meta`` themselves so
    # attribution is not lost. Codex requests are not governance-stripped, so
    # every codex tool reports ``stripped=False`` (correct).
    # ------------------------------------------------------------------
    def _call_litellm(
        self,
        ai_model: AIModel,
        *,
        messages: List[Dict[str, Any]],
        payload: Dict[str, Any],
        stream: bool = False,
        provider: GatewayProvider,
    ):
        # Capture the ORIGINAL tools before optimization strips any of them so
        # per-tool attribution covers the request as the client sent it.
        original_tools = payload.get("tools")
        messages, payload = self._optimize_request_context(
            messages=messages, payload=payload
        )
        # Per locked decision D14: compute tools_meta in its own guarded block
        # at the single choke point, outside _optimize_request_context's broad
        # fail-open, and independent of whether an api_key is present (OAuth
        # MCP-token traffic must still get attribution). Failures fail-open but
        # are logged so attribution holes stay visible.
        self._capture_tools_meta(original_tools)
        kwargs = self._build_completion_kwargs(
            ai_model,
            messages=messages,
            payload=payload,
            stream=stream,
            provider=provider,
        )

        try:
            return self.upstream_backend.completion(**kwargs)
        except Exception as exc:
            raise self._normalize_upstream_error(provider, exc) from exc

    def _capture_tools_meta(self, original_tools: Any) -> None:
        """Stash per-tool cost attribution for the usage row.

        Builds ``self._last_tools_meta``: one entry per tool in the original
        (pre-strip) request, recording the tool name, a token estimate of its
        serialized schema, whether governance stripped it, and a heuristic
        ``source`` label. Read into ``meta_data["tools_meta"]`` at log time.

        Args:
            original_tools: The request ``tools`` value before optimization.
        """
        self._last_tools_meta = None
        if not isinstance(original_tools, list) or not original_tools:
            return
        try:
            stripped_names: set[str] = set()
            if self._last_context_optimization is not None:
                stripped_names = set(self._last_context_optimization.stripped_tools)
            tools_meta: List[Dict[str, Any]] = []
            for definition in original_tools:
                name = tool_definition_name(definition)
                if not name:
                    # Malformed tool (no resolvable name): skip it but keep
                    # going so the rest of the request is still attributed.
                    continue
                try:
                    schema_json = json.dumps(definition, default=str, sort_keys=True)
                except (TypeError, ValueError):
                    schema_json = ""
                tools_meta.append(
                    {
                        "name": name,
                        # Heuristic: the request payload carries only tool
                        # names/schemas, never MCP server identity, so we cannot
                        # reliably distinguish mcp-served from inline tools here.
                        # Default to "payload"; true mcp/payload classification
                        # requires the MCP-serving-layer join (deferred).
                        "source": "payload",
                        "schema_tokens_estimate": estimate_tokens(schema_json),
                        "stripped": name in stripped_names,
                    }
                )
            self._last_tools_meta = tools_meta or None
        except Exception:
            self._last_tools_meta = None
            logger.warning(
                "Failed to compute tools_meta attribution; forwarding without it",
                exc_info=True,
            )

    def _optimize_request_context(
        self,
        *,
        messages: List[Dict[str, Any]],
        payload: Dict[str, Any],
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Apply governance-driven context optimization before the upstream call.

        Strips tools disabled via subject governance and runs the configured
        deterministic transforms (dedupe, noise stripping, tool-result cap)
        on tool messages. Measured savings are stashed for usage logging.

        Args:
            messages: Normalized chat messages for the upstream call.
            payload: Original request payload (read for ``tools``).

        Returns:
            Tuple of (messages, payload), replaced with optimized copies only
            when a transform changed something.
        """
        self._last_context_optimization = None
        if not self.auth_context.api_key:
            return messages, payload
        try:
            meta_data = get_cached_account_meta_data(
                self.db, str(self.auth_context.user.account_id)
            )
            if meta_data is None:
                return messages, payload
            subject_context = build_subject_context_from_api_key(
                self.auth_context.api_key
            )
            raw_tools = payload.get("tools")
            has_tools = isinstance(raw_tools, list) and bool(raw_tools)
            if not subject_governance_affects_gateway_context(
                meta_data,
                subject_context=subject_context,
                has_tools=has_tools,
            ):
                return messages, payload
            settings_resolved = resolve_context_optimization_settings(
                meta_data, subject_context=subject_context
            )
            optimized_messages, stats = optimize_messages(messages, settings_resolved)
            optimized_payload = payload
            if has_tools:
                kept_tools, removed_names = strip_disabled_tools(
                    raw_tools,
                    meta_data=meta_data,
                    subject_context=subject_context,
                )
                if removed_names:
                    stats.stripped_tools = removed_names
                    optimized_payload = {**payload, "tools": kept_tools}
                    if not kept_tools:
                        optimized_payload.pop("tools", None)
                        optimized_payload.pop("tool_choice", None)
                    elif "tool_choice" in optimized_payload:
                        # Partial strip: if tool_choice names a removed tool it
                        # would dangle and the upstream rejects the request with
                        # HTTP 400. Fall back to "auto" in that case only.
                        sanitized_choice, choice_changed = sanitize_tool_choice(
                            optimized_payload["tool_choice"],
                            removed_tool_names=set(removed_names),
                        )
                        if choice_changed:
                            optimized_payload["tool_choice"] = sanitized_choice
            if stats.changed:
                self._last_context_optimization = stats
            return optimized_messages, optimized_payload
        except Exception:
            logger.warning(
                "Context optimization pass failed; forwarding request unchanged",
                exc_info=True,
            )
            return messages, payload

    def _normalize_responses_input(
        self, payload: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        instructions = payload.get("instructions")
        if instructions:
            messages.append({"role": "system", "content": instructions})

        raw_input = payload.get("input")
        if isinstance(raw_input, str):
            messages.append({"role": "user", "content": raw_input})
        elif isinstance(raw_input, list):
            messages.extend(self._normalize_responses_input_items(raw_input))

        if not messages:
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=400,
                message="input must be provided",
            )
        return messages

    def _normalize_responses_input_items(
        self, items: List[Any]
    ) -> List[Dict[str, Any]]:
        """Convert Responses API history into valid chat-completions messages."""
        messages: List[Dict[str, Any]] = []
        staged_tool_calls: List[Dict[str, Any]] = []
        pending_tool_call_ids: set[str] = set()

        def tool_response_error() -> ModelGatewayAPIError:
            missing_ids_set = pending_tool_call_ids or {
                str(tool_call.get("id"))
                for tool_call in staged_tool_calls
                if tool_call.get("id")
            }
            missing_ids = ", ".join(sorted(missing_ids_set))
            return ModelGatewayAPIError(
                provider="openai",
                status_code=400,
                message=(
                    "An assistant message with 'tool_calls' must be followed by "
                    "tool messages responding to each 'tool_call_id'. "
                    f"The following tool_call_ids did not have response messages: {missing_ids}"
                ),
            )

        def flush_staged_tool_calls() -> None:
            nonlocal staged_tool_calls, pending_tool_call_ids
            if not staged_tool_calls:
                return
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": staged_tool_calls,
                }
            )
            pending_tool_call_ids = {tool_call["id"] for tool_call in staged_tool_calls}
            staged_tool_calls = []

        for item in items:
            if not isinstance(item, dict):
                continue

            item_type = item.get("type")
            if item_type == "function_call":
                if pending_tool_call_ids:
                    raise tool_response_error()
                normalized_tool_call = self._normalize_responses_tool_call_item(item)
                if normalized_tool_call:
                    staged_tool_calls.append(normalized_tool_call)
                continue

            if item_type == "function_call_output":
                flush_staged_tool_calls()
                call_id = item.get("call_id")
                if not call_id or call_id not in pending_tool_call_ids:
                    raise tool_response_error()
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": self._content_to_text(item.get("output", "")),
                    }
                )
                pending_tool_call_ids.discard(call_id)
                continue

            if staged_tool_calls or pending_tool_call_ids:
                raise tool_response_error()

            messages.extend(self._normalize_responses_message_item(item))

        flush_staged_tool_calls()
        if pending_tool_call_ids:
            raise tool_response_error()
        if staged_tool_calls:
            pending_tool_call_ids = {tool_call["id"] for tool_call in staged_tool_calls}
            raise tool_response_error()
        return messages

    def _normalize_responses_tool_call_item(
        self, item: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Convert one Responses API function call into chat tool_call format."""
        function_name = item.get("name")
        call_id = item.get("call_id")
        if not function_name or not call_id:
            return None
        return {
            "id": call_id,
            "type": "function",
            "function": {
                "name": function_name,
                "arguments": item.get("arguments") or "",
            },
        }

    def _normalize_responses_message_item(
        self, item: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Convert one non-tool Responses item into chat-completions messages."""
        role = item.get("role", "user")
        content = item.get("content", "")
        return [{"role": role, "content": self._content_to_text(content)}]

    def _normalize_anthropic_messages_input(
        self, payload: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Normalize Anthropic messages input to the internal chat format."""
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ModelGatewayAPIError(
                provider="anthropic",
                status_code=400,
                message="messages must be a non-empty list",
            )

        messages: List[Dict[str, Any]] = []
        system_prompt = payload.get("system")
        if system_prompt:
            messages.append(
                {"role": "system", "content": self._content_to_text(system_prompt)}
            )

        for item in raw_messages:
            if not isinstance(item, dict):
                continue
            role = item.get("role", "user")
            content = item.get("content", "")

            if isinstance(content, str):
                messages.append({"role": role, "content": content})
                continue

            if isinstance(content, list):
                text_parts = []
                tool_calls = []
                has_tools = False

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type", "")
                    if block_type in ("text", "input_text", "output_text"):
                        text_val = block.get("text")
                        if isinstance(text_val, str):
                            text_parts.append(text_val)
                    elif block_type == "tool_use":
                        has_tools = True
                        input_val = block.get("input", {})
                        if isinstance(input_val, dict):
                            input_str = json.dumps(input_val)
                        elif isinstance(input_val, str):
                            try:
                                import ast

                                parsed = ast.literal_eval(input_val)
                                if isinstance(parsed, dict):
                                    input_str = json.dumps(parsed)
                                else:
                                    input_str = input_val
                            except Exception:
                                input_str = input_val
                        else:
                            input_str = "{}"

                        tool_calls.append(
                            {
                                "id": block.get("id") or f"call_{int(time.time())}",
                                "type": "function",
                                "function": {
                                    "name": block.get("name", "unknown_tool"),
                                    "arguments": input_str,
                                },
                            }
                        )
                    elif block_type == "tool_result":
                        has_tools = True
                        tool_content = block.get("content", "")
                        tool_content_str = (
                            self._content_to_text(tool_content)
                            if not isinstance(tool_content, str)
                            else tool_content
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": block.get("tool_use_id", "unknown_id"),
                                "name": "tool",
                                "content": tool_content_str,
                            }
                        )

                msg_content = "\n".join(text_parts) if text_parts else ""
                msg: Dict[str, Any] = {"role": role, "content": msg_content}
                if tool_calls:
                    msg["tool_calls"] = tool_calls

                # We append if it's an assistant message, or if it has text/tool_calls, or if it was empty block
                if msg_content or tool_calls or role == "assistant" or not has_tools:
                    messages.append(msg)

        if len(messages) == 0:
            raise ModelGatewayAPIError(
                provider="anthropic",
                status_code=400,
                message="messages must be a non-empty list",
            )
        return messages

    @staticmethod
    def _normalize_upstream_error(
        provider: GatewayProvider, exc: Exception
    ) -> ModelGatewayAPIError:
        status_code = (
            getattr(exc, "status_code", None) or getattr(exc, "status", None) or 502
        )
        try:
            status_code = int(status_code)
        except (TypeError, ValueError):
            status_code = 502

        if status_code < 400 or status_code > 599:
            status_code = 502

        message = (
            getattr(exc, "message", None)
            or getattr(exc, "detail", None)
            or str(exc)
            or "Gateway upstream error"
        )
        if status_code >= 500 and not getattr(exc, "status_code", None):
            message = f"Gateway upstream error: {message}"

        error_type = getattr(exc, "type", None) or getattr(exc, "error_type", None)
        code = getattr(exc, "code", None)

        if status_code >= 500:
            try:
                from preloop.sync.tasks import notify_admins

                notify_admins(
                    subject=f"[Preloop Alert] AI Gateway HTTP {status_code} Error ({provider})",
                    message=f"The AI Gateway experienced an upstream or timeout failure.\n\nProvider: {provider}\nStatus: {status_code}\nMessage: {message}\nType: {error_type}\nCode: {code}\n\nTrace:\n{str(exc)}",
                )
            except Exception:
                # Admin alert is best-effort; never block error mapping.
                pass

        return ModelGatewayAPIError(
            provider=provider,
            status_code=status_code,
            message=message,
            error_type=str(error_type) if error_type is not None else None,
            code=str(code) if code is not None else None,
        )

    @staticmethod
    def _to_litellm_model(ai_model: AIModel) -> str:
        return to_litellm_model(ai_model)

    @staticmethod
    def _response_to_dict(response: Any) -> Dict[str, Any]:
        if hasattr(response, "model_dump"):
            return response.model_dump()
        if isinstance(response, dict):
            return response
        return dict(response)

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in {
                    "input_text",
                    "text",
                    "output_text",
                }:
                    text_value = item.get("text")
                    if isinstance(text_value, str):
                        texts.append(text_value)
            return "\n".join(filter(None, texts))
        return str(content)

    def _extract_assistant_text(self, response_dict: Dict[str, Any]) -> str:
        choices = response_dict.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content", "")
            return self._content_to_text(content)
        return ""

    def _extract_tool_calls(
        self, response_dict: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        choices = response_dict.get("choices") or []
        if not choices:
            return []
        message = choices[0].get("message") or {}
        tool_calls = message.get("tool_calls") or []
        return [tool_call for tool_call in tool_calls if isinstance(tool_call, dict)]

    def _extract_stream_delta_text(self, response_dict: Dict[str, Any]) -> str:
        """Extract text delta from a streamed chunk."""
        choices = response_dict.get("choices") or []
        if not choices:
            return ""
        delta = choices[0].get("delta") or {}
        content = delta.get("content", "")
        return self._content_to_text(content)

    def _extract_stream_tool_call_deltas(
        self, response_dict: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Extract streamed tool call deltas from one chunk."""
        choices = response_dict.get("choices") or []
        if not choices:
            return []
        delta = choices[0].get("delta") or {}
        tool_calls = delta.get("tool_calls") or []
        return [tool_call for tool_call in tool_calls if isinstance(tool_call, dict)]

    def _build_response_output_items(
        self, response_dict: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Build Responses API output items from one chat-completions payload."""
        output_items: List[Dict[str, Any]] = []
        assistant_text = self._extract_assistant_text(response_dict)
        if assistant_text:
            output_items.append(
                {
                    "id": response_dict.get("id", f"msg_{int(time.time())}"),
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": assistant_text}],
                }
            )
        for index, tool_call in enumerate(self._extract_tool_calls(response_dict)):
            function_payload = tool_call.get("function") or {}
            call_id = tool_call.get("id") or f"call_{index}"
            output_items.append(
                {
                    "id": f"fc_{call_id}",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": call_id,
                    "name": function_payload.get("name", ""),
                    "arguments": function_payload.get("arguments", ""),
                }
            )
        if output_items:
            return output_items

        # No chat-completions `choices` were present. Some upstreams (notably
        # the Codex ChatGPT-OAuth backend) speak the Responses API natively and
        # return their reply under `output`/`output_text` instead. Pass those
        # `output` items through so clients that read the `output` array (Codex
        # reads `output`, NOT `output_text`) actually see the assistant reply.
        upstream_output = response_dict.get("output")
        if isinstance(upstream_output, list) and upstream_output:
            return upstream_output

        # Last resort: synthesize a single assistant message from `output_text`
        # so the `output` array is never empty when there IS assistant text.
        fallback_text = str(response_dict.get("output_text") or "").strip()
        if fallback_text:
            output_items.append(
                {
                    "id": response_dict.get("id", f"msg_{int(time.time())}"),
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": fallback_text}],
                }
            )
        return output_items

    @staticmethod
    def _response_output_text(output_items: List[Dict[str, Any]]) -> str:
        """Return the concatenated assistant text from response output items."""
        text_parts: List[str] = []
        for item in output_items:
            if item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") == "output_text":
                    text_parts.append(content.get("text", ""))
        return "".join(text_parts)

    @staticmethod
    def _normalize_openai_tools(tools: Any) -> Any:
        """Normalize Responses API tools to chat-completions tool format."""
        if not isinstance(tools, list):
            return tools
        normalized_tools = []
        for tool in tools:
            if not isinstance(tool, dict):
                normalized_tools.append(tool)
                continue
            tool_type = tool.get("type")
            if tool_type in {
                "web_search",
                "web_search_preview",
                "file_search",
                "code_interpreter",
                "computer_use_preview",
            }:
                # Hosted Responses tools are not supported by the LiteLLM
                # compatibility path used by the gateway today.
                continue
            if tool_type == "custom" and not isinstance(tool.get("custom"), dict):
                custom_payload = {
                    key: value for key, value in tool.items() if key not in {"type"}
                }
                normalized_tools.append(
                    {
                        "type": "custom",
                        "custom": OpenAIGatewayService._normalize_custom_tool_payload(
                            custom_payload
                        ),
                    }
                )
                continue
            if tool_type != "function":
                normalized_tools.append(tool)
                continue
            function_name = tool.get("name")
            if not function_name:
                normalized_tools.append(tool)
                continue
            normalized_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": function_name,
                        "description": tool.get("description"),
                        "parameters": tool.get("parameters") or {"type": "object"},
                    },
                }
            )
        return normalized_tools

    @staticmethod
    def _normalize_anthropic_tools(tools: Any) -> Any:
        """Normalize Anthropic tool declarations to chat-completions format."""
        if not isinstance(tools, list):
            return tools
        normalized_tools = []
        for tool in tools:
            if not isinstance(tool, dict):
                normalized_tools.append(tool)
                continue
            if tool.get("type") == "function" and isinstance(
                tool.get("function"), dict
            ):
                normalized_tools.append(tool)
                continue
            function_name = tool.get("name")
            if not function_name:
                normalized_tools.append(tool)
                continue
            normalized_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": function_name,
                        "description": tool.get("description"),
                        "parameters": tool.get("input_schema") or {"type": "object"},
                    },
                }
            )
        return normalized_tools

    @staticmethod
    def _normalize_custom_tool_payload(
        custom_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Normalize flat custom tool payloads for LiteLLM/OpenAI compatibility."""
        normalized_payload = dict(custom_payload)
        custom_format = normalized_payload.get("format")
        if isinstance(custom_format, dict) and custom_format.get("type") == "grammar":
            grammar_payload = custom_format.get("grammar")
            if isinstance(grammar_payload, dict):
                normalized_grammar = dict(grammar_payload)
            else:
                normalized_grammar = {}

            if normalized_grammar.get("syntax") is None and custom_format.get("syntax"):
                normalized_grammar["syntax"] = custom_format["syntax"]
            if (
                normalized_grammar.get("definition") is None
                and custom_format.get("definition") is not None
            ):
                normalized_grammar["definition"] = custom_format["definition"]
            if normalized_grammar.get("definition") is None and isinstance(
                grammar_payload, str
            ):
                normalized_grammar["definition"] = grammar_payload

            normalized_payload["format"] = {
                "type": "grammar",
                "grammar": normalized_grammar,
            }
        return normalized_payload

    @staticmethod
    def _normalize_openai_tool_choice(tool_choice: Any) -> Any:
        """Normalize Responses API tool_choice to chat-completions format."""
        if not isinstance(tool_choice, dict) or tool_choice.get("type") != "function":
            return tool_choice
        function_name = tool_choice.get("name")
        if not function_name:
            return tool_choice
        return {
            "type": "function",
            "function": {"name": function_name},
        }

    @staticmethod
    def _extract_finish_reason(response_dict: Dict[str, Any]) -> Optional[str]:
        choices = response_dict.get("choices") or []
        if not choices:
            return None
        return choices[0].get("finish_reason")

    @staticmethod
    def _normalize_usage(
        usage: Optional[Dict[str, Any]],
        *,
        prompt_key: str,
        completion_key: str,
        output_names: tuple[str, ...] = ("completion_tokens",),
    ) -> Dict[str, Any]:
        usage = usage or {}
        prompt_tokens = int(usage.get(prompt_key, usage.get("input_tokens", 0)) or 0)
        completion_tokens = 0
        for key in output_names:
            if usage.get(key) is not None:
                completion_tokens = int(usage.get(key) or 0)
                break
        total_tokens = int(
            usage.get("total_tokens", prompt_tokens + completion_tokens) or 0
        )
        normalized: Dict[str, Any] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }
        # Preserve the provider cache-token breakdown so cost stays cache-aware
        # when recomputed from stored usage and so the cached split can be shown
        # in the UI. OpenAI nests cached input under
        # ``prompt_tokens_details.cached_tokens``; Anthropic reports
        # ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` at the
        # top level. ``estimate_ai_model_usage_cost`` already reads these.
        for details_key in ("prompt_tokens_details", "completion_tokens_details"):
            details = usage.get(details_key)
            if isinstance(details, dict):
                kept = {k: v for k, v in details.items() if v is not None}
                if kept:
                    normalized[details_key] = kept
        for cache_key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            if usage.get(cache_key) is not None:
                normalized[cache_key] = int(usage.get(cache_key) or 0)
        return normalized

    @staticmethod
    def _merge_usage_dicts(
        base: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Merge streaming usage payloads without losing earlier detail.

        Providers may report usage across several chunks (e.g. Anthropic input
        tokens on message_start, output tokens on message_delta). A later
        sparse payload must not clobber earlier richer fields: a key from
        ``new`` wins only when it is non-null and, for numbers, non-zero
        (unless the base value is missing/zero). Nested dicts merge
        recursively.

        Args:
            base: Previously accumulated usage (may be None/empty).
            new: Usage payload from the latest chunk (may be None/empty).

        Returns:
            The merged usage dict (a new dict; inputs are not mutated).
        """
        merged: Dict[str, Any] = dict(base or {})
        for key, value in (new or {}).items():
            if value is None:
                continue
            existing = merged.get(key)
            if isinstance(value, dict):
                merged[key] = OpenAIGatewayService._merge_usage_dicts(
                    existing if isinstance(existing, dict) else {}, value
                )
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                if value != 0 or not existing:
                    merged[key] = value
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _extract_token_details(
        usage_details: Optional[Dict[str, Any]],
    ) -> Dict[str, Optional[int]]:
        """Extract cache/reasoning token counts from a provider usage payload.

        Unifies the OpenAI shape (``prompt_tokens_details.cached_tokens`` /
        ``cache_creation_tokens``, ``completion_tokens_details.reasoning_tokens``)
        and the Anthropic shape (top-level ``cache_read_input_tokens`` /
        ``cache_creation_input_tokens``).

        Args:
            usage_details: Raw provider usage dict, possibly empty.

        Returns:
            Dict with ``cache_read_tokens``, ``cache_creation_tokens``, and
            ``reasoning_tokens`` (None when the provider reported nothing).
        """
        usage_details = usage_details or {}
        prompt_details = usage_details.get("prompt_tokens_details")
        prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
        completion_details = usage_details.get("completion_tokens_details")
        completion_details = (
            completion_details if isinstance(completion_details, dict) else {}
        )

        def _first_int(*values: Any) -> Optional[int]:
            for value in values:
                if value is not None:
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        continue
            return None

        return {
            "cache_read_tokens": _first_int(
                prompt_details.get("cached_tokens"),
                usage_details.get("cache_read_input_tokens"),
            ),
            "cache_creation_tokens": _first_int(
                prompt_details.get("cache_creation_tokens"),
                usage_details.get("cache_creation_input_tokens"),
            ),
            "reasoning_tokens": _first_int(
                completion_details.get("reasoning_tokens"),
            ),
        }

    def _estimate_usage_fallback(
        self,
        *,
        ai_model: AIModel,
        request_payload: Optional[Dict[str, Any]],
        output_text: Optional[str],
    ) -> Optional[tuple[int, int]]:
        """Estimate token usage locally when the provider reported none.

        Uses litellm's tokenizer over the request messages and accumulated
        output text. Only a fallback: provider-reported usage always wins.

        Args:
            ai_model: The model the request was routed to.
            request_payload: Original request body (messages/input extracted).
            output_text: Accumulated assistant text, if any.

        Returns:
            ``(prompt_tokens, completion_tokens)`` or None when nothing could
            be estimated.
        """
        messages = (request_payload or {}).get("messages")
        if not isinstance(messages, list) or not messages:
            raw_input = (request_payload or {}).get("input")
            if isinstance(raw_input, str) and raw_input:
                messages = [{"role": "user", "content": raw_input}]
            elif isinstance(raw_input, list) and raw_input:
                messages = [
                    item
                    for item in raw_input
                    if isinstance(item, dict) and item.get("role")
                ]
            else:
                messages = None

        candidates = list(_iter_litellm_model_candidates(ai_model)) or [
            ai_model.model_identifier
        ]
        prompt_tokens = 0
        if messages:
            for candidate in candidates:
                try:
                    prompt_tokens = int(
                        litellm.token_counter(model=candidate, messages=messages)
                    )
                    break
                except Exception:  # noqa: BLE001 - tokenizer/model lookup issues
                    continue
            if not prompt_tokens:
                # Last resort: char/4 heuristic over the serialized messages.
                try:
                    serialized = json.dumps(messages, default=str)
                    prompt_tokens = max(len(serialized) // 4, 1)
                except (TypeError, ValueError):
                    prompt_tokens = 0

        completion_tokens = 0
        if output_text:
            for candidate in candidates:
                try:
                    completion_tokens = int(
                        litellm.token_counter(
                            model=candidate,
                            text=output_text,
                            count_response_tokens=True,
                        )
                    )
                    break
                except Exception:  # noqa: BLE001
                    continue
            if not completion_tokens:
                completion_tokens = max(len(output_text) // 4, 1)

        if not prompt_tokens and not completion_tokens:
            return None
        return prompt_tokens, completion_tokens

    def _pricing_override_for_request(
        self, *, ai_model: AIModel, model_alias: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Resolve account-scoped pricing metadata for a gateway usage row."""
        return resolve_pricing_override(
            self.db,
            account_id=self.auth_context.user.account_id,
            ai_model=ai_model,
            requested_alias=model_alias,
        )

    def _record_stream_abort(
        self,
        *,
        endpoint: str,
        endpoint_kind: str,
        started_at: float,
        ai_model: AIModel,
        payload: Dict[str, Any],
        usage_details: Optional[Dict[str, Any]],
        budget_result: Optional[BudgetCheckResult],
        accumulated_output_text: Optional[str] = None,
    ) -> None:
        """Best-effort usage record when a streaming client disconnects early.

        Called from a ``finally`` during GeneratorExit, so it must never raise —
        a failure here would replace a clean disconnect with an error. Status
        499 ("client closed request") flags the partial record. Usage captured
        so far is recorded as ``partial``; when no usage chunk arrived yet the
        record path falls back to a local token estimate over the request and
        accumulated output text.
        """
        has_partial_usage = isinstance(usage_details, dict) and any(
            usage_details.get(key)
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "input_tokens",
                "output_tokens",
                "total_tokens",
            )
        )
        try:
            self._record_gateway_request(
                endpoint=endpoint,
                method="POST",
                status_code=499,
                duration=time.perf_counter() - started_at,
                ai_model=ai_model,
                requested_model=payload.get("model"),
                response_payload=None,
                upstream_response={"usage": usage_details}
                if isinstance(usage_details, dict)
                else None,
                endpoint_kind=endpoint_kind,
                budget_result=budget_result,
                error_detail="client disconnected before stream completion",
                request_payload=payload,
                usage_source="partial" if has_partial_usage else None,
                accumulated_output_text=accumulated_output_text,
            )
        except Exception:  # pragma: no cover - defensive; never break teardown
            logger.warning(
                "Failed to record gateway usage after client disconnect",
                exc_info=True,
            )

    def _record_gateway_request(
        self,
        *,
        endpoint: str,
        method: str,
        status_code: int,
        duration: float,
        ai_model: AIModel,
        requested_model: Optional[str],
        response_payload: Optional[Dict[str, Any]],
        upstream_response: Optional[Dict[str, Any]],
        endpoint_kind: str,
        budget_result: Optional[BudgetCheckResult] = None,
        error_detail: Optional[str] = None,
        request_payload: Optional[Dict[str, Any]] = None,
        usage_source: Optional[str] = None,
        accumulated_output_text: Optional[str] = None,
    ) -> None:
        """Persist one usage fact for a gateway request."""
        runtime = resolve_ai_model_runtime(ai_model)
        usage = response_payload.get("usage") if response_payload else {}
        usage_details = (
            upstream_response.get("usage")
            if upstream_response and isinstance(upstream_response.get("usage"), dict)
            else usage
            if isinstance(usage, dict)
            else {}
        )
        if not isinstance(usage, dict):
            usage = {}
        # Prefer the client-facing usage payload, but fall back to the raw
        # upstream usage so records without a response body (e.g. client
        # disconnects) still carry the tokens captured before the abort.
        prompt_tokens = (
            usage.get("prompt_tokens")
            or usage.get("input_tokens")
            or usage_details.get("prompt_tokens")
            or usage_details.get("input_tokens")
            or 0
        )
        completion_tokens = (
            usage.get("completion_tokens")
            or usage.get("output_tokens")
            or usage_details.get("completion_tokens")
            or usage_details.get("output_tokens")
            or 0
        )
        total_tokens = usage.get("total_tokens") or usage_details.get("total_tokens")
        if not total_tokens and (prompt_tokens or completion_tokens):
            total_tokens = prompt_tokens + completion_tokens
        token_details = self._extract_token_details(usage_details)
        usage_estimated = False
        if not prompt_tokens and not completion_tokens and status_code in (200, 499):
            fallback = self._estimate_usage_fallback(
                ai_model=ai_model,
                request_payload=request_payload,
                output_text=accumulated_output_text,
            )
            if fallback:
                prompt_tokens, completion_tokens = fallback
                total_tokens = prompt_tokens + completion_tokens
                usage_source = "estimated"
                usage_estimated = True
        if usage_source is None and (prompt_tokens or completion_tokens):
            usage_source = "provider"

        runtime_context = (
            (self.auth_context.api_key.context_data or {})
            if self.auth_context.api_key
            else {}
        )
        runtime_principal = runtime_context.get("runtime_principal") or {}
        runtime_session_id = self._resolve_runtime_session()

        model_alias = runtime.model_gateway_model_alias or requested_model
        request_fingerprint = self._gateway_request_fingerprint(
            endpoint_kind=endpoint_kind,
            model_alias=model_alias,
            request_payload=request_payload,
        )
        attempt_summary = crud_api_usage.get_gateway_attempt_summary(
            self.db,
            account_id=str(self.auth_context.user.account_id),
            runtime_session_id=runtime_session_id,
            request_fingerprint=request_fingerprint,
        )
        previous_attempt_count = int(attempt_summary.get("count") or 0)
        gateway_attempt = previous_attempt_count + 1
        is_retry = previous_attempt_count > 0
        retry_of_api_usage_id = (
            attempt_summary.get("first_api_usage_id") if is_retry else None
        )
        pricing_override = self._pricing_override_for_request(
            ai_model=ai_model,
            model_alias=model_alias,
        )
        cost_estimate = estimate_ai_model_usage_cost_detailed(
            ai_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens or 0,
            usage_details=usage_details,
            pricing_override=pricing_override,
        )
        estimated_cost = cost_estimate.cost
        cost_source = cost_estimate.source
        api_equivalent_cost: Optional[float] = None
        if self._last_upstream_credential_type == "oauth" and not pricing_override:
            # Subscription-covered upstream (Claude Code Max / ChatGPT OAuth):
            # the call has no marginal API charge. Record $0 spend but keep
            # the API-equivalent value so analytics can show what the
            # subscription absorbed. An explicit price override still wins
            # (e.g. operators amortizing a subscription across usage).
            api_equivalent_cost = estimated_cost
            estimated_cost = 0.0
            cost_source = "subscription"
        usage_row = crud_api_usage.log_gateway_request(
            self.db,
            endpoint=endpoint,
            method=method,
            status_code=status_code,
            duration=duration,
            user_id=str(self.auth_context.user.id),
            account_id=str(self.auth_context.user.account_id),
            api_key_id=(
                str(self.auth_context.api_key.id) if self.auth_context.api_key else None
            ),
            auth_subject_type=(
                "api_key"
                if self.auth_context.api_key
                else "oauth_mcp_token"
                if self.auth_context.oauth_access_token
                else "user_token"
            ),
            ai_model_id=str(ai_model.id),
            flow_id=runtime_context.get("flow_id"),
            flow_execution_id=runtime_context.get("flow_execution_id"),
            runtime_session_id=runtime_session_id,
            managed_agent_id=self._resolve_managed_agent_id(),
            model_alias=model_alias,
            provider_name=ai_model.provider_name,
            upstream_request_id=(
                upstream_response.get("id") if upstream_response else None
            ),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cache_read_tokens=token_details["cache_read_tokens"],
            cache_creation_tokens=token_details["cache_creation_tokens"],
            reasoning_tokens=token_details["reasoning_tokens"],
            estimated_cost=estimated_cost,
            cost_source=cost_source,
            usage_source=usage_source,
            is_retry=is_retry,
            runtime_principal_type=runtime_principal.get("type"),
            runtime_principal_id=runtime_principal.get("id"),
            runtime_principal_name=runtime_principal.get("name"),
            meta_data={
                "endpoint_kind": endpoint_kind,
                "requested_model": requested_model,
                "gateway_provider": runtime.model_gateway_provider,
                "error_detail": error_detail,
                "budget": self._budget_meta_data(budget_result),
                "finish_reason": self._extract_finish_reason(upstream_response or {})
                if upstream_response
                else None,
                "usage_details": usage_details or None,
                "pricing_override_id": pricing_override.get("id")
                if pricing_override
                else None,
                "request_fingerprint": request_fingerprint,
                "gateway_attempt": gateway_attempt,
                "is_retry": is_retry,
                "retry_of_api_usage_id": retry_of_api_usage_id,
                "usage_estimated": usage_estimated or None,
                "api_equivalent_cost": api_equivalent_cost,
                "context_optimization": (
                    self._last_context_optimization.to_meta()
                    if self._last_context_optimization
                    else None
                ),
                "tools_meta": self._last_tools_meta,
                "upstream_credential_type": self._last_upstream_credential_type,
                "purpose": ((request_payload or {}).get("metadata") or {}).get(
                    "purpose"
                ),
            },
        )
        observed_at = usage_row.timestamp

        if cost_source == "unpriced" and (prompt_tokens or completion_tokens):
            # The model is missing from the price snapshot: fetch its price
            # from the live upstream map once (background thread, negative-
            # cached) and fix this row when found.
            try:
                schedule_price_lookup(ai_model=ai_model, api_usage_id=str(usage_row.id))
            except Exception:  # noqa: BLE001 - never break recording
                logger.debug("Scheduling live price lookup failed", exc_info=True)

        log_model_gateway_request(
            self.db,
            account_id=self.auth_context.user.account_id,
            user_id=self.auth_context.user.id,
            api_usage_id=str(usage_row.id),
            endpoint=endpoint,
            endpoint_kind=endpoint_kind,
            status_code=status_code,
            outcome=(
                "success"
                if status_code < 400
                else self._audit_outcome(status_code, error_detail)
            ),
            requested_model=requested_model,
            model_alias=runtime.model_gateway_model_alias or requested_model,
            provider_name=ai_model.provider_name,
            gateway_provider=runtime.model_gateway_provider,
            auth_subject_type=usage_row.auth_subject_type,
            runtime_session_id=(
                str(usage_row.runtime_session_id)
                if usage_row.runtime_session_id
                else None
            ),
            runtime_principal_type=usage_row.runtime_principal_type,
            runtime_principal_id=usage_row.runtime_principal_id,
            runtime_principal_name=usage_row.runtime_principal_name,
            api_key_id=(
                str(self.auth_context.api_key.id) if self.auth_context.api_key else None
            ),
            api_key_name=self.auth_context.api_key.name
            if self.auth_context.api_key
            else None,
            flow_id=str(usage_row.flow_id) if usage_row.flow_id else None,
            flow_execution_id=(
                str(usage_row.flow_execution_id)
                if usage_row.flow_execution_id
                else None
            ),
            upstream_request_id=usage_row.upstream_request_id,
            request_fingerprint=request_fingerprint,
            gateway_attempt=gateway_attempt,
            is_retry=is_retry,
            retry_of_api_usage_id=retry_of_api_usage_id,
            error_detail=error_detail,
            error_type=(
                self._audit_error_type(status_code, error_detail)
                if status_code >= 400
                else None
            ),
            budget=self._budget_meta_data(budget_result),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost=float(usage_row.estimated_cost or 0.0),
        )
        ModelGatewayEventEmitter(self.db).emit_for_usage(
            usage=usage_row,
            request_payload=request_payload,
            response_payload=response_payload,
        )
        try:
            GatewayUsageSearchService(self.db).auto_index_interaction(
                usage=usage_row,
                request_payload=request_payload,
                response_payload=response_payload,
            )
        except Exception:
            logger.exception(
                "Automatic gateway interaction indexing failed for usage %s",
                usage_row.id,
            )
        try:
            runtime_session = None
            if usage_row.runtime_session_id:
                runtime_session = crud_runtime_session.touch_activity(
                    self.db,
                    account_id=self.auth_context.user.account_id,
                    runtime_session_id=usage_row.runtime_session_id,
                    observed_at=observed_at,
                    min_update_interval=_RUNTIME_SESSION_ACTIVITY_TOUCH_MIN_INTERVAL,
                    commit=True,
                )
                if runtime_session is not None:
                    self._maybe_refresh_runtime_session_summary(
                        runtime_session=runtime_session,
                        usage=usage_row,
                        request_payload=request_payload,
                        response_payload=response_payload,
                        observed_at=observed_at,
                    )
                    emit_account_event(
                        build_account_event(
                            account_id=str(self.auth_context.user.account_id),
                            topic=ACCOUNT_TOPIC_RUNTIME_SESSIONS,
                            event_type="runtime_session_updated",
                            payload={
                                "runtime_session_id": str(runtime_session.id),
                                "session_source_type": runtime_session.session_source_type,
                                "session_source_id": runtime_session.session_source_id,
                                "session_reference": runtime_session.session_reference,
                                "runtime_principal_type": runtime_session.runtime_principal_type,
                                "runtime_principal_id": runtime_session.runtime_principal_id,
                                "runtime_principal_name": runtime_session.runtime_principal_name,
                                "last_activity_at": runtime_session.last_activity_at.isoformat()
                                if runtime_session.last_activity_at
                                else None,
                                "last_request_at": observed_at.isoformat(),
                                "ended_at": runtime_session.ended_at.isoformat()
                                if runtime_session.ended_at
                                else None,
                            },
                            runtime_session_id=str(runtime_session.id),
                            execution_id=str(usage_row.flow_execution_id)
                            if usage_row.flow_execution_id
                            else None,
                            flow_id=str(usage_row.flow_id)
                            if usage_row.flow_id
                            else None,
                        )
                    )

            managed_agent = None
            if usage_row.runtime_principal_type and usage_row.runtime_principal_id:
                managed_agent = crud_managed_agent.touch_last_seen_for_principal(
                    self.db,
                    account_id=self.auth_context.user.account_id,
                    session_source_type=usage_row.runtime_principal_type,
                    session_source_id=usage_row.runtime_principal_id,
                    runtime_session_id=usage_row.runtime_session_id,
                    observed_at=observed_at,
                    commit=True,
                )
                if managed_agent is not None:
                    emit_account_event(
                        build_account_event(
                            account_id=str(self.auth_context.user.account_id),
                            topic=ACCOUNT_TOPIC_MANAGED_AGENTS,
                            event_type="managed_agent_updated",
                            payload={
                                "agent_id": str(managed_agent.id),
                                "runtime_session_id": str(
                                    managed_agent.runtime_session_id
                                )
                                if managed_agent.runtime_session_id
                                else None,
                                "display_name": managed_agent.display_name,
                                "session_source_type": managed_agent.session_source_type,
                                "session_source_id": managed_agent.session_source_id,
                                "session_reference": managed_agent.session_reference,
                                "last_seen_at": managed_agent.last_seen_at.isoformat(),
                            },
                            runtime_session_id=str(usage_row.runtime_session_id)
                            if usage_row.runtime_session_id
                            else None,
                            execution_id=str(usage_row.flow_execution_id)
                            if usage_row.flow_execution_id
                            else None,
                            flow_id=str(usage_row.flow_id)
                            if usage_row.flow_id
                            else None,
                        )
                    )
        except SQLAlchemyError:
            self.db.rollback()
            logger.warning(
                "Skipping gateway activity touch after usage %s was recorded",
                usage_row.id,
                exc_info=True,
            )

    def _maybe_refresh_runtime_session_summary(
        self,
        *,
        runtime_session: Any,
        usage: Any,
        request_payload: Optional[Dict[str, Any]],
        response_payload: Optional[Dict[str, Any]],
        observed_at: datetime,
    ) -> None:
        """Refresh the persisted session summary on first request, then occasionally."""
        if not self._runtime_session_summary_columns_available():
            return
        summary_state = self._runtime_session_summary_state(runtime_session.id)
        if summary_state is None:
            return
        existing_summary = summary_state.get("summary")
        request_count = (
            crud_runtime_session_activity.count_model_gateway_calls_for_session(
                self.db,
                account_id=self.auth_context.user.account_id,
                runtime_session_id=runtime_session.id,
            )
        )
        if existing_summary and not isinstance(request_count, int):
            return
        if existing_summary and (
            request_count < 1
            or request_count % _RUNTIME_SESSION_SUMMARY_REFRESH_EVERY_REQUESTS != 0
        ):
            return

        default_model = crud_ai_model.get_default_active_model(
            self.db,
            account_id=self.auth_context.user.account_id,
        )
        if default_model is None:
            return

        try:
            summary = self._generate_runtime_session_summary(
                summary_model=default_model,
                existing_summary=existing_summary,
                usage=usage,
                request_payload=request_payload,
                response_payload=response_payload,
                recent_interactions=crud_runtime_session_activity.list_recent_model_gateway_call_payloads_for_session(
                    self.db,
                    account_id=self.auth_context.user.account_id,
                    runtime_session_id=runtime_session.id,
                    limit=_RUNTIME_SESSION_SUMMARY_REFRESH_EVERY_REQUESTS,
                ),
            )
        except Exception:
            logger.info(
                "Skipping runtime session summary refresh for %s",
                runtime_session.id,
                exc_info=True,
            )
            return

        if not summary:
            return

        self.db.execute(
            text(
                "UPDATE runtime_session "
                "SET summary = :summary, summary_updated_at = :summary_updated_at "
                "WHERE id = :runtime_session_id"
            ),
            {
                "summary": summary[:1000],
                "summary_updated_at": observed_at,
                "runtime_session_id": runtime_session.id,
            },
        )
        self.db.commit()

    def _runtime_session_summary_state(
        self, runtime_session_id: Any
    ) -> Optional[Dict[str, Any]]:
        """Fetch persisted summary state without requiring mapped columns."""
        try:
            row = (
                self.db.execute(
                    text(
                        "SELECT summary, summary_updated_at "
                        "FROM runtime_session WHERE id = :runtime_session_id"
                    ),
                    {"runtime_session_id": runtime_session_id},
                )
                .mappings()
                .first()
            )
        except Exception:
            return None
        return dict(row) if row is not None else None

    def _runtime_session_summary_columns_available(self) -> bool:
        """Return whether the runtime session summary migration has been applied."""
        try:
            bind = self.db.get_bind()
            if bind is None:
                return False
            columns = {
                column["name"]
                for column in inspect(bind).get_columns("runtime_session")
            }
        except Exception:
            return False
        return {"summary", "summary_updated_at"}.issubset(columns)

    def _generate_runtime_session_summary(
        self,
        *,
        summary_model: AIModel,
        existing_summary: Optional[str],
        usage: Any,
        request_payload: Optional[Dict[str, Any]],
        response_payload: Optional[Dict[str, Any]],
        recent_interactions: Optional[list[dict[str, Any]]] = None,
    ) -> Optional[str]:
        """Use the account default AI model to produce a compact session summary."""
        context = {
            "existing_summary": existing_summary,
            "latest_request": self._compact_gateway_payload(request_payload),
            "latest_response": self._compact_gateway_payload(response_payload),
            "recent_interactions": [
                self._compact_gateway_event_payload(payload)
                for payload in (recent_interactions or [])
            ],
            "usage": {
                "model": usage.model_alias,
                "provider": usage.provider_name,
                "status_code": usage.status_code,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "estimated_cost": usage.estimated_cost,
            },
        }
        summary_provider: GatewayProvider = (
            "anthropic"
            if (summary_model.provider_name or "").strip().lower() == "anthropic"
            else "openai"
        )
        response = self._call_litellm(
            summary_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Write one concise runtime session summary for a cost "
                        "analytics table. Return only the summary text. Mention "
                        "the agent's apparent goal and latest meaningful work. "
                        "Keep it under 140 characters. Do not include prices."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(context, ensure_ascii=False),
                },
            ],
            payload={"temperature": 0.1, "max_tokens": 80},
            provider=summary_provider,
        )
        content = response.choices[0].message.content if response else None
        return str(content).strip() if content else None

    @classmethod
    def _compact_gateway_event_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Trim persisted gateway metadata down to the content useful for titles."""
        preview = payload.get("conversation_preview")
        messages = []
        if isinstance(preview, dict) and isinstance(preview.get("messages"), list):
            for message in preview["messages"][-4:]:
                if not isinstance(message, dict):
                    continue
                text_value = message.get("text")
                messages.append(
                    {
                        "role": message.get("role") or message.get("source"),
                        "text": str(text_value)[:1200]
                        if text_value is not None
                        else None,
                    }
                )
        return {
            "model": payload.get("model_alias") or payload.get("requested_model"),
            "provider": payload.get("provider_name") or payload.get("gateway_provider"),
            "outcome": payload.get("outcome"),
            "messages": messages,
        }

    @staticmethod
    def _compact_gateway_payload(payload: Optional[Dict[str, Any]]) -> Any:
        if not isinstance(payload, dict):
            return None
        compact: Dict[str, Any] = {}
        for key in ("model", "messages", "input", "output", "output_text"):
            value = payload.get(key)
            if value is None:
                continue
            text = json.dumps(value, ensure_ascii=False, default=str)
            compact[key] = text[:3000]
        return compact

    @staticmethod
    def _gateway_request_fingerprint(
        *,
        endpoint_kind: str,
        model_alias: Optional[str],
        request_payload: Optional[Dict[str, Any]],
    ) -> str:
        """Hash stable request content for retry grouping without storing raw text."""
        payload = dict(request_payload or {})
        payload.pop("stream", None)
        serialized = json.dumps(
            {
                "endpoint_kind": endpoint_kind,
                "model_alias": model_alias,
                "request": payload,
            },
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _check_budget(
        self,
        ai_model: AIModel,
        payload: Dict[str, Any],
        *,
        gateway_provider: GatewayProvider = "openai",
    ) -> Optional[BudgetCheckResult]:
        """Check configured gateway budgets before the upstream call."""
        # Execute plugin budget enforcement (HTTP 403 on limit exceeded)
        if hasattr(self.budget_enforcer, "enforce_or_raise"):
            try:
                self.budget_enforcer.enforce_or_raise(
                    self.db, self.auth_context, ai_model, payload
                )
            except ModelGatewayAPIError as exc:
                raise self._normalize_budget_gateway_error(
                    exc, gateway_provider=gateway_provider
                ) from exc

        return ModelGatewayBudgetService(self.db, self.auth_context).preflight_check(
            ai_model, payload
        )

    @staticmethod
    def _normalize_budget_gateway_error(
        exc: ModelGatewayAPIError,
        *,
        gateway_provider: GatewayProvider,
    ) -> ModelGatewayAPIError:
        """Render budget denials in the client format for the active gateway."""
        message = exc.message
        is_budget_denial = (
            exc.code == "budget_limit_exceeded"
            or "model gateway budget exceeded" in message.lower()
            or "budget hard limit exceeded" in message.lower()
        )
        if not is_budget_denial:
            return exc
        return ModelGatewayAPIError(
            provider=gateway_provider,
            status_code=exc.status_code,
            message=message,
            code="budget_limit_exceeded" if gateway_provider == "openai" else exc.code,
        )

    @staticmethod
    def _budget_meta_data(
        budget_result: Optional[BudgetCheckResult],
    ) -> Optional[Dict[str, Any]]:
        if not budget_result:
            return None
        return {
            "pricing_available": budget_result.pricing_available,
            "estimated_request_cost_usd": budget_result.estimated_request_cost_usd,
            "account_current_spend_usd": budget_result.account_current_spend_usd,
            "account_estimated_total_usd": budget_result.account_estimated_total_usd,
            "account_limit_usd": budget_result.account_limit_usd,
            "account_soft_limit_usd": budget_result.account_soft_limit_usd,
            "flow_current_spend_usd": budget_result.flow_current_spend_usd,
            "flow_estimated_total_usd": budget_result.flow_estimated_total_usd,
            "flow_limit_usd": budget_result.flow_limit_usd,
            "flow_soft_limit_usd": budget_result.flow_soft_limit_usd,
            "trial_hosted_model_limit_usd": budget_result.trial_hosted_model_limit_usd,
            "trial_hosted_model_current_spend_usd": budget_result.trial_hosted_model_current_spend_usd,
            "trial_hosted_model_estimated_total_usd": budget_result.trial_hosted_model_estimated_total_usd,
            "soft_limit_exceeded": budget_result.soft_limit_exceeded,
            "hard_limit_exceeded": budget_result.hard_limit_exceeded,
            "enforcement_reason": budget_result.enforcement_reason,
        }

    @staticmethod
    def _budget_denial_detail(budget_result: BudgetCheckResult) -> str:
        if budget_result.enforcement_reason == "account_budget_exceeded":
            return "Model gateway budget exceeded: account monthly limit reached"
        if budget_result.enforcement_reason == "flow_budget_exceeded":
            return "Model gateway budget exceeded: flow monthly limit reached"
        if budget_result.enforcement_reason == "trial_hosted_model_budget_exceeded":
            return "Model gateway budget exceeded: trial hosted model limit reached"
        if (
            budget_result.enforcement_reason
            == "pricing_required_for_budget_enforcement"
        ):
            return (
                "Model gateway budget enforcement requires pricing information for "
                "the selected gateway model"
            )
        return "Model gateway budget exceeded"

    @staticmethod
    def _audit_outcome(status_code: int, error_detail: Optional[str]) -> str:
        if (
            status_code == 403
            and error_detail
            and (
                "budget exceeded" in error_detail.lower()
                or "budget enforcement requires pricing information"
                in error_detail.lower()
            )
        ):
            return "budget_denied"
        return "failed"

    @staticmethod
    def _audit_error_type(status_code: int, error_detail: Optional[str]) -> str:
        if (
            status_code == 403
            and error_detail
            and (
                "budget exceeded" in error_detail.lower()
                or "budget enforcement requires pricing information"
                in error_detail.lower()
            )
        ):
            return "budget_limit_exceeded"
        if status_code == 400:
            return "validation_error"
        if status_code == 401:
            return "authentication_error"
        if status_code == 403:
            return "permission_error"
        if status_code == 404:
            return "not_found_error"
        if status_code == 429:
            return "rate_limit_error"
        if status_code >= 500:
            return "upstream_error"
        return "gateway_error"

    @staticmethod
    def _to_anthropic_stop_reason(finish_reason: Optional[str]) -> Optional[str]:
        mapping = {
            "stop": "end_turn",
            "length": "max_tokens",
            "content_filter": "stop_sequence",
            "tool_calls": "tool_use",
        }
        return mapping.get(finish_reason or "", "end_turn" if finish_reason else None)

    @staticmethod
    def _build_anthropic_message_payload(
        *,
        response_id: str,
        model_name: Optional[str],
        assistant_text: str,
        stop_reason: Optional[str],
        usage: Dict[str, int],
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        content: List[Dict[str, Any]] = []
        if assistant_text:
            content.append({"type": "text", "text": assistant_text})
        if tool_calls:
            for tc in tool_calls:
                args_raw = tc.get("function", {}).get("arguments", "{}")
                try:
                    args = json.loads(args_raw)
                    # LiteLLM sometimes double stringifies: json.dumps(str(dict))
                    # json.loads un-escapes it into a Python string. We need a dict.
                    if isinstance(args, str):
                        try:
                            import ast

                            parsed = ast.literal_eval(args)
                            if isinstance(parsed, dict):
                                args = parsed
                        except Exception:
                            # Leave args as the raw string when literal_eval fails.
                            pass
                except ValueError:
                    args = {}
                    if isinstance(args_raw, str):
                        try:
                            import ast

                            parsed = ast.literal_eval(args_raw)
                            if isinstance(parsed, dict):
                                args = parsed
                        except Exception:
                            # Keep args empty when the payload is not valid JSON or Python literal.
                            pass
                content.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id"),
                        "name": tc.get("function", {}).get("name", ""),
                        "input": args,
                    }
                )
        if not content:
            content.append({"type": "text", "text": ""})

        return {
            "id": response_id,
            "type": "message",
            "role": "assistant",
            "content": content,
            "model": model_name,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage["prompt_tokens"],
                "output_tokens": usage["completion_tokens"],
            },
        }

    def _normalize_chat_stream_chunk(
        self,
        chunk_dict: Dict[str, Any],
        *,
        model_name: Optional[str],
        response_id: str,
        created_at: int,
    ) -> Dict[str, Any]:
        """Normalize one streamed chat chunk to OpenAI-compatible shape."""
        payload = {
            "id": chunk_dict.get("id", response_id),
            "object": chunk_dict.get("object", "chat.completion.chunk"),
            "created": chunk_dict.get("created", created_at),
            "model": model_name,
            "choices": [],
        }
        for choice in chunk_dict.get("choices") or []:
            delta = choice.get("delta") or {}
            if not delta and choice.get("message"):
                message = choice["message"]
                delta = {"content": self._content_to_text(message.get("content", ""))}
                if message.get("tool_calls"):
                    delta["tool_calls"] = message["tool_calls"]
            payload["choices"].append(
                {
                    "index": choice.get("index", 0),
                    "delta": delta,
                    "finish_reason": choice.get("finish_reason"),
                }
            )
        if chunk_dict.get("usage") is not None:
            payload["usage"] = self._normalize_usage(
                chunk_dict.get("usage"),
                prompt_key="prompt_tokens",
                completion_key="completion_tokens",
            )
        return payload

    @staticmethod
    def _sse_event(payload: Any) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    @staticmethod
    def _sse_done() -> str:
        return "data: [DONE]\n\n"

    @staticmethod
    def _anthropic_sse_event(event_name: str, payload: Any) -> str:
        return (
            f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        )
