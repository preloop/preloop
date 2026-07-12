"""Startup backfill that encrypts legacy plaintext tracker credentials.

Older rows stored the tracker API key (and Jira webhook secret) directly in the
``tracker.api_key`` / ``tracker.jira_webhook_secret`` columns. This pass moves
any such plaintext value into an encrypted ``SecretReference`` and clears the
column. Idempotent and safe to run on every boot; per-tracker failures are
logged and skipped so one bad row cannot abort the whole pass.
"""

from __future__ import annotations

import logging

from preloop.models.crud.tracker import (
    TRACKER_API_KEY_SECRET_KIND,
    TRACKER_WEBHOOK_SECRET_KIND,
    store_tracker_secret,
)
from preloop.models.db.session import get_session_factory
from preloop.models.models.tracker import Tracker

logger = logging.getLogger(__name__)


def run_tracker_credential_encryption_backfill() -> dict[str, int]:
    """Encrypt any remaining plaintext tracker credentials. Returns counts."""
    session_factory = get_session_factory()
    db = session_factory()
    migrated_api = 0
    migrated_webhook = 0
    scanned = 0
    try:
        # Only rows that still carry a plaintext value with no secret reference.
        candidates = (
            db.query(Tracker)
            .filter(
                (
                    (Tracker.api_key.isnot(None) & (Tracker.api_key != ""))
                    & Tracker.credentials_secret_id.is_(None)
                )
                | (
                    (
                        Tracker.jira_webhook_secret.isnot(None)
                        & (Tracker.jira_webhook_secret != "")
                    )
                    & Tracker.webhook_secret_id.is_(None)
                )
            )
            .all()
        )
        for tracker in candidates:
            scanned += 1
            try:
                if tracker.api_key and not tracker.credentials_secret_id:
                    tracker.credentials_secret_id = store_tracker_secret(
                        db,
                        account_id=tracker.account_id,
                        name=f"{tracker.name} API key",
                        secret_value=tracker.api_key,
                        secret_kind=TRACKER_API_KEY_SECRET_KIND,
                    )
                    tracker.api_key = None
                    migrated_api += 1
                if tracker.jira_webhook_secret and not tracker.webhook_secret_id:
                    tracker.webhook_secret_id = store_tracker_secret(
                        db,
                        account_id=tracker.account_id,
                        name=f"{tracker.name} webhook secret",
                        secret_value=tracker.jira_webhook_secret,
                        secret_kind=TRACKER_WEBHOOK_SECRET_KIND,
                    )
                    tracker.jira_webhook_secret = None
                    migrated_webhook += 1
                db.add(tracker)
                db.commit()
            except Exception as inner_exc:  # pragma: no cover - defensive
                db.rollback()
                logger.warning(
                    "Tracker credential backfill failed for tracker %s: %s",
                    getattr(tracker, "id", "unknown"),
                    inner_exc,
                )
    finally:
        db.close()

    return {
        "scanned": scanned,
        "migrated_api_keys": migrated_api,
        "migrated_webhook_secrets": migrated_webhook,
    }
