"""Admin API for Anthropic gateway identity providers (#1414).

An account admin registers the organization's OpenID Connect issuer so that
Claude Desktop users signed in with that IdP can call ``/anthropic/v1/*``
with their IdP token. Account admin only: the same rule as granting the
``model_gateway:trusted_upstream`` scope (superuser, primary user, or a
holder of ``manage_account``), because both decide who may spend on the
account's gateway. Create, update and delete write audit rows.
"""

from __future__ import annotations

import asyncio
from typing import Any, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models import models
from preloop.models.crud import (
    crud_api_key,
    crud_audit_log,
    crud_gateway_identity_provider,
)
from preloop.models.db.session import get_db_session
from preloop.schemas.gateway_identity_provider import (
    GatewayIdentityProviderCreate,
    GatewayIdentityProviderRead,
    GatewayIdentityProviderTestResult,
    GatewayIdentityProviderUpdate,
)
from preloop.services import gateway_idp_auth

router = APIRouter(
    prefix="/account/gateway-identity-providers",
    tags=["Gateway Identity Providers"],
)

#: Only these provider fields may be cleared with an explicit ``null``.
NULLABLE_FIELDS = frozenset({"groups_claim", "allowed_groups"})

AUDIT_CREATED = "gateway_identity_provider_created"
AUDIT_UPDATED = "gateway_identity_provider_updated"
AUDIT_DELETED = "gateway_identity_provider_deleted"


def require_account_admin(
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> models.User:
    """403 unless the caller is an account admin."""
    from preloop.api.auth.router import _is_account_admin

    if not _is_account_admin(db, current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only account admins can manage gateway identity providers",
        )
    return current_user


def _read(provider: models.GatewayIdentityProvider) -> GatewayIdentityProviderRead:
    data = GatewayIdentityProviderRead.model_validate(provider)
    data.allow_private_network_issuer_effective = (
        gateway_idp_auth.private_issuers_allowed(provider)
    )
    return data


def _get_owned(
    db: Session, account_id: Any, provider_id: UUID
) -> models.GatewayIdentityProvider:
    provider = crud_gateway_identity_provider.get_in_account(
        db, provider_id=provider_id, account_id=account_id
    )
    if provider is None:
        raise HTTPException(status_code=404, detail="Identity provider not found")
    return provider


def _check_binding_key(db: Session, account_id: Any, api_key_id: UUID) -> None:
    """The binding key must be a live gateway-capable key of this account."""
    from preloop.api.auth.key_scopes import is_device_scoped_api_key

    key = crud_api_key.get(db, id=api_key_id)
    if (
        key is None
        or str(key.account_id) != str(account_id)
        or not key.is_active
        or key.is_expired
        or is_device_scoped_api_key(key)
    ):
        raise HTTPException(
            status_code=422,
            detail="api_key_id must be an active model gateway key of this account",
        )


def _check_unique(
    db: Session,
    *,
    issuer: str,
    audiences: list[str],
    api_key_id: UUID,
    exclude_provider_id: Any = None,
) -> None:
    taken = crud_gateway_identity_provider.audience_taken(
        db,
        issuer=issuer,
        audiences=audiences,
        exclude_provider_id=exclude_provider_id,
    )
    if taken:
        # Deliberately does not say which account holds it.
        raise HTTPException(
            status_code=409,
            detail=f"Audience already registered for this issuer: {', '.join(taken)}",
        )
    bound = crud_gateway_identity_provider.get_by_api_key(db, api_key_id=api_key_id)
    if bound is not None and bound.id != exclude_provider_id:
        raise HTTPException(
            status_code=409,
            detail="This API key is already the binding key of another provider",
        )


def _audit(
    db: Session,
    user: models.User,
    action: str,
    provider_id: Any,
    details: dict[str, Any],
) -> None:
    crud_audit_log.log_action(
        db,
        account_id=user.account_id,
        user_id=user.id,
        action=action,
        resource_type="gateway_identity_provider",
        resource_id=str(provider_id),
        status="success",
        details=details,
        commit=False,
    )


def _summary(provider: models.GatewayIdentityProvider) -> dict[str, Any]:
    return {
        "name": provider.name,
        "issuer": provider.issuer,
        "audiences": provider.audiences,
        "api_key_id": str(provider.api_key_id),
        "enabled": provider.enabled,
    }


@router.get("", response_model=List[GatewayIdentityProviderRead])
def list_gateway_identity_providers(
    current_user: models.User = Depends(require_account_admin),
    db: Session = Depends(get_db_session),
) -> List[GatewayIdentityProviderRead]:
    """List the account's identity providers."""
    return [
        _read(p)
        for p in crud_gateway_identity_provider.list_for_account(
            db, account_id=current_user.account_id
        )
    ]


@router.post(
    "",
    response_model=GatewayIdentityProviderRead,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "Issuer and audience or binding key taken"}},
)
def create_gateway_identity_provider(
    payload: GatewayIdentityProviderCreate,
    current_user: models.User = Depends(require_account_admin),
    db: Session = Depends(get_db_session),
) -> GatewayIdentityProviderRead:
    """Register an issuer; ``(issuer, audience)`` is unique across accounts."""
    account_id = current_user.account_id
    _check_binding_key(db, account_id, payload.api_key_id)
    _check_unique(
        db,
        issuer=payload.issuer,
        audiences=payload.audiences,
        api_key_id=payload.api_key_id,
    )
    fields = payload.model_dump(exclude={"audiences"})
    try:
        provider = crud_gateway_identity_provider.create_for_account(
            db,
            account_id=account_id,
            audiences=payload.audiences,
            fields=fields,
            commit=False,
        )
        _audit(db, current_user, AUDIT_CREATED, provider.id, _summary(provider))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="Issuer and audience already registered"
        ) from exc
    db.refresh(provider)
    gateway_idp_auth.default_jwks_cache.forget(provider.issuer)
    return _read(provider)


