"""Startup backfill that encrypts legacy plaintext tracker credentials.

Older rows stored the tracker API key (and Jira webhook secret) directly in the
``tracker.api_key`` / ``tracker.jira_webhook_secret`` columns. This pass moves
any such plaintext value into an encrypted ``SecretReference`` and clears the
column. Idempotent and safe to run on every boot; per-tracker failures are
logged and skipped so one bad row cannot abort the whole pass.
"""

from __future__ import annotations

import logging

from preloop.models.crud.tracker import crud_tracker
from preloop.models.db.session import get_session_factory

logger = logging.getLogger(__name__)


def run_tracker_credential_encryption_backfill() -> dict[str, int]:
    """Encrypt any remaining plaintext tracker credentials. Returns counts."""
    session_factory = get_session_factory()
    db = session_factory()
    migrated_api = 0
    migrated_webhook = 0
    scanned = 0
    try:
        candidates = crud_tracker.list_plaintext_credential_candidates(db)
        for tracker in candidates:
            scanned += 1
            try:
                result = crud_tracker.migrate_plaintext_credentials(
                    db, tracker=tracker, commit=True
                )
                if result["migrated_api_key"]:
                    migrated_api += 1
                if result["migrated_webhook_secret"]:
                    migrated_webhook += 1
            except Exception as inner_exc:
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
