"""Content-free authentication events shared by core and identity plugins."""

import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from fastapi import HTTPException
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
        "prefix",
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


# Existing SSO configuration identifiers retain their API schema lengths.
_SSO_IDENTIFIER_LIMITS = {
    "name": 255,
    "domain": 253,
    "idp_group": 512,
    "oidc_issuer": 512,
    "saml_idp_entity_id": 512,
}
_SSO_CHANGE_FIELDS = frozenset(
    {
        "name",
        "enabled",
        "oidc_issuer",
        "oidc_client_id",
        "oidc_client_secret",
        "oidc_scopes",
        "groups_claim",
        "saml_idp_entity_id",
        "saml_idp_sso_url",
        "saml_idp_metadata_url",
        "saml_idp_certs",
        "saml_sp_signing_key",
        "saml_want_assertions_signed",
        "default_role",
        "jit_enabled",
        "attribute_mapping",
    }
)
_MAX_AUDIT_OWNER_IDS = 100


def _identity_url(value: str, *, issuer: bool) -> str | None:
    """Retain URL identity while removing userinfo, queries and fragments."""
    try:
        parts = urlsplit(value)
        if issuer and parts.scheme not in {"https", "http"}:
            return None
        authority = ""
        if parts.netloc:
            host = parts.hostname
            if not host:
                return None
            authority = f"[{host}]" if ":" in host else host
            if parts.port is not None:
                authority += f":{parts.port}"
        elif issuer:
            return None
        # SAML entity IDs may also be URNs or opaque configured identifiers.
        return urlunsplit((parts.scheme, authority, parts.path, "", ""))
    except ValueError:
        return None


def sanitize_auth_payload(details: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep bounded identifiers, never credential values or nested payloads.

    Approved configuration labels intentionally survive even when their text
    resembles a secret field name (for example the group ``api_key_admins``).
    Changed-field lists contain known field *names*, including secret fields,
    never their values. Owner lists accept UUIDs only; excessive lists carry an
    explicit total alongside the bounded prefix rather than truncating silently.
    """
    clean: dict[str, Any] = {}
    for key, value in (details or {}).items():
        if key == "changed" and isinstance(value, (list, tuple)):
            clean[key] = sorted(
                {
                    item
                    for item in value[:128]
                    if isinstance(item, str) and item in _SSO_CHANGE_FIELDS
                }
            )
        elif key == "break_glass_owner_ids" and isinstance(value, (list, tuple)):
            owners = []
            for item in value[:_MAX_AUDIT_OWNER_IDS]:
                if isinstance(item, (str, UUID)):
                    try:
                        owners.append(str(UUID(str(item))))
                    except ValueError:
                        pass
            clean[key] = owners
            if len(value) > _MAX_AUDIT_OWNER_IDS:
                clean["break_glass_owner_ids_total"] = len(value)
        elif key in _SSO_IDENTIFIER_LIMITS and isinstance(value, str):
            if key in {"oidc_issuer", "saml_idp_entity_id"}:
                value = _identity_url(value, issuer=key == "oidc_issuer")
            if value is not None:
                clean[key] = value[: _SSO_IDENTIFIER_LIMITS[key]]
        elif key in _SAFE_FIELDS and isinstance(value, (str, UUID, int, bool)):
            clean[key] = (
                str(value)[: 12 if key in {"prefix", "scim_token_prefix"} else 128]
                if isinstance(value, (str, UUID))
                else value
            )
    return clean


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
            commit=False,
        )
        if commit:
            # The caller never consumes the audit row: avoid its refresh SELECT.
            db.commit()
            if user is not None:
                # Commit expires ORM rows. Load token fields in this worker, so
                # async login handlers do not issue a lazy SELECT on the loop.
                _ = user.id, user.auth_generation
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


def login_failure_reason(error: Exception, *, method: str) -> str:
    """Classify failures without retaining response text or exception payloads."""
    if not isinstance(error, HTTPException):
        return "internal_error"
    if error.status_code == 404:
        return f"{method}_disabled"
    if error.status_code == 400:
        return "challenge_rejected" if method == "passkey" else "invalid_request"
    if error.status_code == 401:
        return "invalid_credentials" if method == "password" else f"{method}_rejected"
    if error.status_code == 403:
        return "identity_or_policy_rejected"
    if error.status_code == 429:
        return "rate_limited"
    return "internal_error"
