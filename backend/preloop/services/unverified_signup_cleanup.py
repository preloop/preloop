"""Remove self-registered accounts that were never verified or used.

Registration creates an account at once, before the address is verified. An
account whose address was never verified and that nobody ever signed in to
holds nothing but registration's own defaults. This pass finds such accounts
older than ``UNVERIFIED_SIGNUP_MAX_AGE_DAYS`` (default 7) and deletes them in
bounded batches.

It never touches an account that has a verified address, any sign-in, an API
key, an agent, a billing customer, a sub-account, more than one member, or any
other row pointing at it (see
``crud_account.list_unused_unverified_signups``).

Modes (``UNVERIFIED_SIGNUP_CLEANUP``):

* ``dry-run`` (default): count and log what would be deleted; change nothing.
* ``apply``: delete, writing one audit row per batch
  (``unverified_signups_deleted``) in the first ``OPERATOR_ACCOUNT_IDS``
  account. Without an operator account the pass refuses to delete and runs
  as a dry run, so no deletion goes unrecorded.
* ``off``: skip the pass.

The pass runs hourly inside the ``deactivate_ended_run_keys`` maintenance
task. Operators can also run it by hand::

    python -m preloop.services.unverified_signup_cleanup            # dry run
    python -m preloop.services.unverified_signup_cleanup --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_audit_log, crud_person, crud_user

logger = logging.getLogger(__name__)

MODE_ENV = "UNVERIFIED_SIGNUP_CLEANUP"
AGE_ENV = "UNVERIFIED_SIGNUP_MAX_AGE_DAYS"
MODES = ("dry-run", "apply", "off")
DEFAULT_MODE = "dry-run"
DEFAULT_MAX_AGE_DAYS = 7
MIN_MAX_AGE_DAYS = 1
DEFAULT_BATCH_SIZE = 100
DEFAULT_MAX_BATCHES = 2
AUDIT_ACTION = "unverified_signups_deleted"


def configured_mode() -> str:
    """Return the mode from ``UNVERIFIED_SIGNUP_CLEANUP`` (unknown: dry-run)."""
    raw = os.getenv(MODE_ENV, DEFAULT_MODE).strip().lower()
    if raw not in MODES:
        logger.error("Ignoring invalid %s=%r; using %s", MODE_ENV, raw, DEFAULT_MODE)
        return DEFAULT_MODE
    return raw


def configured_max_age_days() -> int:
    """Return ``UNVERIFIED_SIGNUP_MAX_AGE_DAYS``, never below one day."""
    try:
        value = int(os.getenv(AGE_ENV, str(DEFAULT_MAX_AGE_DAYS)))
    except ValueError:
        value = DEFAULT_MAX_AGE_DAYS
    return max(MIN_MAX_AGE_DAYS, value)


def _operator_account_ids() -> list[str]:
    """Operator accounts: never cleaned up; the first holds the audit rows."""
    from preloop.config import settings

    return [str(i) for i in settings.operator_account_ids]


@dataclass
class SignupCleanupResult:
    """What one pass found and did (counts only).

    Attributes:
        mode: ``dry-run``, ``apply`` or ``off`` as run.
        batches: Batches read.
        matched: Accounts that qualified.
        deleted: Accounts deleted (0 in a dry run).
        complete: False when the pass stopped at ``max_batches``.
        refused: Why an ``apply`` ran as a dry run, if it did.
    """

    mode: str
    batches: int = 0
    matched: int = 0
    deleted: int = 0
    complete: bool = True
    refused: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly summary."""
        return {
            "mode": self.mode,
            "batches": self.batches,
            "matched": self.matched,
            "deleted": self.deleted,
            "complete": self.complete,
            "refused": self.refused,
        }


