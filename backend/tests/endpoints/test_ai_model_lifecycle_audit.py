"""Stored audit rows identify model and secret changes without key material."""

import json
from typing import Any

import pytest
from sqlalchemy.orm import Session

from preloop.api.endpoints import ai_models
from preloop.models import crud, models
from preloop.schemas.ai_model import (
    AIModelCreate,
    AIModelUpdate,
    AIModelCredentialRotateRequest,
)
from tests.conftest import maybe_await

KEY = "sk-synthetic-credential-never-allowed-in-audit"
REPLACEMENT = "sk-synthetic-replacement-never-allowed-in-audit"


@pytest.mark.asyncio
async def test_model_and_credential_lifecycle_audit(
    db_session: Session, test_user: models.User
) -> None:
    """Every lifecycle operation has one event with only safe state."""
    model = await maybe_await(
        ai_models.create_ai_model(
            ai_model_in=AIModelCreate(
                name="Review model", provider_name="openai", model_identifier="gpt-5.4"
            ),
            db=db_session,
            current_user=test_user,
        )
    )
    await maybe_await(
        ai_models.update_ai_model(
            model_id=model.id,
            ai_model_in=AIModelUpdate(name="Renamed model"),
            db=db_session,
            current_user=test_user,
        )
    )
    await maybe_await(
        ai_models.update_ai_model(
            model_id=model.id,
            ai_model_in=AIModelUpdate(api_key=KEY),
            db=db_session,
            current_user=test_user,
        )
    )
    await maybe_await(
        ai_models.update_ai_model(
            model_id=model.id,
            ai_model_in=AIModelUpdate(api_key=REPLACEMENT),
            db=db_session,
            current_user=test_user,
        )
    )
    await maybe_await(
        ai_models.rotate_ai_model_credential(
            model_id=model.id,
            body=AIModelCredentialRotateRequest(api_key=KEY),
            db=db_session,
            current_user=test_user,
        )
    )
    await maybe_await(
        ai_models.remove_ai_model_credential(
            model_id=model.id, db=db_session, current_user=test_user
        )
    )
    await maybe_await(
        ai_models.delete_ai_model(
            model_id=model.id, db=db_session, current_user=test_user
        )
    )
    entries = crud.crud_audit_log.get_by_account(
        db_session, account_id=test_user.account_id, action="configuration_change"
    )
    details: list[dict[str, Any]] = [entry.details for entry in entries]
    serialized = json.dumps(details)
    assert KEY not in serialized and REPLACEMENT not in serialized
    for action in ["set", "replaced", "rotated", "removed"]:
        matching = [
            d
            for d in details
            if d["config_type"] == "secret_reference" and d["action"] == action
        ]
        assert len(matching) == 1, details
        state = (
            matching[0].get("old_value")
            if action == "removed"
            else matching[0]["new_value"]
        )
        assert state["reference_id"]
        assert state["backend_type"] == "local_encrypted"
        assert state["version"] >= 1
        assert state["shared_model_count"] == 1
    assert len(details) == 7
    for action in ["created", "deleted"]:
        assert (
            len(
                [
                    d
                    for d in details
                    if d["config_type"] == "ai_model" and d["action"] == action
                ]
            )
            == 1
        )
    renamed = next(
        d for d in details if d.get("new_value", {}).get("changed_fields") == ["name"]
    )
    assert renamed["old_value"]["name"] == "Review model"
    assert renamed["new_value"]["name"] == "Renamed model"


@pytest.mark.asyncio
async def test_bulk_sync_audits_each_created_model_and_shared_credential(
    db_session: Session,
    test_user: models.User,
) -> None:
    """Bulk imports retain each model id and a material-free shared reference."""
    from fastapi import Request
    from unittest.mock import AsyncMock, patch
    from preloop.services.ai_model_catalog_sync import (
        CatalogSyncSummary,
        ProviderCatalogSyncResult,
    )
    from tests.services.test_model_credential_health import _stored_model

    model = _stored_model(db_session, test_user.account_id)
    db_session.commit()
    summary = CatalogSyncSummary(
        providers=[
            ProviderCatalogSyncResult(
                provider="openai",
                added=["openai/gpt-5.4"],
                added_model_ids=[str(model.id)],
            )
        ]
    )
    with patch.object(
        ai_models, "sync_account_model_catalog", new=AsyncMock(return_value=summary)
    ):
        await ai_models.sync_ai_model_catalog(
            request=Request(
                {"type": "http", "client": ("127.0.0.1", 123), "headers": []}
            ),
            db=db_session,
            current_user=test_user,
        )
    rows = crud.crud_audit_log.get_by_account(
        db_session, account_id=test_user.account_id, action="configuration_change"
    )
    assert len(rows) == 2
    details = [row.details for row in rows]
    created = next(row for row in details if row["action"] == "created")
    assert created["new_value"]["id"] == str(model.id)
    assert created["new_value"]["reference_id"] == str(model.credentials_secret_id)
    credential = next(row for row in details if row["action"] == "set")
    assert credential["new_value"]["shared_model_count"] == 1


@pytest.mark.asyncio
async def test_credential_export_refresh_is_audited_without_oauth_material(
    db_session: Session,
    test_user: models.User,
) -> None:
    """Export may replace an expiring bundle; both operations keep only identity."""
    from unittest.mock import patch
    from preloop.services.secret_service import ResolvedModelCredentials
    from tests.services.test_model_credential_health import _stored_model

    model = _stored_model(db_session, test_user.account_id)
    db_session.commit()
    old_version = model.credentials_version

    def refresh(*args: Any, **kwargs: Any) -> ResolvedModelCredentials:
        """Simulate a successful provider refresh through the ordinary CRUD write."""
        crud.crud_ai_model.update(
            db_session,
            db_obj=model,
            obj_in={
                "credential_type": "oauth_openai_codex",
                "credential_payload": {"access": KEY, "refresh": REPLACEMENT},
            },
        )
        return ResolvedModelCredentials(
            credential_type="oauth_openai_codex",
            backend_type="local_encrypted",
            payload={"access": KEY, "refresh": REPLACEMENT},
            secret_reference_id=model.credentials_secret_id,
        )

    with patch.object(ai_models, "get_secret_service") as service:
        service.return_value.resolve_ai_model_credentials.side_effect = refresh
        await maybe_await(
            ai_models.export_ai_model_credentials(
                model_id=model.id, db=db_session, current_user=test_user
            )
        )
    rows = crud.crud_audit_log.get_by_account(
        db_session, account_id=test_user.account_id, action="configuration_change"
    )
    assert len(rows) == 2
    details = [row.details for row in rows]
    assert {row["action"] for row in details} == {"refreshed", "exported"}
    assert KEY not in json.dumps(details) and REPLACEMENT not in json.dumps(details)
    refreshed = next(row for row in details if row["action"] == "refreshed")
    assert refreshed["old_value"]["version"] == old_version
    assert refreshed["new_value"]["version"] > old_version
