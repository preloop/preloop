"""Atomic audited weekly email preference updates."""

from typing import Any
from sqlalchemy.orm import Session
from preloop.models import models
from preloop.models.crud import crud_audit_log, notification_preferences


def preferences(
    db: Session, *, account: models.Account, user: models.User
) -> dict[str, bool]:
    """Read defaults without creating preference rows."""
    prefs = notification_preferences.get_by_user(db, user.id)
    member_setting = account.weekly_member_report_enabled
    if member_setting is None:
        from preloop.plugins.base import get_plugin_manager

        default = get_plugin_manager().get_service("weekly_member_report_default")
        member_setting = (
            bool(default(db, account_id=str(account.id))) if default else False
        )
    return {
        "members_enabled": member_setting,
        "member_policy_findings_enabled": account.weekly_member_policy_findings_enabled,
        "account_enabled": account.weekly_report_enabled,
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
) -> dict[str, bool]:
    """Serialize account preference writes and commit the audit atomically."""
    account = (
        db.query(models.Account)
        .filter(models.Account.id == account_id)
        .with_for_update()
        .one()
    )
    before = preferences(db, account=account, user=user)
    if account_enabled is not None:
        account.weekly_report_enabled = account_enabled
    if members_enabled is not None:
        account.weekly_member_report_enabled = members_enabled
    if member_policy_findings_enabled is not None:
        account.weekly_member_policy_findings_enabled = member_policy_findings_enabled
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
    db.commit()
    return after
