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
from typing import Any, Mapping, Optional, Tuple

from sqlalchemy import select

from preloop.models.models.webhook_endpoint import (
    SOURCE_APPROVAL_WORKFLOW,
    WebhookEndpoint,
)
from preloop.services.event_webhooks.signing import generate_secret, secret_hint
from preloop.utils.encryption import decrypt_value, encrypt_value

logger = logging.getLogger(__name__)

# Config keys read from ApprovalWorkflow.approval_config.
CONFIG_URL_KEY = "webhook_url"
CONFIG_SECRET_KEY = "webhook_secret"

#: Channels that deliver through the workflow's webhook endpoint, in the
#: order they are looked up in ``channel_configs``. A workflow has one
#: webhook destination; the first configured channel wins.
WEBHOOK_CHANNELS: Tuple[str, ...] = ("webhook", "slack", "mattermost")

SHIM_DESCRIPTION = "Approval workflow webhook (compatibility)"


def _clean_str(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def resolve_webhook_target(approval_workflow: Any) -> Optional[Tuple[str, str]]:
    """Return ``(channel, url)`` for the workflow's webhook, or None.

    Two places configure it:

    * ``channel_configs.<webhook|slack|mattermost>.url`` (``webhook_url`` is
      accepted as a synonym). This is the documented form and the only one
      policy YAML can express.
    * ``approval_config.webhook_url``, the original API form. Its payload
      format follows ``approval_type`` (slack or mattermost), else generic.
    """
    channel_configs = getattr(approval_workflow, "channel_configs", None)
    if isinstance(channel_configs, Mapping):
        for channel in WEBHOOK_CHANNELS:
            entry = channel_configs.get(channel)
            if not isinstance(entry, Mapping):
                continue
            url = _clean_str(entry.get("url")) or _clean_str(entry.get("webhook_url"))
            if url:
                return channel, url

    config = getattr(approval_workflow, "approval_config", None)
    if isinstance(config, Mapping):
        url = _clean_str(config.get(CONFIG_URL_KEY))
        if url:
            approval_type = getattr(approval_workflow, "approval_type", None)
            channel = approval_type if approval_type in WEBHOOK_CHANNELS else "webhook"
            return channel, url
    return None


def config_webhook_url(approval_workflow: Any) -> Optional[str]:
    """Return the configured webhook URL, or None."""
    target = resolve_webhook_target(approval_workflow)
    return target[1] if target else None


def _ensure_secret(approval_workflow: Any) -> str:
    """Return the workflow's signing secret, generating one on first use.

    The API generates the secret when a workflow with a webhook is created
    and returns it once (see ``ensure_webhook_secret``); this is the fallback
    for workflows created another way, such as policy apply. Operators who
    did not keep it rotate it to get a new one.
    """
    config = getattr(approval_workflow, "approval_config", None)
    if not isinstance(config, dict):
        config = dict(config or {})
        approval_workflow.approval_config = config
    secret = config.get(CONFIG_SECRET_KEY)
    if isinstance(secret, str) and secret.strip():
        return secret.strip()
    return rotate_webhook_secret(approval_workflow)


def rotate_webhook_secret(approval_workflow: Any) -> str:
    """Store a new signing secret in ``approval_config`` and return it.

    The shim endpoint picks the new secret up on its next sync.
    """
    config = dict(getattr(approval_workflow, "approval_config", None) or {})
    secret = generate_secret()
    config[CONFIG_SECRET_KEY] = secret
    # Reassigned rather than mutated in place: JSONB columns are not
    # mutation-tracked, so an in-place edit would never be written back.
    approval_workflow.approval_config = config
    return secret


def ensure_webhook_secret(approval_workflow: Any) -> Optional[str]:
    """Generate a secret for a workflow that has a webhook and none yet.

    Returns:
        The newly generated secret (to show once), or None when the
        workflow has no webhook or already had a secret.
    """
    if resolve_webhook_target(approval_workflow) is None:
        return None
    config = getattr(approval_workflow, "approval_config", None)
    if isinstance(config, Mapping) and _clean_str(config.get(CONFIG_SECRET_KEY)):
        return None
    return rotate_webhook_secret(approval_workflow)


def _endpoint_secret_matches(endpoint: WebhookEndpoint, secret: str) -> bool:
    try:
        return decrypt_value(endpoint.secret_encrypted or "") == secret
    except Exception:
        return False


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

    if (
        endpoint.url != url
        or not endpoint.active
        or not _endpoint_secret_matches(endpoint, secret)
    ):
        _apply(endpoint, url=url, secret=secret)
        db.add(endpoint)
    db.add(approval_workflow)
    await db.flush()
    return endpoint


def sync_shim_endpoint(db: Any, approval_workflow: Any) -> Optional[WebhookEndpoint]:
    """Sync twin of :func:`sync_shim_endpoint_async` for worker threads.

    Used by policy notices (#959), which are delivered from the DB thread
    pool with a plain ``Session``. Same rules: create, update or deactivate
    the workflow's shim endpoint and return it when a URL is configured.

    Args:
        db: Sync session.
        approval_workflow: The ``ApprovalWorkflow`` carrying the config.

    Returns:
        The active shim endpoint, or None when no URL is configured.
    """
    workflow_id = getattr(approval_workflow, "id", None)
    account_id = getattr(approval_workflow, "account_id", None)
    if workflow_id is None or account_id is None:
        return None

    endpoint = (
        db.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.source == SOURCE_APPROVAL_WORKFLOW,
                WebhookEndpoint.approval_workflow_id == workflow_id,
            )
        )
        .scalars()
        .first()
    )

    url = config_webhook_url(approval_workflow)
    if not url:
        if endpoint is not None and endpoint.active:
            endpoint.active = False
            db.add(endpoint)
            db.flush()
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
        db.flush()
        return endpoint

    if (
        endpoint.url != url
        or not endpoint.active
        or not _endpoint_secret_matches(endpoint, secret)
    ):
        _apply(endpoint, url=url, secret=secret)
        db.add(endpoint)
    db.add(approval_workflow)
    db.flush()
    return endpoint