def cleanup_unverified_signups(
    db: Session,
    *,
    mode: Optional[str] = None,
    max_age_days: Optional[int] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = DEFAULT_MAX_BATCHES,
    now: Optional[datetime] = None,
) -> SignupCleanupResult:
    """Run one bounded pass.

    Args:
        db: Database session. Each applied batch commits on its own.
        mode: ``dry-run``, ``apply`` or ``off``; defaults to the setting.
        max_age_days: Minimum account age; defaults to the setting.
        batch_size: Accounts examined per batch.
        max_batches: Batches per pass.
        now: Current time; defaults to now.

    Returns:
        Counts for the pass.
    """
    run_mode = mode or configured_mode()
    result = SignupCleanupResult(mode=run_mode)
    if run_mode == "off":
        return result
    operator_ids = _operator_account_ids()
    audit_account = operator_ids[0] if operator_ids else None
    if run_mode == "apply" and audit_account is None:
        result.mode = "dry-run"
        result.refused = "no OPERATOR_ACCOUNT_IDS account to hold the audit rows"
        logger.warning(
            "Unverified sign-up cleanup set to apply but OPERATOR_ACCOUNT_IDS is "
            "empty; running as a dry run"
        )
    apply = result.mode == "apply"
    days = max(MIN_MAX_AGE_DAYS, max_age_days or configured_max_age_days())
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    exclude = tuple(operator_ids)

    after_id: Optional[Any] = None
    for _ in range(max_batches):
        account_ids, after_id = crud_account.list_unused_unverified_signups(
            db,
            created_before=cutoff,
            limit=batch_size,
            after_id=after_id,
            exclude_ids=exclude,
        )
        if after_id is None:
            break
        result.batches += 1
        result.matched += len(account_ids)
        if apply and account_ids:
            result.deleted += _delete_batch(
                db,
                account_ids,
                audit_account=str(audit_account),
                days=days,
                created_before=cutoff,
                exclude=exclude,
            )
    else:
        result.complete = after_id is None
        if after_id is not None:
            # One more peek tells a full last batch from a finished scan.
            _, more = crud_account.list_unused_unverified_signups(
                db,
                created_before=cutoff,
                limit=1,
                after_id=after_id,
                exclude_ids=exclude,
            )
            result.complete = more is None
    if result.matched:
        logger.info("Unverified sign-up cleanup: %s", result.as_dict())
    return result


def _delete_batch(
    db: Session,
    account_ids: list[Any],
    *,
    audit_account: str,
    days: int,
    created_before: datetime,
    exclude: tuple[str, ...],
) -> int:
    """Delete one batch and record it, in one transaction.

    The candidates are qualified again with their rows locked, so an account
    verified or signed in to since the scan is kept.

    Returns:
        Accounts deleted.
    """
    account_ids, _ = crud_account.list_unused_unverified_signups(
        db,
        created_before=created_before,
        limit=len(account_ids),
        exclude_ids=exclude,
        only_ids=account_ids,
        lock=True,
    )
    if not account_ids:
        db.rollback()
        return 0
    emails: list[str] = []
    for account_id in account_ids:
        for user in crud_user.get_by_account(db, account_id=str(account_id)):
            if user.email:
                emails.append(user.email)
        crud_account.purge(db, account_id=str(account_id), commit=False)
    crud_person.delete_unattached(db, emails=emails)
    crud_audit_log.log_action(
        db,
        account_id=audit_account,
        action=AUDIT_ACTION,
        resource_type="account",
        status="success",
        details={
            "count": len(account_ids),
            "account_ids": [str(i) for i in account_ids],
            "max_age_days": days,
        },
        commit=False,
    )
    db.commit()
    return len(account_ids)


def main(argv: Optional[list[str]] = None) -> int:
    """Command-line entry point: dry run unless ``--apply``."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="delete accounts")
    parser.add_argument("--max-age-days", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-batches", type=int, default=DEFAULT_MAX_BATCHES)
    args = parser.parse_args(argv)

    from preloop.models.db.session import get_db_session

    db = next(get_db_session())
    try:
        result = cleanup_unverified_signups(
            db,
            mode="apply" if args.apply else "dry-run",
            max_age_days=args.max_age_days,
            batch_size=args.batch_size,
            max_batches=args.max_batches,
        )
    finally:
        db.close()
    print(json.dumps(result.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
