"""Content-free authentication events shared by core and identity plugins."""

import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_audit_log, crud_person
from preloop.services.audit.service import AuditActionType

logger = logging.getLogger(__name__)

# An allowlist also excludes secrets nested under an innocuous field name.
_SAFE_FIELDS = frozenset(
    {
        "method",
        "provider",
        "reason_code",
        "person_id",
        "scim_token_prefix",
        "connection_id",
        "domain_id",
        "policy_id",
        "mapping_id",
        "target_person_id",
        "role",
        "group_id",
        "credential_id",
        "protocol",
        "enabled",
        "default_role",
        "jit_enabled",
        "team_id",
        "enforce",
        "session_max_age_seconds",
        "was_verified",
    }
)


def sanitize_auth_payload(
    details: Mapping[str, Any] | None,
) -> dict[str, str | int | bool]:
    """Keep only bounded scalar identity metadata; never retain protocol payloads."""
    return {
        key: (
            str(value)[: 12 if key == "scim_token_prefix" else 128]
            if isinstance(value, (str, UUID))
            else value
        )
        for key, value in (details or {}).items()
        if key in _SAFE_FIELDS and isinstance(value, (str, UUID, int, bool))
    }


def record_auth_event(
    db: Session,
    *,
    action: str,
    account_id: UUID | str | None = None,
    user: models.User | None = None,
    person_id: UUID | str | None = None,
    details: Mapping[str, Any] | None = None,
    source_ip: str | None = None,
    resource_type: str = "person",
    resource_id: UUID | str | None = None,
    commit: bool = True,
) -> None:
    """Write an account-scoped event, or a content-free unscoped security log.

    Failed attempts without an identifiable account (unknown password identity,
    unknown passkey or rejected OAuth state) cannot enter a tenant's audit trail.
    They emit the same action and reason code to the service security log. Never
    accept an account id from an unauthenticated request to route those events.
    ``user_id`` remains a membership FK; ``person_id`` identifies the person.
    With ``commit=False``, the caller owns the transaction and errors propagate.
    """
    clean = sanitize_auth_payload(details)
    if user is not None:
        account_id = user.account_id
        person_id = user.person_id
    if person_id is not None:
        clean["person_id"] = str(person_id)
    if account_id is None:
        logger.info(
            "Unscoped authentication event",
            extra={
                "auth_event": {
                    "action": action,
                    "details": clean,
                }
            },
        )
        return
    try:
        crud_audit_log.log_action(
            db,
            account_id=account_id,
            user_id=user.id if user is not None else None,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id or person_id)
            if resource_id or person_id
            else None,
            status="failure" if action.endswith((".failed", ".blocked")) else "success",
            ip_address=source_ip,
            details=clean,
            commit=commit,
        )
    except Exception:
        if not commit:
            raise
        db.rollback()
        # Do not echo DB exception text: it can include authentication material.
        logger.warning("Authentication audit write failed")


def login_subject(db: Session, person: models.Person | None) -> models.User | None:
    """Find a known person's audit destination even when their access is disabled."""
    if person is None:
        return None
    memberships = crud_person.list_memberships(db, person_id=person.id)
    for preferred in (
        person.last_active_user_id,
        getattr(person, "primary_user_id", None),
    ):
        for user in memberships:
            if user.id == preferred:
                return user
    return memberships[0] if memberships else None


def record_login_event(
    db: Session,
    *,
    user: models.User | None,
    method: str,
    succeeded: bool,
    reason_code: str | None = None,
    source_ip: str | None = None,
    provider: str | None = None,
) -> None:
    """Record exactly one outcome with a fixed reason code, never an exception."""
    record_auth_event(
        db,
        user=user,
        action=AuditActionType.AUTH_LOGIN_SUCCEEDED
        if succeeded
        else AuditActionType.AUTH_LOGIN_FAILED,
        details={"method": method, "reason_code": reason_code, "provider": provider},
        source_ip=source_ip,
    )
