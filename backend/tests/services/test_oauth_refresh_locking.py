"""OAuth refresh locks observe committed rotations and own only their savepoint."""

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4
import json

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account, crud_ai_model
from preloop.services.secret_service import SecretService
from preloop.utils.encryption import encrypt_value


@pytest.fixture(params=["oauth_openai_codex", "oauth_anthropic_claude_code"])
def oauth_model(db_engine: Engine, request: pytest.FixtureRequest) -> Iterator[UUID]:
    """Use synthetic committed credentials visible across worker sessions."""
    with Session(db_engine) as db:
        account = crud_account.create(
            db, obj_in={"organization_name": "OAuth lock test"}
        )
        account_id = account.id
        model = crud_ai_model.create_with_account(
            db=db,
            obj_in={
                "name": "Synthetic OAuth model",
                "provider_name": "openai"
                if request.param == "oauth_openai_codex"
                else "anthropic",
                "model_identifier": "synthetic-model",
                "credential_type": request.param,
                "credential_payload": {
                    "access": "synthetic-old",
                    "refresh": "synthetic-old",
                    "expires": 1,
                },
            },
            account_id=account_id,
        )
        model_id = model.id
    try:
        yield model_id
    finally:
        with Session(db_engine) as db:
            db.query(models.AIModel).filter_by(id=model_id).delete()
            db.query(models.SecretReference).filter_by(account_id=account_id).delete()
            db.query(models.Account).filter_by(id=account_id).delete()
            db.commit()


def refresh_method(service: SecretService, model: models.AIModel) -> Any:
    """Select the provider-specific path without making provider requests."""
    return (
        service._refresh_openai_codex_ai_model_credentials
        if model.provider_name == "openai"
        else service._refresh_anthropic_claude_code_ai_model_credentials
    )


def assert_secret_unlocked(db_engine: Engine, secret_id: UUID) -> None:
    """A separate transaction can acquire the secret lock before caller cleanup."""
    with Session(db_engine) as contender:
        contender.execute(text("SET LOCAL lock_timeout = '250ms'"))
        contender.query(models.SecretReference).filter_by(
            id=secret_id
        ).with_for_update().one()


def test_rotated_payload_refreshes_identity_map_without_committing_caller_work(
    db_engine: Engine, oauth_model: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = SecretService()
    with Session(db_engine) as reader, Session(db_engine) as updater:
        model = reader.get(models.AIModel, oauth_model)
        secret = model.credentials_secret
        secret_id = secret.id
        unrelated_id = uuid4()
        reader.add(
            models.Account(id=unrelated_id, organization_name="Uncommitted caller work")
        )
        reader.flush()
        rotated = {
            "access": "synthetic-new",
            "refresh": "synthetic-new",
            "expires": 4102444800000,
        }
        fresh = updater.get(models.SecretReference, secret_id)
        fresh.encrypted_value = encrypt_value(json.dumps(rotated))
        updater.commit()

        def no_provider_call(refresh_token: str) -> dict[str, Any]:
            pytest.fail(
                "A committed rotation must not trigger another provider refresh"
            )

        monkeypatch.setattr(service, "_refresh_openai_codex_token", no_provider_call)
        monkeypatch.setattr(
            service, "_refresh_anthropic_claude_code_token", no_provider_call
        )
        result = refresh_method(service, model)(
            model, {"expires": 1, "refresh": "synthetic-old"}, db=reader
        )
        assert result == rotated
        assert_secret_unlocked(db_engine, secret_id)
        assert reader.in_transaction()
        assert reader.get(models.Account, unrelated_id) is not None
        with Session(db_engine) as observer:
            assert observer.get(models.Account, unrelated_id) is None
        reader.rollback()


def test_missing_refresh_token_releases_only_owned_lock(
    db_engine: Engine, oauth_model: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = SecretService()
    with Session(db_engine) as setup:
        model = setup.get(models.AIModel, oauth_model)
        model.credentials_secret.encrypted_value = encrypt_value(
            json.dumps({"access": "synthetic-old", "expires": 1})
        )
        setup.commit()
    with Session(db_engine) as reader:
        model = reader.get(models.AIModel, oauth_model)
        secret_id = model.credentials_secret.id
        unrelated_id = uuid4()
        reader.add(
            models.Account(id=unrelated_id, organization_name="Uncommitted caller work")
        )
        reader.flush()
        result = refresh_method(service, model)(model, {"expires": 1}, db=reader)
        assert result["expires"] == 1
        assert_secret_unlocked(db_engine, secret_id)
        assert reader.get(models.Account, unrelated_id) is not None
        with Session(db_engine) as observer:
            assert observer.get(models.Account, unrelated_id) is None
        reader.rollback()


def test_needed_refresh_keeps_exclusion_until_rotated_bundle_commits(
    db_engine: Engine, oauth_model: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = SecretService()
    with Session(db_engine) as reader:
        model = reader.get(models.AIModel, oauth_model)
        secret_id = model.credentials_secret.id
        calls = []

        def provider_refresh(refresh_token: str) -> dict[str, Any]:
            with pytest.raises(OperationalError, match="lock timeout"):
                assert_secret_unlocked(db_engine, secret_id)
            calls.append(True)
            return {
                "access": "synthetic-new",
                "refresh": "synthetic-new",
                "expires": 4102444800000,
            }

        monkeypatch.setattr(service, "_refresh_openai_codex_token", provider_refresh)
        monkeypatch.setattr(
            service, "_refresh_anthropic_claude_code_token", provider_refresh
        )
        result = refresh_method(service, model)(model, {"expires": 1}, db=reader)
        assert result["expires"] == 4102444800000
        assert calls == [True]
        assert_secret_unlocked(db_engine, secret_id)
