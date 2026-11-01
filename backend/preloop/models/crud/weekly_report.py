"""Atomic audited weekly email preference updates."""

from typing import Any
from uuid import UUID
from sqlalchemy.orm import Session
from preloop.models import models
from preloop.models.crud import crud_audit_log, notification_preferences


def preferences(
    db: Session, *, account: models.Account, user: models.User
) -> dict[str, Any]:
    """Read defaults without creating preference rows."""
    prefs = notification_preferences.get_by_user(db, user.id)
    member_setting = account.weekly_member_report_enabled
    if member_setting is None:
        from preloop.plugins.base import get_plugin_manager

        default = get_plugin_manager().get_service("weekly_member_report_default")
        member_setting = (
            bool(default(db, account_id=str(account.id))) if default else False
        )
    synthesis = (account.meta_data or {}).get("weekly_exception_synthesis") or {}
    return {
        "synthesis_enabled": synthesis.get("enabled") is True,
        "synthesis_model_id": synthesis.get("model_id"),
        "members_enabled": member_setting,
        "member_policy_findings_enabled": account.weekly_member_policy_findings_enabled,
        "account_enabled": account.weekly_report_enabled,
        "tracking_enabled": (account.meta_data or {}).get(
            "report_engagement_tracking", True
        )
        is not False,
        "user_enabled": prefs.weekly_report_enabled if prefs else True,
    }


def update_preferences(
    db: Session,
    *,
    account_id: Any,
    user: models.User,
    account_enabled: bool | None,
    user_enabled: bool | None,
    members_enabled: bool | None = None,
    member_policy_findings_enabled: bool | None = None,
    tracking_enabled: bool | None = None,
    synthesis_enabled: bool | None = None,
    synthesis_model_id: UUID | None = None,
    synthesis_model_sent: bool = False,
) -> dict[str, Any]:
    """Serialize account preference writes and commit the audit atomically."""
    account = (
        db.query(models.Account)
        .filter(models.Account.id == account_id)
        .with_for_update()
        .one()
    )
    before = preferences(db, account=account, user=user)
    if synthesis_enabled is not None or synthesis_model_sent:
        from . import crud_ai_model

        if (
            synthesis_model_sent
            and synthesis_model_id is not None
            and crud_ai_model.get_for_account(
                db, id=synthesis_model_id, account_id=account.id
            )
            is None
        ):
            raise ValueError("Synthesis model not found in account")
        synthesis = dict(
            (account.meta_data or {}).get("weekly_exception_synthesis") or {}
        )
        if synthesis_enabled is not None:
            synthesis["enabled"] = synthesis_enabled
        if synthesis_model_sent:
            synthesis["model_id"] = (
                str(synthesis_model_id) if synthesis_model_id else None
            )
        account.meta_data = {
            **(account.meta_data or {}),
            "weekly_exception_synthesis": synthesis,
        }
    if account_enabled is not None:
        account.weekly_report_enabled = account_enabled
    if members_enabled is not None:
        account.weekly_member_report_enabled = members_enabled
    if member_policy_findings_enabled is not None:
        account.weekly_member_policy_findings_enabled = member_policy_findings_enabled
    if tracking_enabled is not None:
        account.meta_data = {
            **(account.meta_data or {}),
            "report_engagement_tracking": tracking_enabled,
        }
    if user_enabled is not None:
        prefs = notification_preferences.get_or_create(db, user.id)
        prefs.weekly_report_enabled = user_enabled
    db.flush()
    after = preferences(db, account=account, user=user)
    for key, value in after.items():
        if value != before[key]:
            crud_audit_log.log_action(
                db,
                account_id=account.id,
                user_id=user.id,
                action="weekly_report_preferences_updated",
                resource_type="account_settings"
                if key != "user_enabled"
                else "notification_preferences",
                resource_id=str(account.id if key != "user_enabled" else user.id),
                status="success",
                details={"field": key, "from": before[key], "to": value},
                commit=False,
            )
    if before["user_enabled"] and not after["user_enabled"]:
        from preloop.models.crud.admin_analytics import record_unsubscribe

        record_unsubscribe(db, account_id=account.id, user_id=user.id)
    db.commit()
    return after
