"""Operator rotation of AI model credentials (EE #57)."""

from typing import Any, Optional
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account, crud_ai_model
from preloop.services.secret_service import SecretService


class _FakeVault:
    """Stand-in external backend that resolves only known paths."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def validate_reference(
        self, *, external_ref: str, meta_data: Optional[dict] = None
    ) -> None:
        if not external_ref.startswith("secret/"):
            raise ValueError("Invalid external reference")

    def resolve(self, secret_ref: models.SecretReference) -> str:
        if secret_ref.external_ref not in self.values:
            raise RuntimeError("vault read failed")
        return self.values[secret_ref.external_ref]


def _service(vault: Optional[_FakeVault] = None) -> SecretService:
    service = SecretService()
    if vault is not None:
        service._backends["vault_kv_v2"] = vault  # type: ignore[assignment]
    return service


def _model(db: Session, account_id: Any, **fields: Any) -> models.AIModel:
    obj_in = {
        "name": f"Rotation model {uuid4().hex[:6]}",
        "provider_name": "openai",
        "model_identifier": "gpt-test",
        **fields,
    }
    return crud_ai_model.create_with_account(
        db=db, obj_in=obj_in, account_id=account_id
    )


def test_new_secret_starts_at_version_one(db_session: Session, test_user) -> None:
    model = _model(db_session, test_user.account_id, api_key="sk-old")
    assert model.credentials_version == 1
    assert model.credentials_rotated_at is None


def test_rotate_local_key_bumps_version_and_resolves_new_value(
    db_session: Session, test_user
) -> None:
    model = _model(db_session, test_user.account_id, api_key="sk-old")
    sibling = _model(
        db_session,
        test_user.account_id,
        credentials_secret_id=model.credentials_secret_id,
    )
    service = _service()

    secret = service.rotate_secret_reference(
        db_session,
        secret_id=model.credentials_secret_id,
        account_id=test_user.account_id,
        secret_value="sk-new",
    )

    assert secret.version == 2
    assert secret.rotated_at is not None
    db_session.refresh(sibling)
    assert service.resolve_ai_model_api_key(sibling).value == "sk-new"
    assert "sk-new" not in (secret.encrypted_value or "")


def test_rotate_refuses_other_account(db_session: Session, test_user) -> None:
    model = _model(db_session, test_user.account_id, api_key="sk-old")
    other = crud_account.create(db_session, obj_in={"organization_name": "Other"})
    with pytest.raises(LookupError):
        _service().rotate_secret_reference(
            db_session,
            secret_id=model.credentials_secret_id,
            account_id=other.id,
            secret_value="sk-new",
        )


def test_rotate_refuses_structured_credentials(db_session: Session, test_user) -> None:
    model = _model(
        db_session,
        test_user.account_id,
        credential_type="oauth_openai_codex",
        credential_payload={"access": "a", "refresh": "r", "expires": 1},
    )
    with pytest.raises(ValueError, match="Only API-key credentials"):
        _service().rotate_secret_reference(
            db_session,
            secret_id=model.credentials_secret_id,
            account_id=test_user.account_id,
            secret_value="sk-new",
        )
    db_session.refresh(model.credentials_secret)
    assert model.credentials_secret.version == 1


def test_failed_encryption_verification_keeps_old_secret(
    db_session: Session, test_user
) -> None:
    model = _model(db_session, test_user.account_id, api_key="sk-old")
    service = _service()
    with (
        patch("preloop.services.secret_service.decrypt_value", return_value="garbled"),
        pytest.raises(ValueError, match="verification failed"),
    ):
        service.rotate_secret_reference(
            db_session,
            secret_id=model.credentials_secret_id,
            account_id=test_user.account_id,
            secret_value="sk-new",
        )
    db_session.expire_all()
    model = crud_ai_model.get(db_session, id=model.id)
    assert model.credentials_version == 1
    assert service.resolve_ai_model_api_key(model).value == "sk-old"


def test_rotate_external_ref_requires_resolvable_path(
    db_session: Session, test_user
) -> None:
    vault = _FakeVault({"secret/old": "vk-old", "secret/new": "vk-new"})
    service = _service(vault)
    secret = service.create_external_secret_reference(
        db_session,
        account_id=test_user.account_id,
        name="Vault key",
        secret_kind="ai_model_api_key",
        backend_type="vault_kv_v2",
        external_ref="secret/old",
    )
    assert secret.version == 1

    with pytest.raises(ValueError, match="could not be resolved"):
        service.rotate_secret_reference(
            db_session,
            secret_id=secret.id,
            account_id=test_user.account_id,
            external_ref="secret/missing",
        )
    db_session.refresh(secret)
    assert (secret.external_ref, secret.version) == ("secret/old", 1)

    rotated = service.rotate_secret_reference(
        db_session,
        secret_id=secret.id,
        account_id=test_user.account_id,
        external_ref="secret/new",
    )
    assert (rotated.external_ref, rotated.version) == ("secret/new", 2)
    assert service.resolve_secret_reference(rotated).value == "vk-new"


def test_update_with_new_key_counts_as_rotation(db_session: Session, test_user) -> None:
    model = _model(db_session, test_user.account_id, api_key="sk-old")
    crud_ai_model.update(db_session, db_obj=model, obj_in={"api_key": "sk-2"})
    db_session.refresh(model.credentials_secret)
    assert model.credentials_version == 2
    assert model.credentials_rotated_at is not None


def _client(db_session: Session, user: Any) -> TestClient:
    from preloop.api.app import create_app
    from preloop.api.auth import get_current_active_user
    from preloop.models.db.session import get_db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def test_rotate_endpoint_audits_without_material(
    db_session: Session, test_user
) -> None:
    model = _model(db_session, test_user.account_id, api_key="sk-old")
    _model(
        db_session,
        test_user.account_id,
        credentials_secret_id=model.credentials_secret_id,
    )
    client = _client(db_session, test_user)

    with patch("preloop.utils.audit.log_config_change") as audit:
        response = client.post(
            f"/api/v1/ai-models/{model.id}/credentials/rotate",
            json={"api_key": "sk-rotated-secret"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 2
    assert body["shared_model_count"] == 2
    assert body["backend_type"] == "local_encrypted"
    assert "sk-rotated-secret" not in response.text
    audit.assert_called_once()
    kwargs = audit.call_args.kwargs
    assert (kwargs["config_type"], kwargs["action"]) == ("secret_reference", "rotated")
    assert kwargs["old_value"]["version"] == 1
    assert kwargs["new_value"]["version"] == 2
    assert kwargs["new_value"]["reference_id"] == str(model.credentials_secret_id)
    assert "sk-rotated-secret" not in repr(kwargs)
    read = client.get(f"/api/v1/ai-models/{model.id}").json()
    assert read["credentials_version"] == 2
    assert read["credentials_rotated_at"]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({}, 400),
        ({"api_key": "sk-echo-me", "external_ref": "secret/x"}, 400),
        ({"api_key": "   "}, 400),
        ({"external_ref": " "}, 400),
        ({"api_key": "sk-echo-me", "x": 1}, 422),
    ],
)
def test_rotate_endpoint_rejects_bad_bodies_without_echo(
    db_session: Session, test_user, body: dict, status: int
) -> None:
    model = _model(db_session, test_user.account_id, api_key="sk-old")
    response = _client(db_session, test_user).post(
        f"/api/v1/ai-models/{model.id}/credentials/rotate", json=body
    )
    assert response.status_code == status
    assert "sk-echo-me" not in response.text
    db_session.refresh(model.credentials_secret)
    assert model.credentials_secret.version == 1


def test_rotate_refuses_external_structured_credentials(
    db_session: Session, test_user
) -> None:
    service = _service(_FakeVault({"secret/a": "{}", "secret/b": "{}"}))
    secret = service.create_external_secret_reference(
        db_session,
        account_id=test_user.account_id,
        name="Vault bundle",
        secret_kind="ai_model_credentials",
        backend_type="vault_kv_v2",
        external_ref="secret/a",
    )
    with pytest.raises(ValueError, match="Only API-key credentials"):
        service.rotate_secret_reference(
            db_session,
            secret_id=secret.id,
            account_id=test_user.account_id,
            external_ref="secret/b",
        )


def test_external_update_is_account_scoped(db_session: Session, test_user) -> None:
    service = _service(_FakeVault({"secret/a": "v"}))
    secret = service.create_external_secret_reference(
        db_session,
        account_id=test_user.account_id,
        name="Vault key",
        secret_kind="ai_model_api_key",
        backend_type="vault_kv_v2",
        external_ref="secret/a",
    )
    other = crud_account.create(db_session, obj_in={"organization_name": "Other"})
    created = service.create_external_secret_reference(
        db_session,
        account_id=other.id,
        name="Hijack",
        secret_kind="ai_model_api_key",
        backend_type="vault_kv_v2",
        external_ref="secret/evil",
        existing_secret_id=secret.id,
    )
    assert created.id != secret.id
    db_session.refresh(secret)
    assert (secret.external_ref, secret.version) == ("secret/a", 1)

    updated = service.create_external_secret_reference(
        db_session,
        account_id=test_user.account_id,
        name="Vault key",
        secret_kind="ai_model_api_key",
        backend_type="vault_kv_v2",
        external_ref="secret/b",
        existing_secret_id=secret.id,
    )
    assert (updated.id, updated.version) == (secret.id, 2)


def test_rotate_endpoint_hides_other_accounts_models(
    db_session: Session, test_user, test_viewer_user
) -> None:
    model = _model(db_session, test_viewer_user.account_id, api_key="sk-old")
    response = _client(db_session, test_user).post(
        f"/api/v1/ai-models/{model.id}/credentials/rotate", json={"api_key": "x"}
    )
    assert response.status_code == 404


def test_rotate_endpoint_refuses_model_without_secret(
    db_session: Session, test_user
) -> None:
    model = _model(db_session, test_user.account_id)
    response = _client(db_session, test_user).post(
        f"/api/v1/ai-models/{model.id}/credentials/rotate", json={"api_key": "x"}
    )
    assert response.status_code == 400
