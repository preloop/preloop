"""Service for managing default approval workflows.

This service provides functions to create default approval workflows for accounts
when they are first created. In the open-source edition, each account has a
single default "Standard" approval workflow that is used for all tool approvals.
"""

import logging
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_approval_workflow
from preloop.models.db.session import get_session_factory

logger = logging.getLogger(__name__)

# Stored value for the default workflow's ``approval_type``. The frontend
# dialog's "Type" dropdown labels this as "Standard Human Approval"; matching
# the dropdown value (rather than the older "manual" synonym) ensures that
# when an account owner opens the default workflow, the type field is
# populated correctly instead of appearing blank.
DEFAULT_APPROVAL_TYPE = "standard"


def _resolve_account_owner_user_id(db: Session, account_id: UUID) -> Optional[UUID]:
    """Best-effort lookup of an account's owner / first user.

    Used as a fallback when ``create_default_approval_workflow_for_account``
    is called without an explicit ``user_id`` so the seeded default workflow
    still has at least one approver — otherwise approval requests created
    against the default workflow would have no one able to act on them.

    Prefers the account's ``primary_user_id`` (the actual owner set at
    signup) and only falls back to the oldest user when it is unset.
    """
    account = db.query(models.Account).filter(models.Account.id == account_id).first()
    if account is not None and account.primary_user_id:
        return account.primary_user_id
    user = (
        db.query(models.User)
        .filter(models.User.account_id == account_id)
        .order_by(models.User.created_at.asc())
        .first()
    )
    return user.id if user else None


def create_default_approval_workflow_for_account(
    account_id: UUID, user_id: UUID | None = None
) -> None:
    """
    Create a default approval workflow for a newly created account.

    This ensures every account has a default "Standard" workflow that can be
    used for tool approvals. In the open-source edition, this is the only
    workflow the account will need.

    Args:
        account_id: The UUID of the account to create the workflow for.
        user_id: The UUID of the user to set as the default approver.
                 In single-user mode, this should be the account owner.
                 If omitted, the account's first user (by ``created_at``) is
                 used so the default workflow always has an approver.
    """
    session_factory = get_session_factory()
    db = session_factory()

    try:
        # Check if a default workflow already exists
        existing_default = crud_approval_workflow.get_default(
            db, account_id=str(account_id)
        )

        if existing_default:
            logger.debug(
                f"Default approval workflow already exists for account {account_id}"
            )
            return

        # Check if any policies exist at all
        existing_policies = crud_approval_workflow.get_multi_by_account(
            db, account_id=str(account_id), limit=1
        )

        if existing_policies:
            logger.debug(
                f"Approval workflows already exist for account {account_id}, "
                "skipping default creation"
            )
            return

        # Resolve an approver user id. Caller-provided ``user_id`` wins; we
        # only fall back to the account owner lookup so we never seed a
        # default workflow with an empty ``approver_user_ids`` array (which
        # would make every default-routed approval request unresolvable).
        approver_user_id: Optional[UUID] = user_id or _resolve_account_owner_user_id(
            db, account_id
        )

        # Create the default workflow with the user as the approver
        workflow_data = {
            "name": "Default Approval Workflow",
            "description": (
                "Default workflow for tool approval requests. "
                "Approval requests will be shown in the Preloop UI."
            ),
            "approval_type": DEFAULT_APPROVAL_TYPE,
            "approval_mode": "standard",  # standard = human approval
            "is_default": True,
            "approvals_required": 1,
        }

        if approver_user_id:
            workflow_data["approver_user_ids"] = [str(approver_user_id)]
        else:
            logger.warning(
                f"No approver user could be resolved for account {account_id}; "
                "default approval workflow will be created without approvers. "
                "Tool calls routed through this default workflow will need "
                "approvers configured before they can be acted upon."
            )

        crud_approval_workflow.create(
            db,
            obj_in=workflow_data,
            account_id=str(account_id),
        )

        logger.info(
            f"Created default approval workflow for account {account_id} "
            f"with approver user_id={approver_user_id}"
        )

    except Exception as e:
        logger.error(
            f"Failed to create default approval workflow for account {account_id}: {e}",
            exc_info=True,
        )
    finally:
        db.close()


