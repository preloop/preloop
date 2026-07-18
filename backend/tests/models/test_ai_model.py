"""Tests for AIModel model and CRUD operations."""

import json

from sqlalchemy.orm import Session

from preloop.models.crud import ai_model as ai_model_crud_module
from preloop.models.models import Account
from preloop.models.models.secret_reference import SecretReference
from preloop.models.crud import crud_ai_model
from preloop.schemas.ai_model import AIModelRead
from preloop.services import secret_service as secret_service_module
from preloop.services.secret_service import SecretService, VAULT_KV_V2_BACKEND


def test_create_ai_model(db_session: Session, create_account):
    """Test creating an AIModel instance."""
    account: Account = create_account()

    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Test OpenAI Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_endpoint": "https://api.openai.com/v1",
            "api_key": "test_key_123",
            "is_default": True,
        },
        account_id=account.id,
    )

    assert ai_model is not None
    assert ai_model.provider_name == "openai"
    assert ai_model.model_identifier == "gpt-5.4"
    assert ai_model.api_endpoint == "https://api.openai.com/v1"
    assert ai_model.api_key is None
    assert ai_model.credentials_secret_id is not None
    assert ai_model.has_api_key is True
    assert ai_model.credentials_backend_type == "local_encrypted"
    assert ai_model.is_default is True
    assert ai_model.account_id == account.id


def test_audio_model_kind_is_stored_in_metadata_and_has_separate_defaults(
    db_session: Session, create_account
):
    """Audio model defaults should not displace the LLM default."""
    account: Account = create_account()

    llm_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Default Chat",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4-mini",
            "api_key": "llm-key",
            "model_kind": "llm",
            "is_default": True,
        },
        account_id=account.id,
    )
    stt_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Default STT",
            "provider_name": "openai",
            "model_identifier": "whisper-1",
            "api_key": "stt-key",
            "model_kind": "stt",
            "is_default": True,
        },
        account_id=account.id,
    )

    assert llm_model.model_kind == "llm"
    assert stt_model.model_kind == "stt"
    assert stt_model.meta_data["service_kind"] == "stt"
    assert (
        crud_ai_model.get_default_active_model(db_session, account_id=account.id).id
        == llm_model.id
    )
    assert (
        crud_ai_model.get_default_active_model(
            db_session, account_id=account.id, model_kind="stt"
        ).id
        == stt_model.id
    )


def test_ai_model_with_ambient_credentials_counts_as_configured(
    db_session: Session, create_account
):
    """Ambient provider credentials should surface as configured."""
    account: Account = create_account()

    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Ambient Bedrock Model",
            "provider_name": "bedrock",
            "model_identifier": "us.anthropic.claude-opus-4-6-v1",
            "meta_data": {
                "provider_runtime": {
                    "ambient_credentials": True,
                    "region": "us-east-1",
                }
            },
        },
        account_id=account.id,
    )

    assert ai_model.credentials_secret_id is None
    assert ai_model.uses_ambient_credentials is True
    assert ai_model.has_api_key is True
    assert ai_model.credentials_backend_type == "ambient_provider"

    response_model = AIModelRead.model_validate(ai_model)
    assert response_model.has_api_key is True
    assert response_model.credentials_backend_type == "ambient_provider"


def test_create_system_default_ai_model_with_secret_reference(db_session: Session):
    """System-wide default models should support secret-backed credentials."""
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "System Default Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4-mini",
            "api_key": "system-default-secret",
            "is_default": True,
        },
        account_id=None,
    )

    assert ai_model.account_id is None
    assert ai_model.api_key is None
    assert ai_model.credentials_secret_id is not None
    assert ai_model.credentials_secret is not None
    assert ai_model.credentials_secret.account_id is None
    assert ai_model.credentials_secret.secret_kind == "ai_model_api_key"


def test_create_ai_model_with_external_secret_reference(
    db_session: Session, create_account, monkeypatch
):
    """External secret references should be stored and exposed on the model."""
    account: Account = create_account()
    monkeypatch.setattr(secret_service_module.settings.vault_kv_v2, "enabled", True)
    monkeypatch.setattr(
        secret_service_module.settings.vault_kv_v2,
        "url",
        "https://vault.example.test",
    )
    monkeypatch.setattr(
        secret_service_module.settings.vault_kv_v2, "token", "test-token"
    )
    monkeypatch.setattr(secret_service_module.settings.vault_kv_v2, "mount", "kv")

    monkeypatch.setattr(
        ai_model_crud_module,
        "get_secret_service",
        lambda: SecretService(),
    )

    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "External Secret Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "credentials_backend_type": VAULT_KV_V2_BACKEND,
            "credentials_external_ref": "providers/openai/team-a",
            "credentials_meta_data": {"field": "api_key", "version": 4},
            "is_default": False,
        },
        account_id=account.id,
    )

    assert ai_model.api_key is None
    assert ai_model.credentials_secret_id is not None
    assert ai_model.has_api_key is True
    assert ai_model.credentials_backend_type == VAULT_KV_V2_BACKEND
    assert ai_model.credentials_external_ref == "providers/openai/team-a"
    assert ai_model.credentials_secret is not None
    assert ai_model.credentials_secret.backend_type == VAULT_KV_V2_BACKEND
    assert ai_model.credentials_secret.external_ref == "providers/openai/team-a"
    assert ai_model.credentials_secret.encrypted_value is None

    response_model = AIModelRead.model_validate(ai_model)
    assert response_model.credentials_backend_type == VAULT_KV_V2_BACKEND
    assert response_model.credentials_external_ref == "providers/openai/team-a"
    assert response_model.has_api_key is True


