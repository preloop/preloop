"""Endpoint tests for reading and changing the session embedding setting.

The contract worth pinning here is the default an account reads before it has
decided anything, the round trip of ``scope``, and the refusal of a value
that is neither scope: a 422 is the difference between "this build does not
know that word" and a row that silently embeds nothing or everything.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.app import create_app
from preloop.api.auth import get_current_active_user
from preloop.models.crud import crud_account, crud_session_embedding_setting
from preloop.models.db.session import get_db_session as get_db
from preloop.models.models.session_embedding_setting import (
    EMBEDDING_SCOPE_FULL,
    EMBEDDING_SCOPE_SUMMARIES_ONLY,
)

SETTING_URL = "/api/v1/runtime-sessions/settings/embedding"


@pytest.fixture(autouse=True)
def account_owner(db_session, test_user):
    """Make the caller the account's own owner.

    Changing the scope is gated on ``manage_budgets``, and the OSS fallback
    lets the account's primary user through. The seeded role matrix is not
    present in the test database, so this is how a test says "the person
    whose account this is".
    """
    account = crud_account.get(db_session, id=test_user.account_id)
    account.primary_user_id = test_user.id
    db_session.flush()
    return account


def test_an_account_that_never_decided_reads_the_shipped_default(
    client, db_session, test_user
):
    """Off, and summaries only: the same answer the worker acts on."""
    response = client.get(SETTING_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["scope"] == EMBEDDING_SCOPE_SUMMARIES_ONLY
    # The help text ships with the setting so a console does not have to
    # invent its own wording for the trade off.
    assert "summaries_only" in body["scope_help"]
    assert "full" in body["scope_help"]


def test_the_scope_round_trips_through_the_endpoint(client, db_session, test_user):
    """What the endpoint returns is what the worker will read next pass."""
    widened = client.put(SETTING_URL, json={"scope": EMBEDDING_SCOPE_FULL})

    assert widened.status_code == 200
    assert widened.json()["scope"] == EMBEDDING_SCOPE_FULL
    assert client.get(SETTING_URL).json()["scope"] == EMBEDDING_SCOPE_FULL
    stored = crud_session_embedding_setting.get_for_account(
        db_session, account_id=test_user.account_id
    )
    assert stored is not None
    assert stored.scope == EMBEDDING_SCOPE_FULL

    narrowed = client.put(SETTING_URL, json={"scope": EMBEDDING_SCOPE_SUMMARIES_ONLY})

    assert narrowed.status_code == 200
    assert narrowed.json()["scope"] == EMBEDDING_SCOPE_SUMMARIES_ONLY


def test_an_unknown_scope_is_rejected_with_422(client):
    """A misspelled scope is a validation error, not a quiet default."""
    response = client.put(SETTING_URL, json={"scope": "everything"})

    assert response.status_code == 422


def test_an_unrecognised_field_is_rejected_with_422(client):
    """This body carries scope alone; opting in names a provider elsewhere."""
    response = client.put(
        SETTING_URL,
        json={"scope": EMBEDDING_SCOPE_FULL, "enabled": True},
    )

    assert response.status_code == 422


def test_the_change_is_bound_to_the_calling_account(client, db_session, test_user):
    """Another account's setting is not touched by this one's decision."""
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    crud_session_embedding_setting.get_or_create(
        db_session, account_id=other_account.id, commit=True
    )

    assert (
        client.put(SETTING_URL, json={"scope": EMBEDDING_SCOPE_FULL}).status_code == 200
    )

    theirs = crud_session_embedding_setting.get_for_account(
        db_session, account_id=other_account.id
    )
    assert theirs.scope == EMBEDDING_SCOPE_SUMMARIES_ONLY


def test_a_user_without_the_budget_permission_cannot_widen_the_scope(
    db_session, test_viewer_user
):
    """Widening the scope spends money, so a viewer is refused."""
    app: FastAPI = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_viewer_user

    with TestClient(app) as viewer_client:
        response = viewer_client.put(SETTING_URL, json={"scope": EMBEDDING_SCOPE_FULL})

    assert response.status_code == 403
    assert "manage_budgets" in response.json()["detail"]
    stored = crud_session_embedding_setting.get_for_account(
        db_session, account_id=test_viewer_user.account_id
    )
    assert stored is None or stored.scope == EMBEDDING_SCOPE_SUMMARIES_ONLY
