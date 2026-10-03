"""Tenant ownership checks for configurable budget subjects."""

from typing import Any
from sqlalchemy import select, or_
from sqlalchemy.orm import Session
from preloop.models import models
from preloop.models.models.access_values import RESERVED_BUDGET_SUBJECTS


def validate_budget_subject(
    db: Session, *, account_id: Any, subject_type: str, subject_id: Any
) -> Any:
    if subject_type in RESERVED_BUDGET_SUBJECTS:
        # Owned by a plugin (team budgets, account hierarchy), which validates
        # and enforces the subject behind its own endpoints.
        raise ValueError(
            f"Budget subject '{subject_type}' is available with the plugin "
            "that provides it, not on this endpoint"
        )
    if subject_type == "account":
        if subject_id is not None:
            raise ValueError("Account policies must not specify a subject id")
        return None
    subjects = {
        "api_key": models.ApiKey,
        "managed_agent": models.ManagedAgent,
        "ai_model": models.AIModel,
        "user": models.User,
    }
    model = subjects.get(subject_type)
    if model is None or subject_id is None:
        raise ValueError("A supported budget subject and its id are required")
    account_filter = model.account_id == account_id
    if subject_type == "ai_model":
        account_filter = or_(account_filter, model.account_id.is_(None))
    subject = db.execute(
        select(model).where(model.id == subject_id, account_filter)
    ).scalar_one_or_none()
    if subject is None:
        raise ValueError("Budget subject was not found in this account")
    return subject


def validate_budget_recipients(
    db: Session, *, account_id: Any, data: dict[str, Any]
) -> None:
    for field, model in (
        ("notification_user_ids", models.User),
        ("notification_team_ids", models.Team),
    ):
        ids = set(data.get(field) or [])
        if ids:
            found = {
                str(row[0])
                for row in db.execute(
                    select(model.id).where(
                        model.id.in_(ids), model.account_id == account_id
                    )
                ).all()
            }
            if found != set(map(str, ids)):
                raise ValueError("Notification recipients must belong to this account")