def test_create_ai_model_with_structured_credentials(
    db_session: Session, create_account
):
    """Structured inline credentials should be encrypted via SecretReference."""
    account: Account = create_account()

    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Codex OAuth Model",
            "provider_name": "openai-codex",
            "model_identifier": "gpt-5.4",
            "credential_type": "oauth_openai_codex",
            "credential_payload": {
                "access": "access-token",
                "refresh": "refresh-token",
                "expires": 1893456000000,
                "account_id": "acct-123",
            },
        },
        account_id=account.id,
    )

    assert ai_model.api_key is None
    assert ai_model.credentials_secret_id is not None
    assert ai_model.credentials_secret is not None
    assert ai_model.credentials_secret.secret_kind == "ai_model_credentials"
    assert ai_model.credential_type == "oauth_openai_codex"

    stored = json.loads(
        SecretService().resolve_secret_reference(ai_model.credentials_secret).value
    )
    assert stored["type"] == "oauth_openai_codex"
    assert stored["account_id"] == "acct-123"

    response_model = AIModelRead.model_validate(ai_model)
    assert response_model.credential_type == "oauth_openai_codex"
    assert response_model.has_api_key is True


def test_get_ai_models_by_account(db_session: Session, create_account):
    """Test retrieving AIModels for a specific account."""
    account1: Account = create_account()
    account2: Account = create_account()

    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Test OpenAI Model Account1",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_endpoint": "https://api.openai.com/v1",
            "api_key": "test_key_123",
            "is_default": True,
        },
        account_id=account1.id,
    )
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Test OpenAI Model Account1",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_endpoint": "https://api.openai.com/v1",
            "api_key": "test_key_123",
        },
        account_id=account1.id,
    )
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Test Anthropic Model Account2",
            "provider_name": "anthropic",
            "model_identifier": "claude-2",
            "api_endpoint": "https://api.anthropic.com/v1",
            "api_key": "test_key_456",
        },
        account_id=account2.id,
    )

    models_acc1 = crud_ai_model.get_by_account(db=db_session, account_id=account1.id)
    models_acc2 = crud_ai_model.get_by_account(db=db_session, account_id=account2.id)

    assert len(models_acc1) == 2
    assert len(models_acc2) == 1
    assert models_acc1[0].provider_name in ["openai", "openai"]
    assert models_acc2[0].provider_name == "anthropic"


def test_update_ai_model_and_default_logic(db_session: Session, create_account):
    """Test updating an AIModel and the default model logic."""
    account: Account = create_account()
    model1 = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Default Test Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_endpoint": "https://api.openai.com/v1",
            "api_key": "test_key_123",
            "is_default": True,
        },
        account_id=account.id,
    )
    model2 = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Non-Default Test Model",
            "provider_name": "anthropic",
            "model_identifier": "claude-instant-1",
            "api_endpoint": "https://api.anthropic.com/v1",
            "api_key": "test_key_456",
            "is_default": False,
        },
        account_id=account.id,
    )

    assert model1.is_default is True
    assert model2.is_default is False
    assert model2.credentials_secret_id is not None

    # Update m2 to be default, m1 should become non-default
    updated_m2 = crud_ai_model.update(
        db=db_session, db_obj=model2, obj_in={"is_default": True}
    )
    db_session.refresh(model1)  # Refresh m1 to get its updated state from the DB

    assert updated_m2.is_default is True
    assert updated_m2.api_key is None
    assert updated_m2.has_api_key is True
    assert model1.is_default is False

    # Update m1 to be default again
    updated_m1 = crud_ai_model.update(
        db=db_session, db_obj=model1, obj_in={"is_default": True}
    )
    db_session.refresh(updated_m1)

    assert updated_m1.is_default is True
    assert updated_m2.is_default is False

    # Test setting a model to non-default
    still_default_m1 = crud_ai_model.update(
        db=db_session, db_obj=updated_m1, obj_in={"is_default": False}
    )
    assert still_default_m1.is_default is False


