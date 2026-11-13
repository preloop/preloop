"""Recheck a rejected model key before spending resources on an execution."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.services.gateway_execution import GatewayModelSnapshot
from preloop.services.model_credential_health import record_gateway_credential_outcome
from preloop.services.model_gateway_auth import ModelGatewayAuthContext, NO_BEARER_TOKEN
from preloop.services.model_gateway_errors import extract_upstream_error_detail
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.upstream_errors import (
    classify_upstream_error,
    classify_recorded_error,
)
from preloop.utils.jsonb_sanitize import sanitize_string
from preloop.utils.secret_scrubbing import scrub_secrets

logger = logging.getLogger(__name__)
_PROBE_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class CredentialProbeResult:
    """Only a fresh provider authentication rejection can refuse admission."""

    auth_rejected: bool = False
    message: str = ""


def probe_rejected_model_credentials(
    db: Session, *, model: models.AIModel
) -> CredentialProbeResult:
    """Test exactly this model's stored key and record ordinary gateway usage.

    OpenRouter has a free authenticated key endpoint. Other providers receive
    a one-token completion with no retries and a five-second timeout. No
    fallback model or environment credential is permitted. All inconclusive
    results allow admission; only an upstream authentication rejection refuses.

    Args:
        db: Trigger session, before execution insertion.
        model: Customer model whose API key has a standing auth rejection.

    Returns:
        The fresh provider verdict, with sanitized detail and no key material.
    """
    users = crud.crud_user.get_by_account(db, account_id=str(model.account_id), limit=1)
    if not users:
        return CredentialProbeResult()
    gateway = OpenAIGatewayService(
        db,
        ModelGatewayAuthContext(token=NO_BEARER_TOKEN, user=users[0]),
        skip_runtime_session_resolution=True,
    )
    gateway._begin_request_accounting()
    snapshot = GatewayModelSnapshot.from_model(model)
    payload: dict[str, Any] = {
        "model": str(model.id),
        "messages": [{"role": "user", "content": "."}],
        "max_tokens": 1,
        "metadata": {"purpose": "credential_probe"},
    }
    started = time.perf_counter()
    status = 503
    error_class: str | None = None
    message: str | None = None
    response: dict[str, Any] | None = None
    key = ""
    attempted = False
    endpoint = "/openai/v1/chat/completions"
    method = "POST"
    try:
        built = gateway._build_completion_kwargs(
            model,
            messages=payload["messages"],
            payload=payload,
            stream=False,
            provider="openai",
        )
        key = built.get("api_key") or ""
        if not key or gateway._last_upstream_credential_type != "api_key":
            return CredentialProbeResult()
        base = str(built.get("api_base") or "").rstrip("/")
        openrouter = base == "https://openrouter.ai/api/v1" or (
            not base and model.provider_name == "openrouter"
        )
        if openrouter:
            endpoint = "/openai/v1/credential-probe"
            method = "GET"
            with httpx.Client(
                timeout=_PROBE_TIMEOUT_SECONDS, follow_redirects=False
            ) as client:
                attempted = True
                upstream = client.get(
                    "https://openrouter.ai/api/v1/key",
                    headers={"Authorization": f"Bearer {key}"},
                )
                upstream.raise_for_status()
            # The free key check has no inference tokens to bill.
            response = {
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            }
        else:
            # Ignore free-form model parameters and auth overrides. The model
            # mapping and validated endpoint still come from gateway routing.
            kwargs: dict[str, Any] = {
                k: built[k]
                for k in (
                    "model",
                    "api_base",
                    "api_version",
                    "custom_llm_provider",
                )
                if k in built
            }
            kwargs.update(
                api_key=key,
                messages=payload["messages"],
                max_tokens=1,
                timeout=_PROBE_TIMEOUT_SECONDS,
                num_retries=0,
            )
            attempted = True
            response = gateway._response_to_dict(
                gateway.upstream_backend.completion(**kwargs)
            )
        status = 200
    except Exception as exc:
        classified = classify_upstream_error(exc) if attempted else None
        status = (
            classified.status_code
            if classified is not None
            else (getattr(getattr(exc, "response", None), "status_code", None) or 503)
        )
        error_class = classified.error_class if classified is not None else None
        # Never allow an echoed key, even an unfamiliar provider key format,
        # into the usage ledger, notice, refusal or logs.
        raw_detail = str(exc)
        if isinstance(exc, httpx.HTTPStatusError) and attempted:
            status = exc.response.status_code
            raw_detail = exc.response.text
            error_class = classify_recorded_error(status, raw_detail)
        raw = (
            raw_detail.replace(key, "[REDACTED]")
            if key
            else "Credential probe unavailable"
        )
        message = sanitize_string(
            scrub_secrets(extract_upstream_error_detail(raw).message)
            or "Provider rejected the key",
            max_chars=200,
        )
    finally:
        gateway._record_gateway_request(
            endpoint=endpoint,
            method=method,
            status_code=status,
            duration=time.perf_counter() - started,
            ai_model=snapshot,
            requested_model=str(model.id),
            response_payload=response,
            upstream_response=response,
            endpoint_kind="credential_probe",
            error_class=error_class,
            error_detail=message,
            request_payload={"model": str(model.id), "purpose": "credential_probe"},
        )
    record_gateway_credential_outcome(
        db,
        ai_model=snapshot,
        status_code=status,
        error_class=error_class,
        error_detail=message,
        credential_type="api_key",
    )
    current = crud.crud_ai_model.get(db, id=snapshot.id)
    still_current = (
        current is not None
        and current.credentials_secret_id == snapshot.credentials_secret_id
        and current.credentials_version == snapshot.credentials_version
    )
    return CredentialProbeResult(
        auth_rejected=still_current
        and status == 401
        and error_class == "upstream_auth",
        message=message or "Provider rejected the API key.",
    )
