"""Account-scoped basic budget administration for every edition."""

import logging
from datetime import datetime, timezone
from typing import Any, List
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ConfigDict, field_validator
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models.db.session import get_db_session
from preloop.utils.permissions import require_permission
from preloop.models import models
from preloop.models.models.access_values import BUDGET_SUBJECT_TEAM
from preloop.services.configuration_gating import authorize_budget_configuration
from preloop.models.crud.budget_configuration import (
    validate_budget_subject,
    validate_budget_recipients,
)
from preloop.utils.permissions import ensure_permission_in_oss
from preloop.models.crud.budget import (
    crud_budget_policy,
    crud_budget_spend,
    get_period_end,
    get_period_start,
    spend_bucket_for_policy,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/budget", tags=["Budget"])


class BudgetPolicyCreate(BaseModel):
    subject_type: str = Field(
        ...,
        description="Subject type: account, api_key, managed_agent, ai_model, or user",
    )
    subject_id: uuid.UUID | str | None = Field(
        None, description="UUID of the subject. None if subject_type is account"
    )
    model_alias: str | None = Field(
        None, description="Model alias to restrict. None for all models."
    )
    period: models.BudgetPeriod = Field(...)
    hard_limit_usd: float | None = Field(
        None, ge=0, allow_inf_nan=False, description="Hard limit in USD"
    )
    soft_limit_usd: float | None = Field(
        None, ge=0, allow_inf_nan=False, description="Soft limit in USD"
    )
    notify_on_soft: bool | None = Field(False)
    notify_on_hard: bool | None = Field(False)
    notification_user_ids: List[uuid.UUID] | None = Field(
        None, description="Users to notify using their notification preferences"
    )
    notification_team_ids: List[uuid.UUID] | None = Field(
        None, description="Teams to notify using member notification preferences"
    )
    notification_emails: List[str] | None = Field(
        None, description="Additional custom email addresses to notify"
    )

    @field_validator("subject_id", mode="before")
    @classmethod
    def serialize_uuid(cls, value: Any) -> Any:
        if isinstance(value, uuid.UUID):
            return str(value)
        return value


class BudgetPolicyUpdate(BaseModel):
    hard_limit_usd: float | None = Field(None, ge=0, allow_inf_nan=False)
    soft_limit_usd: float | None = Field(None, ge=0, allow_inf_nan=False)
    notify_on_soft: bool | None = Field(None)
    notify_on_hard: bool | None = Field(None)
    notification_user_ids: List[uuid.UUID] | None = Field(None)
    notification_team_ids: List[uuid.UUID] | None = Field(None)
    notification_emails: List[str] | None = Field(None)


class BudgetPolicyResponse(BudgetPolicyCreate):
    id: uuid.UUID
    # Spend accumulated in the policy's CURRENT period window (e.g. today for
    # a daily policy, this month for a monthly one). The UI must use this
    # instead of deriving spend from the cost-summary date range, which
    # ignores the policy period entirely.
    current_spend_usd: float | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


def _subject_coordinates(
    db: Session,
    account_id: Any,
    subject_type: str,
    subject_id: Any,
    model_alias: str | None = None,
) -> tuple[str, Any, str | None]:
    from preloop.services.model_runtime_resolver import resolve_ai_model_runtime

    subject = validate_budget_subject(
        db, account_id=account_id, subject_type=subject_type, subject_id=subject_id
    )
    if subject_type == "ai_model":
        alias = resolve_ai_model_runtime(subject).model_gateway_model_alias
        if not alias:
            raise ValueError("Model does not have an enabled gateway alias")
        if model_alias and model_alias != alias:
            raise ValueError("Model alias does not match the selected model")
        return "account", None, alias
    model_alias = model_alias.strip() or None if model_alias is not None else None
    if model_alias is not None:
        from preloop.models.crud import crud_ai_model
        from preloop.services.model_runtime_resolver import effective_gateway_alias

        available = crud_ai_model.get_all_for_account(db, account_id=account_id)
        if not any(
            effective_gateway_alias(model) == model_alias for model in available
        ):
            raise ValueError(
                "Model alias does not match an enabled gateway model in this account"
            )
    return subject_type, subject_id, model_alias


def _policy_response_fields(policy: Any) -> BudgetPolicyResponse:
    """Serialize configured fields without an individual spend query."""
    return BudgetPolicyResponse(
        id=policy.id,
        subject_type=policy.subject_type,
        subject_id=policy.subject_id,
        model_alias=policy.model_alias,
        period=policy.period,
        hard_limit_usd=policy.hard_limit_usd,
        soft_limit_usd=policy.soft_limit_usd,
        notify_on_soft=policy.notify_on_soft,
        notify_on_hard=policy.notify_on_hard,
        notification_user_ids=policy.notification_user_ids,
        notification_team_ids=policy.notification_team_ids,
        notification_emails=policy.notification_emails,
    )


def _policies_to_response(
    db: Session, policies: list[Any]
) -> list[BudgetPolicyResponse]:
    """Decorate one account's policies from a single current-period spend query."""
    responses = [_policy_response_fields(policy) for policy in policies]
    if not policies:
        return responses
    now = datetime.now(timezone.utc)
    buckets = []
    for policy, response in zip(policies, responses, strict=False):
        response.period_start = get_period_start(now, policy.period)
        response.period_end = get_period_end(now, policy.period)
        bucket_type, subject_id, alias = spend_bucket_for_policy(policy)
        buckets.append(
            (
                bucket_type,
                subject_id,
                alias or None,
                policy.period,
                response.period_start,
            )
        )
    try:
        spend = crud_budget_spend.get_spend_multi(
            db, account_id=policies[0].account_id, buckets=list(dict.fromkeys(buckets))
        )
    except Exception:  # noqa: BLE001 - unavailable spend is unknown, never zero
        logger.exception("Failed to resolve current spend for account policies")
        return responses
    for response, bucket in zip(responses, buckets, strict=False):
        response.current_spend_usd = spend.get(bucket, 0.0)
    return responses


def _policy_to_response(db: Session, policy: Any) -> BudgetPolicyResponse:
    return _policies_to_response(db, [policy])[0]


@router.post(
    "/policies",
    response_model=BudgetPolicyResponse,
)
@require_permission("manage_budgets")
def create_budget_policy(
    *,
    db: Session = Depends(get_db_session),
    policy_in: BudgetPolicyCreate,
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Create a new budget policy."""
    ensure_permission_in_oss(db, current_user, "manage_budgets")
    authorize_budget_configuration(
        db, str(current_user.account_id), policy_in.model_dump()
    )
    subject_type = (
        "account" if policy_in.subject_type == "global" else policy_in.subject_type
    )
    subject_id_uuid = None
    if policy_in.subject_id and str(policy_in.subject_id) != "global":
        try:
            subject_id_uuid = uuid.UUID(str(policy_in.subject_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid subject_id UUID")

    try:
        subject_type, subject_id_uuid, model_alias = _subject_coordinates(
            db,
            current_user.account_id,
            subject_type,
            subject_id_uuid,
            policy_in.model_alias,
        )
        validate_budget_recipients(
            db, account_id=current_user.account_id, data=policy_in.model_dump()
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    # Check for existing policy
    existing = crud_budget_policy.get_policies_for_subject(
        db,
        account_id=current_user.account_id,
        subject_type=subject_type,
        subject_id=subject_id_uuid,
    )
    for p in existing:
        if (p.model_alias or None) == model_alias and p.period == policy_in.period:
            raise HTTPException(
                status_code=400,
                detail="Policy with this subject, model, and period already exists",
            )

    new_policy = crud_budget_policy.create(
        db=db,
        obj_in={
            "account_id": current_user.account_id,
            "subject_type": subject_type,
            "subject_id": subject_id_uuid,
            "model_alias": model_alias,
            "period": policy_in.period,
            "hard_limit_usd": policy_in.hard_limit_usd,
            "soft_limit_usd": policy_in.soft_limit_usd,
            "notify_on_soft": policy_in.notify_on_soft,
            "notify_on_hard": policy_in.notify_on_hard,
            "notification_user_ids": policy_in.notification_user_ids,
            "notification_team_ids": policy_in.notification_team_ids,
            "notification_emails": policy_in.notification_emails,
        },
    )
    return _policy_to_response(db, new_policy)


@router.get("/policies", response_model=List[BudgetPolicyResponse])
@require_permission("view_cost")
def get_budget_policies(
    subject_type: str | None = None,
    subject_id: str | None = None,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Retrieve budget policies for the account, optionally filtered."""
    ensure_permission_in_oss(db, current_user, "view_cost")
    if subject_type:
        lookup_subject_type = "account" if subject_type == "global" else subject_type
        sid = None
        if subject_id and str(subject_id) != "global":
            try:
                sid = uuid.UUID(str(subject_id))
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid subject_id UUID")

        if lookup_subject_type == "ai_model":
            try:
                _, _, alias = _subject_coordinates(
                    db, current_user.account_id, lookup_subject_type, sid
                )
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            legacy = crud_budget_policy.get_policies_for_subject(
                db,
                account_id=current_user.account_id,
                subject_type="ai_model",
                subject_id=sid,
            )
            account_policies = crud_budget_policy.get_policies_for_subject(
                db,
                account_id=current_user.account_id,
                subject_type="account",
                subject_id=None,
            )
            return _policies_to_response(
                db,
                [
                    *legacy,
                    *(
                        policy
                        for policy in account_policies
                        if policy.model_alias == alias
                    ),
                ],
            )
        policies = crud_budget_policy.get_policies_for_subject(
            db,
            account_id=current_user.account_id,
            subject_type=lookup_subject_type,
            subject_id=sid,
        )
        return _policies_to_response(db, policies)

    return _policies_to_response(
        db, crud_budget_policy.get_multi(db, account_id=str(current_user.account_id))
    )


@router.put(
    "/policies/{policy_id}",
    response_model=BudgetPolicyResponse,
)
@require_permission("manage_budgets")
def update_budget_policy(
    *,
    db: Session = Depends(get_db_session),
    policy_id: uuid.UUID,
    policy_in: BudgetPolicyUpdate,
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Update a budget policy."""
    ensure_permission_in_oss(db, current_user, "manage_budgets")
    policy = crud_budget_policy.get(
        db, id=policy_id, account_id=current_user.account_id
    )
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    if policy.subject_type == BUDGET_SUBJECT_TEAM:
        raise HTTPException(
            status_code=400,
            detail="Team budgets are available with the teams plugin",
        )
    if policy.subject_type == "flow":
        raise HTTPException(
            status_code=400,
            detail="This budget scope is not enforced; replace it with an account, model, agent or API-key policy",
        )
    update_data = policy_in.model_dump(exclude_unset=True)
    if policy.subject_type == "ai_model":
        try:
            subject_type, subject_id, alias = _subject_coordinates(
                db, current_user.account_id, "ai_model", policy.subject_id
            )
            update_data.update(
                subject_type=subject_type, subject_id=subject_id, model_alias=alias
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    authorize_budget_configuration(
        db, str(current_user.account_id), update_data, previous=policy
    )
    try:
        validate_budget_recipients(
            db, account_id=current_user.account_id, data=update_data
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    policy = crud_budget_policy.update(db, db_obj=policy, obj_in=update_data)
    return _policy_to_response(db, policy)


@router.delete("/policies/{policy_id}")
@require_permission("manage_budgets")
def delete_budget_policy(
    *,
    db: Session = Depends(get_db_session),
    policy_id: uuid.UUID,
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Delete a budget policy."""
    ensure_permission_in_oss(db, current_user, "manage_budgets")
    policy = crud_budget_policy.get(
        db, id=policy_id, account_id=current_user.account_id
    )
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    crud_budget_policy.remove(db, id=policy_id, account_id=str(current_user.account_id))
    return {"status": "ok"}
