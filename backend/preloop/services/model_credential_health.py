"""Health of a model's API key as the upstream provider last judged it.

A stored API key can stop working without anything changing in Preloop: the
key is revoked, its provider account is deleted, or it was pasted from the
wrong provider account. Until now the only trace was a 401 on each request,
so a dead key failed run after run while the model row still read ``active``.

The gateway records the provider's verdict on the credential secret itself:

* an upstream ``401`` on an API-key credential stores a small
  ``upstream_auth_rejection`` object in the secret's ``meta_data``;
* the next request the provider accepts with that credential removes it;
* replacing or rotating the key removes it.

``AIModel`` reads the object back as ``credentials_status == "error"`` with a
summary, so the console's model attention marker, the API and the CLI that
already show a failed OAuth refresh show a rejected API key the same way.

Nothing here reads, stores or logs key material.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from preloop.services.model_gateway_errors import provider_message_from_label
from preloop.models.crud.secret_reference import crud_secret_reference
from preloop.models.models.secret_reference import UPSTREAM_AUTH_REJECTION_KEY
from preloop.services.upstream_errors import ERROR_CLASS_UPSTREAM_AUTH
from preloop.utils.jsonb_sanitize import sanitize_string

logger = logging.getLogger(__name__)

#: Error code surfaced as ``credentials_last_error_code`` for a rejected key.
UPSTREAM_AUTH_REJECTED_CODE = "upstream_auth_rejected"

#: Longest provider message kept on the secret row.
_MAX_PROVIDER_MESSAGE_CHARS = 200

__all__ = [
    "UPSTREAM_AUTH_REJECTED_CODE",
    "UPSTREAM_AUTH_REJECTION_KEY",
    "credential_rejection",
    "credential_rejection_summary",
    "record_gateway_credential_outcome",
]


def credential_rejection(secret: Any) -> Optional[dict[str, Any]]:
    """Return the stored upstream rejection for a credential secret, if any.

    Args:
        secret: A ``SecretReference`` (or ``None``).

    Returns:
        The rejection object, or ``None`` when the provider has not rejected
        the stored key since it was last accepted, replaced or rotated.
    """
    rejection = getattr(secret, "upstream_auth_rejection", None)
    return rejection if isinstance(rejection, dict) else None


def credential_rejection_summary(rejection: dict[str, Any]) -> str:
    """Describe a stored rejection in one sentence, without key material.

    Args:
        rejection: The object returned by :func:`credential_rejection`.

    Returns:
        A sentence naming the provider, its HTTP status and message, and since
        when the stored key has been rejected.
    """
    provider = str(rejection.get("provider") or "The upstream provider")
    status_code = rejection.get("status_code") or 401
    message = str(rejection.get("message") or "").strip()
    detail = f"HTTP {status_code}"
    if message:
        detail = f"{detail}: {message}"
    summary = f"{provider} rejected the stored API key ({detail})."
    first = rejection.get("first_rejected_at")
    if isinstance(first, str) and first:
        summary = f"{summary} Rejected since {first}."
    return summary


def record_gateway_credential_outcome(
    db: Session,
    *,
    ai_model: Any,
    status_code: int,
    error_class: Optional[str],
    error_detail: Optional[str],
    credential_type: Optional[str],
    hosted: bool = False,
) -> None:
    """Record what one gateway request proved about the model's API key.

    Only customer-owned models with an API-key credential are tracked. A
    deployment-provided model's key is the operator's concern and must not
    show up as a customer's credential problem: that covers system models
    (no owning account) and account rows marked ``hosted``.

    Args:
        db: The gateway's own short bookkeeping session. Each write commits.
        ai_model: The model row or gateway snapshot the request used.
        status_code: Status recorded for the request.
        error_class: Recorded error class, if the request failed.
        error_detail: Provider message recorded for the failure.
        credential_type: Credential type the gateway sent upstream.
        hosted: The model is deployment-provided (operator-paid), by the same
            rule billing uses. Its key is never a customer's to rotate.
    """
    if credential_type != "api_key" or hosted:
        return
    model_id = getattr(ai_model, "id", None)
    if model_id is None or getattr(ai_model, "account_id", None) is None:
        return

    if status_code == 401 and error_class == ERROR_CLASS_UPSTREAM_AUTH:
        # Provider bodies can carry NUL or lone surrogates, which PostgreSQL
        # refuses in JSONB; an unsanitized message would lose the record.
        # The recorded detail is the client-facing message, which names the
        # upstream origin (#2288); the credential keeps the provider's own
        # sentence, as the summary already says who rejected the key.
        message = sanitize_string(
            " ".join(str(provider_message_from_label(error_detail) or "").split()),
            max_chars=_MAX_PROVIDER_MESSAGE_CHARS,
        )
        crud_secret_reference.record_upstream_auth_rejection(
            db,
            ai_model_id=model_id,
            secret_id=getattr(ai_model, "credentials_secret_id", None),
            version=getattr(ai_model, "credentials_version", None),
            provider=str(getattr(ai_model, "provider_name", "") or ""),
            status_code=status_code,
            message=message,
            now=datetime.now(timezone.utc),
        )
        return

    if status_code < 400 and getattr(ai_model, "credentials_rejected", False):
        crud_secret_reference.clear_upstream_auth_rejection(
            db,
            ai_model_id=model_id,
            secret_id=getattr(ai_model, "credentials_secret_id", None),
            version=getattr(ai_model, "credentials_version", None),
            now=datetime.now(timezone.utc),
        )