@router.patch(
    "/{provider_id}",
    response_model=GatewayIdentityProviderRead,
    responses={409: {"description": "Issuer and audience or binding key taken"}},
)
def update_gateway_identity_provider(
    provider_id: UUID,
    payload: GatewayIdentityProviderUpdate,
    current_user: models.User = Depends(require_account_admin),
    db: Session = Depends(get_db_session),
) -> GatewayIdentityProviderRead:
    """Change a provider; takes effect on the next request."""
    provider = _get_owned(db, current_user.account_id, provider_id)
    changes = payload.model_dump(exclude_unset=True)
    audiences = changes.pop("audiences", None)
    null_fields = sorted(
        name
        for name, value in payload.model_dump(exclude_unset=True).items()
        if value is None and name not in NULLABLE_FIELDS
    )
    if null_fields:
        raise HTTPException(
            status_code=422, detail=f"Fields cannot be null: {', '.join(null_fields)}"
        )
    if "api_key_id" in changes:
        _check_binding_key(db, current_user.account_id, changes["api_key_id"])
    before = _summary(provider)
    old_issuer = provider.issuer
    _check_unique(
        db,
        issuer=changes.get("issuer", provider.issuer),
        audiences=audiences if audiences is not None else provider.audiences,
        api_key_id=changes.get("api_key_id", provider.api_key_id),
        exclude_provider_id=provider.id,
    )
    try:
        provider = crud_gateway_identity_provider.update_provider(
            db, provider=provider, fields=changes, audiences=audiences, commit=False
        )
        after = _summary(provider)
        _audit(
            db,
            current_user,
            AUDIT_UPDATED,
            provider.id,
            {
                "changed_fields": sorted(
                    set(changes) | ({"audiences"} if audiences is not None else set())
                ),
                "before": before,
                "after": after,
            },
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="Issuer and audience already registered"
        ) from exc
    db.refresh(provider)
    gateway_idp_auth.default_jwks_cache.forget(old_issuer)
    gateway_idp_auth.default_jwks_cache.forget(provider.issuer)
    return _read(provider)


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_gateway_identity_provider(
    provider_id: UUID,
    current_user: models.User = Depends(require_account_admin),
    db: Session = Depends(get_db_session),
) -> None:
    """Remove a provider; its tokens stop working on the next request."""
    provider = _get_owned(db, current_user.account_id, provider_id)
    summary = _summary(provider)
    crud_gateway_identity_provider.delete_provider(db, provider=provider, commit=False)
    _audit(db, current_user, AUDIT_DELETED, provider_id, summary)
    db.commit()
    gateway_idp_auth.default_jwks_cache.forget(summary["issuer"])


@router.post("/{provider_id}/test", response_model=GatewayIdentityProviderTestResult)
def test_gateway_identity_provider(
    provider_id: UUID,
    current_user: models.User = Depends(require_account_admin),
    db: Session = Depends(get_db_session),
) -> GatewayIdentityProviderTestResult:
    """Fetch discovery and JWKS and list the key ids. No token is used."""
    provider = _get_owned(db, current_user.account_id, provider_id)
    issuer = provider.issuer
    allow_private = gateway_idp_auth.private_issuers_allowed(provider)
    extra_hosts = list(provider.allowed_jwks_hosts or [])
    db.close()  # release the connection before the outbound fetch
    # Sync handler: runs on a worker thread, so a private loop is safe.
    result = asyncio.run(
        gateway_idp_auth.probe_issuer(
            issuer, allow_private=allow_private, extra_jwks_hosts=extra_hosts
        )
    )
    return GatewayIdentityProviderTestResult.model_validate(result)
