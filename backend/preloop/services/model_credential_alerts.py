"""Best-effort owner email for a serialized credential rejection claim.

The caller persists the claim with the rejection under the secret row lock
before enqueueing. A queue or email failure consumes that episode, matching
flow failure alerts: delivery failures must never produce notification storms.
"""

from __future__ import annotations

import atexit
import html
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from preloop.models.db import get_session_factory
from preloop.services.flow_failure_alerts import _send_emails
from preloop.services.policy_notice_delivery import policy_owners
from preloop.utils.secret_scrubbing import scrub_secrets

logger = logging.getLogger(__name__)
_PENDING = threading.BoundedSemaphore(32)
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="credential-alert")
atexit.register(_EXECUTOR.shutdown, wait=False, cancel_futures=True)


def rejection_notice_data(model: Any, rejection: dict[str, Any]) -> dict[str, Any]:
    """Build an allowlisted notice payload without credential material.

    Args:
        model: Model snapshot used by the rejected request.
        rejection: Sanitized provider verdict stored under the row lock.

    Returns:
        Fields suitable for audit, webhook and owner email.
    """
    billing = rejection.get("status_code") == 402
    return {
        "ai_model_id": str(model.id),
        "model_name": scrub_secrets(str(model.name)) or "Model",
        "reference_id": str(model.credentials_secret_id),
        "provider": scrub_secrets(str(rejection.get("provider") or "Provider")),
        "code": "upstream_billing_rejected" if billing else "upstream_auth_rejected",
        "message": rejection.get("message") or "No provider detail was returned.",
        "first_rejected_at": rejection["first_rejected_at"],
        "remedy": (
            "add credits or replace the key"
            if billing
            else "replace or rotate the API key for this model"
        ),
    }


def build_credential_rejection_message(data: dict[str, Any]) -> dict[str, str]:
    """Render the provider verdict and remedy for an account owner."""
    subject = f"API key rejected for model '{data['model_name']}'"
    lines = [
        subject,
        f"Provider: {data['provider']}",
        f"Provider message: {data['message']}",
        f"First rejected at: {data['first_rejected_at']}",
        f"Remedy: {data['remedy']}.",
        "This rejection came from the provider about the model's API key; "
        "it does not concern your Preloop login.",
        "One notice is sent per rejection episode. Acceptance or key replacement "
        "clears the mark.",
    ]
    return {
        "subject": subject,
        "text": "\n\n".join(lines),
        "html": "".join(f"<p>{html.escape(line)}</p>" for line in lines),
    }


def enqueue_credential_rejection_notice(
    *, account_id: Any, data: dict[str, Any]
) -> None:
    """Defer owner lookup and email without delaying the gateway response."""
    if not _PENDING.acquire(blocking=False):
        logger.warning("Credential notice queue is full; dropping claimed notice")
        return

    def deliver() -> None:
        """Use a worker-owned session; the gateway session may already be closed."""
        try:
            with get_session_factory()() as db:
                _send_emails(
                    db,
                    policy_owners(db, account_id),
                    build_credential_rejection_message(data),
                )
        except Exception:
            logger.warning("Credential rejection notice failed", exc_info=True)
        finally:
            _PENDING.release()

    try:
        _EXECUTOR.submit(deliver)
    except Exception:
        _PENDING.release()
        logger.warning("Could not enqueue credential rejection notice", exc_info=True)
