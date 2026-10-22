"""Construct synthetic model credentials using the persisted secret contract."""

from typing import Any
from uuid import uuid4

from preloop.models import models
from preloop.utils.encryption import encrypt_value


def ai_model_with_secret(
    *, api_key: str | None = None, **fields: Any
) -> models.AIModel:
    """Build a model fixture with an encrypted reference instead of a legacy key."""
    if api_key:
        secret_id = fields.get("credentials_secret_id") or uuid4()
        fields["credentials_secret_id"] = secret_id
        fields["credentials_secret"] = models.SecretReference(
            id=secret_id,
            account_id=fields.get("account_id"),
            name="Synthetic model credential",
            backend_type="local_encrypted",
            secret_kind="ai_model_api_key",
            encrypted_value=encrypt_value(api_key),
            status="active",
        )
    return models.AIModel(**fields)
