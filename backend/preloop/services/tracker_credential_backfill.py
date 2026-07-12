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
    """Encrypt any remaining plaintext tracker credentials. Returns counts.

    Each tracker runs in its own session because secret creation commits
    internally — a shared-session ``rollback()`` after failure would otherwise
    discard work (or leave the session unusable) for later candidates.
    """
    session_factory = get_session_factory()
    list_db = session_factory()
    try:
        candidates = crud_tracker.list_plaintext_credential_candidates(list_db)
        candidate_ids = [tracker.id for tracker in candidates]
    finally:
        list_db.close()

    migrated_api = 0
    migrated_webhook = 0
    scanned = 0
    for tracker_id in candidate_ids:
        scanned += 1
        db = session_factory()
        try:
            tracker = crud_tracker.get(db, id=tracker_id)
            if tracker is None:
                continue
            result = crud_tracker.migrate_plaintext_credentials(
                db, tracker=tracker, commit=True
            )
            if result["migrated_api_key"]:
                migrated_api += 1
            if result["migrated_webhook_secret"]:
                migrated_webhook += 1
        except Exception as inner_exc:
            logger.warning(
                "Tracker credential backfill failed for tracker %s: %s",
                tracker_id,
                inner_exc,
            )
        finally:
            db.close()

    return {
        "scanned": scanned,
        "migrated_api_keys": migrated_api,
        "migrated_webhook_secrets": migrated_webhook,
    }