def test_get_default_ai_model(db_session: Session, create_account):
    """Test retrieving the default AIModel for an account."""
    account: Account = create_account()
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Non-Default Model for Get Default Test",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_endpoint": "https://api.openai.com/v1",
            "api_key": "test_key_123",
            "is_default": False,
        },
        account_id=account.id,
    )
    default_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Actual Default Model for Get Default Test",
            "provider_name": "anthropic",
            "model_identifier": "claude-2",
            "api_endpoint": "https://api.anthropic.com/v1",
            "api_key": "test_key_456",
            "is_default": True,
        },
        account_id=account.id,
    )

    retrieved_default = crud_ai_model.get_default_active_model(
        db=db_session, account_id=account.id
    )
    assert retrieved_default is not None
    assert retrieved_default.id == default_model.id
    assert retrieved_default.model_identifier == "claude-2"

    # With nothing flagged as default, the first BYOK/API-key-backed model
    # wins rather than resolving to nothing. See
    # tests/models/crud/test_ai_model_default_selection.py for the full rule
    # (principal-bound OAuth models are never auto-selected).
    account2: Account = create_account()
    only_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Non-Default Model for Get Default Test",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_endpoint": "https://api.openai.com/v1",
            "api_key": "test_key_123",
            "is_default": False,
        },
        account_id=account2.id,
    )
    fallback_default = crud_ai_model.get_default_active_model(
        db=db_session, account_id=account2.id
    )
    assert fallback_default is not None
    assert fallback_default.id == only_model.id


def test_delete_ai_model(db_session: Session, create_account):
    """Test deleting an AIModel."""
    account: Account = create_account()
    model_to_delete = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Model to Delete",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_endpoint": "https://api.openai.com/v1",
            "api_key": "test_key_123",
            "is_default": False,
        },
        account_id=account.id,
    )
    model_id = model_to_delete.id
    secret_id = model_to_delete.credentials_secret_id
    assert secret_id is not None

    crud_ai_model.remove(db=db_session, id=model_id)

    retrieved_after_delete = crud_ai_model.get(db=db_session, id=model_id)
    assert retrieved_after_delete is None
    assert db_session.get(SecretReference, secret_id) is None

    # Ensure other models for the same account are not affected
    surviving_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Surviving Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_endpoint": "https://api.openai.com/v1",
            "api_key": "test_key_123",
            "is_default": False,
        },
        account_id=account.id,
    )
    assert crud_ai_model.get(db=db_session, id=surviving_model.id) is not None


def test_delete_ai_model_preserves_shared_secret_reference(
    db_session: Session, create_account
):
    """Deleting one model should not remove a secret still referenced elsewhere."""
    account: Account = create_account()
    primary_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Primary Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_key": "shared-secret",
            "is_default": False,
        },
        account_id=account.id,
    )
    shared_secret_id = primary_model.credentials_secret_id
    assert shared_secret_id is not None

    secondary_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Secondary Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "is_default": False,
        },
        account_id=account.id,
    )
    secondary_model.credentials_secret_id = shared_secret_id
    db_session.add(secondary_model)
    db_session.commit()

    crud_ai_model.remove(db=db_session, id=primary_model.id)

    assert crud_ai_model.get(db=db_session, id=secondary_model.id) is not None
    assert db_session.get(SecretReference, shared_secret_id) is not None


def test_default_model_exists(db_session: Session):
    """Test checking if a system-wide default model exists."""
    # Check the method returns a boolean
    result = crud_ai_model.default_model_exists(db=db_session)
    assert isinstance(result, bool)

    # Ensure a system-wide default model exists for test coverage
    from preloop.models.models.ai_model import AIModel

    default_model = AIModel(
        name="System Default Test",
        provider_name="openai",
        model_identifier="gpt-5.4-test",
        api_endpoint="https://api.openai.com/v1",
        api_key="test_key",
        is_default=True,
        account_id=None,
    )
    db_session.add(default_model)
    db_session.commit()

    # Verify a default exists
    assert crud_ai_model.default_model_exists(db=db_session) is True


def test_normalize_model_kind_fields_does_not_mutate_caller_dict():
    """model_kind normalization should return a new dict without mutating input."""
    obj_data = {
        "name": "Whisper",
        "model_kind": "stt",
        "provider_name": "openai",
        "meta_data": {"region": "us"},
    }
    original = dict(obj_data)
    original_meta = obj_data["meta_data"]

    normalized = ai_model_crud_module.CRUDAIModel._normalize_model_kind_fields(obj_data)

    assert obj_data == original
    assert "model_kind" in obj_data
    assert "model_kind" not in normalized
    assert normalized["name"] == "Whisper"
    assert normalized["provider_name"] == "openai"
    assert normalized["meta_data"]["service_kind"] == "stt"
    assert normalized["meta_data"]["region"] == "us"
    assert original_meta["region"] == "us"


