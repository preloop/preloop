"""Read and change the Delivery Report policy (#2020).

One account layer and optional per-repository layers, each storing only the
fields it changes. Reading takes ``view_flows``. Saving the account layer
takes ``manage_budgets``, because it sets what the report may spend and
whether it may write to the tracker; saving a repository layer takes
``edit_projects``, and a repository may change only the windows, the
criteria source and whether it is in the report at all.

Refusals are a 422 carrying ``{"code", "message"}``: ``close_not_allowed``
(the report never closes issues), ``budget_required`` (recurring
assessment needs both budgets), ``account_setting`` (a field a repository
cannot set) and ``invalid_policy``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.models.crud import crud_audit_log, crud_delivery_report_policy
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.user import User
from preloop.schemas.delivery_report_policy import (
    DeliveryReportPolicyResponse,
    DeliveryReportPolicyUpdate,
)
from preloop.services.delivery_report import (
    DEFAULT_POLICY,
    PROJECT_OVERRIDE_FIELDS,
    PolicyOverrideError,
    resolve_policy,
    sync_report_flows,
    validate_account_overrides,
    validate_project_overrides,
)
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

router = APIRouter()

POLICY_PATH = "/delivery-report/policy"
PROJECT_POLICY_PATH = "/delivery-report/policy/projects/{project_id}"


def _response(
    *,
    account_overrides: Optional[Dict[str, Any]],
    project_id: Optional[uuid.UUID] = None,
    project_overrides: Optional[Dict[str, Any]] = None,
) -> DeliveryReportPolicyResponse:
    return DeliveryReportPolicyResponse(
        level="project" if project_id is not None else "account",
        project_id=str(project_id) if project_id is not None else None,
        overrides=(project_overrides if project_id is not None else account_overrides)
        or {},
        effective=resolve_policy(account_overrides, project_overrides),
        defaults=DEFAULT_POLICY,
        project_override_fields=sorted(PROJECT_OVERRIDE_FIELDS),
    )


def _refusal(exc: PolicyOverrideError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": exc.code, "message": exc.message},
    )


def _require_project(db: Session, account: Account, project_id: uuid.UUID) -> None:
    if not crud_delivery_report_policy.project_in_account(
        db, account_id=account.id, project_id=project_id
    ):
        raise HTTPException(status_code=404, detail="Project not found")


def _audit(
    db: Session,
    *,
    user: User,
    action: str,
    resource_id: str,
    details: Dict[str, Any],
) -> None:
    crud_audit_log.log_action(
        db,
        account_id=user.account_id,
        user_id=user.id,
        action=action,
        resource_type="delivery_report_policy",
        resource_id=resource_id,
        status="success",
        details=details,
        commit=False,
    )


@router.get(
    POLICY_PATH,
    response_model=DeliveryReportPolicyResponse,
    summary="Read the account's Delivery Report policy",
)
@require_permission("view_flows")
def read_delivery_report_policy(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> DeliveryReportPolicyResponse:
    """Return the account layer and the policy it resolves to."""
    return _response(
        account_overrides=crud_delivery_report_policy.get_overrides(
            db, account_id=account.id
        )
    )


@router.put(
    POLICY_PATH,
    response_model=DeliveryReportPolicyResponse,
    summary="Save the account's Delivery Report policy",
)
@require_permission("manage_budgets")
def update_delivery_report_policy(
    payload: DeliveryReportPolicyUpdate,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> DeliveryReportPolicyResponse:
    """Replace the account layer. Budgets and tracker writes live here.

    The cadence and the per-run ceiling are copied onto the account's
    Delivery Report flows in the same save.
    """
    ensure_permission_in_oss(db, current_user, "manage_budgets")
    try:
        overrides = validate_account_overrides(payload.overrides)
    except PolicyOverrideError as exc:
        raise _refusal(exc) from exc
    crud_delivery_report_policy.save_overrides(
        db, account_id=account.id, overrides=overrides, user_id=current_user.id
    )
    _audit(
        db,
        user=current_user,
        action="delivery_report_policy_changed",
        resource_id=str(account.id),
        details={"overrides": overrides},
    )
    # The schedule and the per-run ceiling live on the report flows, where
    # the scheduler and the gateway read them.
    sync_report_flows(db, account_id=account.id, policy=resolve_policy(overrides))
    db.commit()
    return _response(account_overrides=overrides)


@router.get(
    PROJECT_POLICY_PATH,
    response_model=DeliveryReportPolicyResponse,
    summary="Read one repository's Delivery Report policy",
)
@require_permission("view_flows")
def read_project_delivery_report_policy(
    project_id: uuid.UUID,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> DeliveryReportPolicyResponse:
    """Return the repository layer and the policy it resolves to."""
    _require_project(db, account, project_id)
    return _response(
        account_overrides=crud_delivery_report_policy.get_overrides(
            db, account_id=account.id
        ),
        project_id=project_id,
        project_overrides=crud_delivery_report_policy.get_overrides(
            db, account_id=account.id, project_id=project_id
        ),
    )


@router.put(
    PROJECT_POLICY_PATH,
    response_model=DeliveryReportPolicyResponse,
    summary="Save one repository's Delivery Report overrides",
)
@require_permission("edit_projects")
def update_project_delivery_report_policy(
    project_id: uuid.UUID,
    payload: DeliveryReportPolicyUpdate,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> DeliveryReportPolicyResponse:
    """Replace the repository layer (windows, criteria source, on or off)."""
    ensure_permission_in_oss(db, current_user, "edit_projects")
    _require_project(db, account, project_id)
    try:
        overrides = validate_project_overrides(payload.overrides)
    except PolicyOverrideError as exc:
        raise _refusal(exc) from exc
    crud_delivery_report_policy.save_overrides(
        db,
        account_id=account.id,
        project_id=project_id,
        overrides=overrides,
        user_id=current_user.id,
    )
    _audit(
        db,
        user=current_user,
        action="delivery_report_policy_changed",
        resource_id=str(project_id),
        details={"project_id": str(project_id), "overrides": overrides},
    )
    db.commit()
    return _response(
        account_overrides=crud_delivery_report_policy.get_overrides(
            db, account_id=account.id
        ),
        project_id=project_id,
        project_overrides=overrides,
    )


@router.delete(
    PROJECT_POLICY_PATH,
    response_model=DeliveryReportPolicyResponse,
    summary="Drop one repository's overrides so the account policy applies",
)
@require_permission("edit_projects")
def delete_project_delivery_report_policy(
    project_id: uuid.UUID,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> DeliveryReportPolicyResponse:
    """Remove the repository layer."""
    ensure_permission_in_oss(db, current_user, "edit_projects")
    _require_project(db, account, project_id)
    if crud_delivery_report_policy.delete_overrides(
        db, account_id=account.id, project_id=project_id
    ):
        _audit(
            db,
            user=current_user,
            action="delivery_report_policy_cleared",
            resource_id=str(project_id),
            details={"project_id": str(project_id)},
        )
        db.commit()
    return _response(
        account_overrides=crud_delivery_report_policy.get_overrides(
            db, account_id=account.id
        ),
        project_id=project_id,
    )
