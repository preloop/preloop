"""Subscription imports repair one shared lineage without replacing healthy grants."""

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_ai_model, crud_secret_reference
from preloop.schemas.ai_model import AIModelCreate, AIModelUpdate
from preloop.services.secret_service import get_secret_service

CLAUDE = "oauth_anthropic_claude_code"
CODEX = "oauth_openai_codex"


def payload(token: str) -> dict[str, Any]:
    """Return a synthetic future-dated grant."""
    return {
        "access": token,
        "refresh": token,
        "expires": 4102444800000,
        "account_id": "synthetic",
    }


def model(db: Session, account: models.Account, kind: str, name: str) -> models.AIModel:
    """Create a family with an independent synthetic grant."""
    return crud_ai_model.create_with_account(
        db=db,
        account_id=account.id,
        obj_in={
            "name": name,
            "provider_name": "anthropic" if kind == CLAUDE else "openai",
            "model_identifier": name,
            "credential_type": kind,
            "credential_payload": payload(name),
        },
    )


@pytest.mark.parametrize("kind", [CLAUDE, CODEX])
@pytest.mark.parametrize(
    "code", ["invalid_grant", "refresh_token_reused", "revoked", None]
)
@pytest.mark.parametrize("operation", ["create", "repoint", "reconnect"])
def test_repair_updates_shared_lineage(
    db_session: Session,
    create_account: Callable[..., models.Account],
    kind: str,
    code: str | None,
    operation: str,
) -> None:
    """Both old-client reconnect and new-family imports replace the shared bundle."""
    account = create_account()
    owner = model(db_session, account, kind, "owner")
    sibling = crud_ai_model.create_with_account(
        db=db_session,
        account_id=account.id,
        obj_in={
            "name": "sibling",
            "provider_name": owner.provider_name,
            "model_identifier": "sibling",
            "credentials_secret_id": owner.credentials_secret_id,
        },
    )
    secret = owner.credentials_secret
    secret_id = secret.id
    crud_secret_reference.update(
        db_session,
        db_obj=secret,
        obj_in={
            "status": "revoked" if code is None else "error",
            "meta_data": {
                "credential_type": kind,
                "last_refresh_code": code,
                "last_refresh_error": "synthetic",
            },
        },
    )
    update = {"credential_type": kind, "credential_payload": payload("fresh")}
    if operation != "reconnect":
        update["credentials_secret_id"] = secret_id
    if operation == "create":
        request = AIModelCreate(
            name="new-family",
            provider_name=owner.provider_name,
            model_identifier="new-family",
            **update,
        )
        repaired = crud_ai_model.create_with_account(
            db=db_session, account_id=account.id, obj_in=request.model_dump()
        )
    else:
        target = (
            model(db_session, account, kind, "target")
            if operation == "repoint"
            else owner
        )
        request = AIModelUpdate(**update)
        repaired = crud_ai_model.update(
            db=db_session, db_obj=target, obj_in=request.model_dump(exclude_unset=True)
        )
    db_session.expire_all()
    assert (
        repaired.credentials_secret_id
        == sibling.credentials_secret_id
        == owner.credentials_secret_id
        == secret_id
    )
    for row in (owner, sibling, repaired):
        resolved = get_secret_service().resolve_ai_model_credentials(
            row, db=db_session, allow_refresh=False
        )
        assert resolved.payload["refresh"] == "fresh"
        assert row.credentials_secret.status == "active"
        assert "last_refresh_code" not in row.credentials_secret.meta_data


@pytest.mark.parametrize("operation", ["create", "repoint"])
def test_legacy_link_to_failed_secret_names_exact_repair(
    db_session: Session,
    create_account: Callable[..., models.Account],
    operation: str,
) -> None:
    """A v1.0.2 link-only request cannot silently attach a known dead credential."""
    account = create_account()
    owner = model(db_session, account, CLAUDE, "owner")
    target = model(db_session, account, CLAUDE, "target")
    old_id = target.credentials_secret_id
    crud_secret_reference.update(
        db_session,
        db_obj=owner.credentials_secret,
        obj_in={
            "status": "error",
            "meta_data": {
                "credential_type": CLAUDE,
                "last_refresh_code": "invalid_grant",
            },
        },
    )
    with pytest.raises(
        ValueError, match='preloop agents reconnect "Claude Code" --from-local'
    ):
        if operation == "create":
            crud_ai_model.create_with_account(
                db=db_session,
                account_id=account.id,
                obj_in={
                    "name": "new",
                    "provider_name": "anthropic",
                    "model_identifier": "new",
                    "credentials_secret_id": owner.credentials_secret_id,
                },
            )
        else:
            crud_ai_model.update(
                db=db_session,
                db_obj=target,
                obj_in={"credentials_secret_id": owner.credentials_secret_id},
            )
    assert target.credentials_secret_id == old_id


@pytest.mark.parametrize("attach", [True, False])
def test_healthy_shared_secret_ignores_local_payload(
    db_session: Session,
    create_account: Callable[..., models.Account],
    attach: bool,
) -> None:
    """Even an unexpired local token must not roll back a live single-use lineage."""
    account = create_account()
    owner = model(db_session, account, CLAUDE, "owner")
    before = owner.credentials_secret.encrypted_value
    version = owner.credentials_secret.version
    if not attach:
        crud_ai_model.update(
            db=db_session,
            db_obj=owner,
            obj_in={
                "credential_type": CLAUDE,
                "credential_payload": payload("possibly-stale"),
            },
        )
        assert owner.credentials_secret.encrypted_value == before
        assert owner.credentials_secret.version == version
        return
    sibling = crud_ai_model.create_with_account(
        db=db_session,
        account_id=account.id,
        obj_in={
            "name": "new",
            "provider_name": "anthropic",
            "model_identifier": "new",
            "credentials_secret_id": owner.credentials_secret_id,
            "credential_type": CLAUDE,
            "credential_payload": payload("possibly-stale"),
        },
    )
    assert sibling.credentials_secret_id == owner.credentials_secret_id
    assert owner.credentials_secret.encrypted_value == before
    assert owner.credentials_secret.version == version


def test_payload_cannot_bypass_shared_secret_ownership(
    db_session: Session,
    create_account: Callable[..., models.Account],
) -> None:
    """Supplying material must still enforce account ownership before mutation."""
    owner = model(db_session, create_account(), CLAUDE, "owner")
    with pytest.raises(ValueError, match="different account"):
        crud_ai_model.create_with_account(
            db=db_session,
            account_id=create_account().id,
            obj_in={
                "name": "new",
                "provider_name": "anthropic",
                "model_identifier": "new",
                "credentials_secret_id": owner.credentials_secret_id,
                "credential_type": CLAUDE,
                "credential_payload": payload("foreign"),
            },
        )


@pytest.mark.parametrize("schema", [AIModelCreate, AIModelUpdate])
def test_expired_import_refused_before_storage(
    schema: type[AIModelCreate | AIModelUpdate],
) -> None:
    """An expired local credential receives a login instruction at the API boundary."""
    fields = (
        {"name": "new", "provider_name": "anthropic", "model_identifier": "new"}
        if schema is AIModelCreate
        else {}
    )
    with pytest.raises(ValueError, match="claude auth login --claudeai"):
        schema(
            **fields,
            credential_type=CLAUDE,
            credential_payload={**payload("dead"), "expires": 1700000000000},
        )
