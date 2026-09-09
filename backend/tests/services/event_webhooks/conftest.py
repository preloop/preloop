"""Shared fixtures for outbound event webhook tests."""

import pytest

from preloop.models.crud import crud_account
from preloop.models.models.webhook_endpoint import (
    SOURCE_ACCOUNT,
    WebhookEndpoint,
)
from preloop.services.event_webhooks.signing import generate_secret, secret_hint
from preloop.utils.encryption import encrypt_value


@pytest.fixture
def account(db_session):
    """A bare account to own endpoints and deliveries."""
    return crud_account.create(
        db_session,
        obj_in={"organization_name": "Webhook Test Org", "is_active": True},
    )


@pytest.fixture
def make_endpoint(db_session, account):
    """Factory for endpoints, returning (endpoint, plaintext secret)."""

    def _make(
        *,
        url="https://example.com/hook",
        event_types=None,
        active=True,
        source=SOURCE_ACCOUNT,
        account_id=None,
        secret=None,
    ):
        plaintext = secret or generate_secret()
        endpoint = WebhookEndpoint(
            account_id=account_id or account.id,
            url=url,
            secret_encrypted=encrypt_value(plaintext),
            secret_hint=secret_hint(plaintext),
            event_types=event_types if event_types is not None else [],
            active=active,
            source=source,
        )
        db_session.add(endpoint)
        db_session.flush()
        return endpoint, plaintext

    return _make
