"""CRUD coverage for Azure auth normalization on partial updates.

``_normalize_azure_auth_fields`` re-runs when a write omits ``provider_name``
and when a provider switch has to normalize a deep copy of the stored meta.
"""

import uuid

from sqlalchemy.orm import Session

from preloop.models.crud import crud_ai_model


def _entra_meta() -> dict:
    return {
        "provider_runtime": {
            "azure_auth": "entra",
            "azure_client_id": "11111111-2222-3333-4444-555555555555",
            "api_version": "2024-10-21",
            "region": "eastus",
        }
    }


def _create_entra_model(db_session: Session, account) -> object:
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"Azure chat {uuid.uuid4()}",
            "provider_name": "azure",
            "model_identifier": "chat-deployment",
            "api_endpoint": "https://example-resource.openai.azure.com",
            "meta_data": _entra_meta(),
        },
        account_id=account.id,
    )


def test_partial_update_without_provider_keeps_ambient_credentials(
    db_session: Session, create_account
) -> None:
    """meta_data without provider_name is normalized against the stored provider."""
    account = create_account()
    model = _create_entra_model(db_session, account)

    updated = crud_ai_model.update(
        db=db_session,
        db_obj=model,
        obj_in={
            "meta_data": {
                "provider_runtime": {
                    "azure_auth": "entra",
                    "azure_client_id": "11111111-2222-3333-4444-555555555555",
                    "api_version": "2025-01-01",
                }
            }
        },
    )

    runtime = updated.meta_data["provider_runtime"]
    assert updated.provider_name == "azure"
    assert runtime["azure_auth"] == "entra"
    assert runtime["ambient_credentials"] is True
    assert runtime["azure_client_id"] == "11111111-2222-3333-4444-555555555555"


def test_switch_azure_to_openai_clears_entra_fields(
    db_session: Session, create_account
) -> None:
    """A provider switch drops Entra keys carried in the update payload."""
    account = create_account()
    model = _create_entra_model(db_session, account)

    updated = crud_ai_model.update(
        db=db_session,
        db_obj=model,
        obj_in={
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "meta_data": _entra_meta(),
        },
    )

    runtime = updated.meta_data["provider_runtime"]
    assert updated.provider_name == "openai"
    assert "azure_auth" not in runtime
    assert "azure_client_id" not in runtime
    assert "ambient_credentials" not in runtime
    assert runtime["region"] == "eastus"


def test_switch_without_meta_data_normalizes_deep_copied_meta(
    db_session: Session, create_account
) -> None:
    """Switching provider with no meta_data still normalizes a copy of the row."""
    account = create_account()
    model = _create_entra_model(db_session, account)
    stored = model.meta_data
    assert stored["provider_runtime"]["azure_auth"] == "entra"
    assert stored["provider_runtime"]["ambient_credentials"] is True

    updated = crud_ai_model.update(
        db=db_session,
        db_obj=model,
        obj_in={"provider_name": "openai", "model_identifier": "gpt-5.4"},
    )

    runtime = updated.meta_data["provider_runtime"]
    assert stored["provider_runtime"]["azure_auth"] == "entra"
    assert stored["provider_runtime"]["ambient_credentials"] is True
    assert "azure_auth" not in runtime
    assert "azure_client_id" not in runtime
    assert "ambient_credentials" not in runtime
    assert runtime["region"] == "eastus"
    assert updated.meta_data is not stored
