"""Compatibility shim for the approval workflow's own webhook_url.

Before signed event webhooks existed, one POST was fired inline when an
approval request was created, unsigned and unretried. That configuration key
keeps working. What changed: the POST now goes through the outbox, so it is
signed, retried and visible in the deliveries list.

The shim owns a hidden ``webhook_endpoint`` row per approval workflow
(``source='approval_workflow'``). Those rows are not part of the v1 event
routing: they only ever receive the legacy body, and the console lists them
read-only so an operator can see why deliveries are failing.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from sqlalchemy import select

from preloop.models.models.webhook_endpoint import (
    SOURCE_APPROVAL_WORKFLOW,
    WebhookEndpoint,
)
from preloop.services.event_webhooks.signing import generate_secret, secret_hint
from preloop.utils.encryption import encrypt_value

logger = logging.getLogger(__name__)

# Config keys read from ApprovalWorkflow.approval_config.
CONFIG_URL_KEY = "webhook_url"
CONFIG_SECRET_KEY = "webhook_secret"

SHIM_DESCRIPTION = "Approval workflow webhook (compatibility)"


def config_webhook_url(approval_workflow: Any) -> Optional[str]:
    """Return the configured legacy webhook URL, or None."""
    config = getattr(approval_workflow, "approval_config", None)
    if not isinstance(config, Mapping):
        return None
    url = config.get(CONFIG_URL_KEY)
    return url.strip() if isinstance(url, str) and url.strip() else None


def _ensure_secret(approval_workflow: Any) -> str:
    """Return the workflow's signing secret, generating one on first use.

    Unlike an account endpoint, nobody chose to create this endpoint, so
    there is no "shown once" moment to hand a secret over in. It is written
    back into ``approval_config`` beside the URL, which is where an operator
    who configured the URL will look for it. Receivers that ignore signatures
    are unaffected.
    """
    config = getattr(approval_workflow, "approval_config", None)
    if not isinstance(config, dict):
        config = dict(config or {})
        approval_workflow.approval_config = config
    secret = config.get(CONFIG_SECRET_KEY)
    if isinstance(secret, str) and secret.strip():
        return secret.strip()
    secret = generate_secret()
    config[CONFIG_SECRET_KEY] = secret
    # Reassigned rather than mutated in place: JSONB columns are not
    # mutation-tracked, so an in-place edit would never be written back and
    # the next call would mint a different secret.
    approval_workflow.approval_config = dict(config)
    return secret


def _apply(endpoint: WebhookEndpoint, *, url: str, secret: str) -> None:
    """Point an endpoint row at the configured URL and secret."""
    endpoint.url = url
    endpoint.secret_encrypted = encrypt_value(secret)
    endpoint.secret_hint = secret_hint(secret)
    endpoint.active = True
    endpoint.description = SHIM_DESCRIPTION
    # A URL or secret change is an operator fixing something. Shut the
    # breaker so the fix is tried immediately instead of after a cooldown.
    endpoint.consecutive_failures = 0
    endpoint.circuit_opened_at = None


async def sync_shim_endpoint_async(
    db: Any, approval_workflow: Any
) -> Optional[WebhookEndpoint]:
    """Create, update or deactivate the shim endpoint for one workflow.

    Args:
        db: Async session (or the sync approval adapter).
        approval_workflow: The ``ApprovalWorkflow`` carrying the config.

    Returns:
        The active shim endpoint, or None when no URL is configured.
    """
    workflow_id = getattr(approval_workflow, "id", None)
    account_id = getattr(approval_workflow, "account_id", None)
    if workflow_id is None or account_id is None:
        return None

    result = await db.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.source == SOURCE_APPROVAL_WORKFLOW,
            WebhookEndpoint.approval_workflow_id == workflow_id,
        )
    )
    endpoint = result.scalars().first()

    url = config_webhook_url(approval_workflow)
    if not url:
        if endpoint is not None and endpoint.active:
            endpoint.active = False
            db.add(endpoint)
            await db.flush()
        return None

    secret = _ensure_secret(approval_workflow)
    if endpoint is None:
        endpoint = WebhookEndpoint(
            account_id=account_id,
            source=SOURCE_APPROVAL_WORKFLOW,
            approval_workflow_id=workflow_id,
            event_types=[],
            url=url,
            secret_encrypted=encrypt_value(secret),
            secret_hint=secret_hint(secret),
            description=SHIM_DESCRIPTION,
        )
        db.add(endpoint)
        db.add(approval_workflow)
        await db.flush()
        return endpoint

    if endpoint.url != url or not endpoint.active:
        _apply(endpoint, url=url, secret=secret)
        db.add(endpoint)
    db.add(approval_workflow)
    await db.flush()
    return endpoint