def test_create_models_sharing_one_credentials_secret(
    db_session: Session, create_account
):
    """One provider key should back several models via credentials_secret_id."""
    account: Account = create_account()

    first = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Claude Sonnet",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "one-anthropic-key",
        },
        account_id=account.id,
    )
    shared_secret_id = first.credentials_secret_id
    assert shared_secret_id is not None

    second = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Claude Haiku",
            "provider_name": "anthropic",
            "model_identifier": "claude-haiku-4-5",
            "credentials_secret_id": shared_secret_id,
        },
        account_id=account.id,
    )

    assert second.credentials_secret_id == shared_secret_id
    assert second.api_key is None
    # Reuse must not mint a second secret for the same key.
    secrets = (
        db_session.query(SecretReference)
        .filter(SecretReference.account_id == account.id)
        .all()
    )
    assert len(secrets) == 1


def test_shared_secret_survives_until_last_model_is_deleted(
    db_session: Session, create_account
):
    """Deleting one of N shared models keeps the key; the last deletion reaps it."""
    account: Account = create_account()

    first = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Model A",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "one-anthropic-key",
        },
        account_id=account.id,
    )
    shared_secret_id = first.credentials_secret_id
    second = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Model B",
            "provider_name": "anthropic",
            "model_identifier": "claude-haiku-4-5",
            "credentials_secret_id": shared_secret_id,
        },
        account_id=account.id,
    )
    third = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Model C",
            "provider_name": "anthropic",
            "model_identifier": "claude-opus-4-1",
            "credentials_secret_id": shared_secret_id,
        },
        account_id=account.id,
    )

    crud_ai_model.remove(db=db_session, id=first.id)
    assert db_session.get(SecretReference, shared_secret_id) is not None
    assert crud_ai_model.get(db=db_session, id=second.id) is not None
    assert crud_ai_model.get(db=db_session, id=third.id) is not None

    crud_ai_model.remove(db=db_session, id=second.id)
    assert db_session.get(SecretReference, shared_secret_id) is not None

    crud_ai_model.remove(db=db_session, id=third.id)
    assert db_session.get(SecretReference, shared_secret_id) is None


def test_cannot_attach_secret_belonging_to_another_account(
    db_session: Session, create_account
):
    """A model must not be able to borrow another account's provider key."""
    owner: Account = create_account()
    intruder: Account = create_account()

    owned = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Owner Model",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "owner-key",
        },
        account_id=owner.id,
    )

    try:
        crud_ai_model.create_with_account(
            db=db_session,
            obj_in={
                "name": "Stolen",
                "provider_name": "anthropic",
                "model_identifier": "claude-haiku-4-5",
                "credentials_secret_id": owned.credentials_secret_id,
            },
            account_id=intruder.id,
        )
    except ValueError as exc:
        assert "different account" in str(exc)
    else:
        raise AssertionError("expected ValueError for cross-account secret reuse")


def test_get_all_for_account_is_deterministically_ordered(
    db_session: Session, create_account
):
    """Ordering must be stable: account models first, then oldest-first.

    Model resolution walks this list, so an unordered query makes pricing
    nondeterministic when several models share an identifier suffix.
    """
    account: Account = create_account()

    system_default = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "System Default",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
        },
        account_id=None,
    )
    owned_first = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Owned First",
            "provider_name": "anthropic",
            "model_identifier": "claude-haiku-4-5",
        },
        account_id=account.id,
    )
    owned_second = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Owned Second",
            "provider_name": "anthropic",
            "model_identifier": "claude-opus-4-1",
        },
        account_id=account.id,
    )

    created = {owned_first.id, owned_second.id, system_default.id}

    def ordering() -> list:
        # Other tests may leave system-default rows behind, so compare only the
        # rows this test created while preserving their relative order.
        return [
            model.id
            for model in crud_ai_model.get_all_for_account(
                db=db_session, account_id=account.id
            )
            if model.id in created
        ]

    observed = ordering()

    # Account-owned models must sort ahead of system defaults.
    assert observed[-1] == system_default.id
    assert set(observed[:2]) == {owned_first.id, owned_second.id}

    # These rows share a created_at (same transaction), so id.asc() is the
    # tiebreak. The point is that the order is fixed and explainable, not that
    # any particular model wins an inherently ambiguous tie.
    assert observed[:2] == sorted([owned_first.id, owned_second.id])

    # Repeated calls must agree — this is what pricing stability depends on.
    for _ in range(3):
        assert ordering() == observed