def repair_default_approval_workflow_for_account(
    account_id: UUID, user_id: UUID | None = None
) -> bool:
    """Heal an existing default workflow that's missing a usable type/approver.

    Older code paths created the per-account default workflow with
    ``approval_type="manual"`` (a synonym the dialog dropdown can't render,
    so the type field showed as blank) and could leave ``approver_user_ids``
    empty if no ``user_id`` was passed in. This helper normalises both:

    * If the default workflow's ``approval_type`` is the legacy ``manual``
      value, rewrite it to :data:`DEFAULT_APPROVAL_TYPE` (``"standard"``).
    * If the default workflow has no ``approver_user_ids`` *and* no
      ``approver_team_ids``, populate ``approver_user_ids`` with the
      provided ``user_id`` (or the account's first user) so approvals
      routed through the default workflow have someone able to act on them.

    Returns ``True`` if the workflow was modified, ``False`` otherwise.
    """
    session_factory = get_session_factory()
    db = session_factory()

    try:
        existing_default = crud_approval_workflow.get_default(
            db, account_id=str(account_id)
        )
        if not existing_default:
            return False

        modified = False

        if existing_default.approval_type == "manual":
            existing_default.approval_type = DEFAULT_APPROVAL_TYPE
            modified = True

        has_approver = bool(existing_default.approver_user_ids) or bool(
            existing_default.approver_team_ids
        )
        if not has_approver:
            approver_user_id = user_id or _resolve_account_owner_user_id(db, account_id)
            if approver_user_id:
                existing_default.approver_user_ids = [approver_user_id]
                modified = True

        if modified:
            db.commit()
            logger.info(
                f"Repaired default approval workflow {existing_default.id} "
                f"for account {account_id} (approval_type="
                f"{existing_default.approval_type}, "
                f"approvers={existing_default.approver_user_ids})"
            )
        return modified

    except Exception as e:
        logger.error(
            f"Failed to repair default approval workflow for account {account_id}: {e}",
            exc_info=True,
        )
        db.rollback()
        return False
    finally:
        db.close()


def create_default_approval_workflow_background(
    account_id: UUID, user_id: UUID | None = None
) -> None:
    """
    Background task wrapper for creating default approval workflow.

    This function is designed to be called via FastAPI's BackgroundTasks
    to avoid blocking the registration response.

    Args:
        account_id: The UUID of the account to create the workflow for.
        user_id: The UUID of the user to set as the default approver.
    """
    try:
        create_default_approval_workflow_for_account(account_id, user_id)
    except Exception as e:
        # Log but don't raise - this is a background task
        logger.error(
            f"Background task failed to create default approval workflow "
            f"for account {account_id}: {e}",
            exc_info=True,
        )


def run_default_approval_workflow_startup_repair() -> dict[str, int]:
    """Heal legacy defaults and seed missing account workflows on API boot.

    Idempotent and safe to run on every startup. Per-account failures are
    logged and skipped so one bad account cannot abort the whole pass.
    """
    session_factory = get_session_factory()
    scan_db = session_factory()
    try:
        broken, missing = crud_approval_workflow.find_startup_repair_targets(scan_db)
    finally:
        scan_db.close()

    repaired = 0
    for account_id in broken:
        try:
            if repair_default_approval_workflow_for_account(account_id):
                repaired += 1
        except Exception as inner_exc:
            logger.warning(
                "Default workflow repair failed for account %s: %s",
                account_id,
                inner_exc,
            )

    seeded = 0
    for account_id in missing:
        try:
            create_default_approval_workflow_for_account(account_id)
            seeded += 1
        except Exception as inner_exc:
            logger.warning(
                "Default workflow seeding failed for account %s: %s",
                account_id,
                inner_exc,
            )

    return {
        "broken": len(broken),
        "repaired": repaired,
        "missing": len(missing),
        "seeded": seeded,
    }
