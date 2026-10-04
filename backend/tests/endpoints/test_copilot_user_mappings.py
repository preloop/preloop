"""Endpoint tests for Copilot login mappings and spend coverage (#1061)."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.app import create_app
from preloop.api.auth import get_current_active_user
from preloop.models.crud import (
    crud_account,
    crud_copilot_import_connection,
    crud_copilot_user_mapping,
    crud_role,
    crud_user_role,
)
from preloop.models.db.session import get_db_session as get_db
from tests.services.test_copilot_spend_source import (
    DAY,
    connect,
    make_user,
    premium_row,
    store,
)

CONNECTION_URL = "/api/v1/cost/copilot/connection"
MAPPINGS_URL = "/api/v1/cost/copilot/mappings"
COVERAGE_URL = "/api/v1/cost/copilot/spend-coverage"


def _connect(client, **overrides):
    payload = {"organization": "example-org", "token": "org-secret-token", **overrides}
    response = client.put(CONNECTION_URL, json=payload)
    assert response.status_code == 200, response.text
    return response


def _client_as(db_session, user) -> TestClient:
    app: FastAPI = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


@pytest.fixture
def anon_client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        yield client


# --- happy path -------------------------------------------------------------


def test_list_is_empty_without_a_connection(client):
    response = client.get(MAPPINGS_URL)

    assert response.status_code == 200
    assert response.json() == {"organization": None, "items": [], "total": 0}


def test_upsert_needs_a_connection(client, test_user):
    response = client.put(
        MAPPINGS_URL, json={"github_login": "alice", "user_id": str(test_user.id)}
    )

    assert response.status_code == 404
    assert "Connect" in response.json()["detail"]


def test_upsert_list_update_and_delete(client, db_session, test_user):
    _connect(client)
    other = make_user(db_session, test_user.account_id, "jane@example.com")

    created = client.put(
        MAPPINGS_URL, json={"github_login": " Alice ", "user_id": str(test_user.id)}
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["github_login"] == "alice"
    assert body["user_id"] == str(test_user.id)
    assert body["user_name"] == "Test User"
    assert body["organization"] == "example-org"

    # The same login in another case updates the one mapping.
    updated = client.put(
        MAPPINGS_URL, json={"github_login": "ALICE", "user_id": str(other.id)}
    )
    assert updated.status_code == 200
    assert updated.json()["user_id"] == str(other.id)

    listing = client.get(MAPPINGS_URL).json()
    assert listing["organization"] == "example-org"
    assert listing["total"] == 1
    assert listing["items"][0]["github_login"] == "alice"
    assert listing["items"][0]["user_id"] == str(other.id)

    assert client.delete(f"{MAPPINGS_URL}/Alice").status_code == 204
    assert client.delete(f"{MAPPINGS_URL}/alice").status_code == 404
    assert client.get(MAPPINGS_URL).json()["total"] == 0


def test_delete_without_connection_is_404(client):
    assert client.delete(f"{MAPPINGS_URL}/alice").status_code == 404


def test_invalid_logins_are_422(client, test_user):
    _connect(client)
    for login in ("", "   ", "has space", "slash/name", "a" * 101, "-leading"):
        response = client.put(
            MAPPINGS_URL, json={"github_login": login, "user_id": str(test_user.id)}
        )
        assert response.status_code == 422, login


def test_upsert_on_paused_connection_is_refused(client, test_user):
    _connect(client, is_active=False)

    response = client.put(
        MAPPINGS_URL, json={"github_login": "alice", "user_id": str(test_user.id)}
    )

    assert response.status_code == 404


# --- isolation --------------------------------------------------------------


def test_foreign_unknown_and_inactive_users_are_refused_without_a_hint(
    client, db_session, test_user
):
    _connect(client)
    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )
    foreign = make_user(
        db_session, other_account.id, "foreign-secret@other.example.com"
    )
    inactive = make_user(
        db_session, test_user.account_id, "inactive@example.com", is_active=False
    )

    details = set()
    for target in (foreign.id, inactive.id, uuid.uuid4()):
        response = client.put(
            MAPPINGS_URL, json={"github_login": "alice", "user_id": str(target)}
        )
        assert response.status_code == 404, response.text
        assert "foreign-secret" not in response.text
        assert "Foreign" not in response.text
        details.add(response.json()["detail"])
    assert len(details) == 1
    assert client.get(MAPPINGS_URL).json()["total"] == 0


def test_list_shows_only_this_account_and_the_current_organization(
    client, db_session, test_user
):
    _connect(client)
    client.put(
        MAPPINGS_URL, json={"github_login": "alice", "user_id": str(test_user.id)}
    )

    # Another account maps the same login to its own user.
    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )
    other_user = make_user(db_session, other_account.id, "other@other.example.com")
    other_connection = connect(db_session, other_account.id)
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=other_connection,
        github_login="alice",
        user_id=other_user.id,
        commit=False,
    )

    listing = client.get(MAPPINGS_URL).json()
    assert listing["total"] == 1
    assert listing["items"][0]["user_id"] == str(test_user.id)
    assert "other@other.example.com" not in str(listing)

    # Moving the connection to another organization hides the mappings
    # written for the first one.
    _connect(client, organization="other-org", token=None)
    assert client.get(MAPPINGS_URL).json() == {
        "organization": "other-org",
        "items": [],
        "total": 0,
    }


# --- authorization ----------------------------------------------------------


def test_unauthenticated_requests_are_401(anon_client):
    assert anon_client.get(MAPPINGS_URL).status_code == 401
    assert (
        anon_client.put(
            MAPPINGS_URL, json={"github_login": "alice", "user_id": str(uuid.uuid4())}
        ).status_code
        == 401
    )
    assert anon_client.delete(f"{MAPPINGS_URL}/alice").status_code == 401
    assert anon_client.get(COVERAGE_URL).status_code == 401


def test_viewer_cannot_read_or_write_mappings(db_session, test_viewer_user):
    connect(db_session, test_viewer_user.account_id)
    with _client_as(db_session, test_viewer_user) as viewer:
        listed = viewer.get(MAPPINGS_URL)
        written = viewer.put(
            MAPPINGS_URL,
            json={"github_login": "alice", "user_id": str(test_viewer_user.id)},
        )
        deleted = viewer.delete(f"{MAPPINGS_URL}/alice")
        coverage = viewer.get(COVERAGE_URL)

    assert listed.status_code == 403
    assert "view_cost" in listed.json()["detail"]
    assert written.status_code == 403
    assert "manage_budgets" in written.json()["detail"]
    assert deleted.status_code == 403
    assert coverage.status_code == 403


def test_cost_reader_can_list_but_not_write(db_session, test_viewer_user):
    """An analyst holds view_cost but not manage_budgets."""
    analyst_role = crud_role.get_by_name(db_session, name="analyst")
    crud_user_role.create(
        db_session, obj_in={"user_id": test_viewer_user.id, "role_id": analyst_role.id}
    )
    db_session.flush()
    connection = connect(db_session, test_viewer_user.account_id)
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=connection,
        github_login="alice",
        user_id=test_viewer_user.id,
        commit=False,
    )
    with _client_as(db_session, test_viewer_user) as analyst:
        listed = analyst.get(MAPPINGS_URL)
        written = analyst.put(
            MAPPINGS_URL,
            json={"github_login": "bob", "user_id": str(test_viewer_user.id)},
        )
        deleted = analyst.delete(f"{MAPPINGS_URL}/alice")
        coverage = analyst.get(COVERAGE_URL)

    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert written.status_code == 403
    assert deleted.status_code == 403
    assert coverage.status_code == 200


# --- coverage ---------------------------------------------------------------


def test_spend_coverage_reports_counts_without_payloads(client, db_session, test_user):
    _connect(client)
    client.put(
        MAPPINGS_URL, json={"github_login": "alice", "user_id": str(test_user.id)}
    )
    store(
        db_session,
        test_user.account_id,
        premium_row(DAY, login="alice", amount=8.0),
        premium_row(DAY, login="bob", amount=4.0),
        premium_row(DAY, login=None, amount=2.0, unattributed=True),
    )

    response = client.get(
        COVERAGE_URL, params={"start_day": DAY.isoformat(), "end_day": DAY.isoformat()}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["organization"] == "example-org"
    assert body["connection_active"] is True
    assert body["period_start"] == "2026-09-24"
    assert body["period_end"] == "2026-09-24"
    assert body["mapped_rows"] == 1
    assert body["mapped_net_amount"] == 8.0
    assert body["excluded"]["unmapped"] == 1
    assert body["excluded"]["unattributed"] == 1
    assert body["unmapped_logins"] == ["bob"]
    assert body["mapped_logins"] == ["alice"]
    assert "org-secret-token" not in response.text
    assert "netQuantity" not in response.text
    assert "raw" not in body


def test_spend_coverage_defaults_to_the_replay_window_and_rejects_inversion(client):
    _connect(client)

    body = client.get(COVERAGE_URL).json()
    start = date.fromisoformat(body["period_start"])
    end = date.fromisoformat(body["period_end"])
    assert (end - start).days == 27
    assert body["mapped_net_amount"] is None

    inverted = client.get(
        COVERAGE_URL, params={"start_day": "2026-09-10", "end_day": "2026-09-01"}
    )
    assert inverted.status_code == 422


def test_deleting_the_connection_removes_its_mappings(client, db_session, test_user):
    _connect(client)
    client.put(
        MAPPINGS_URL, json={"github_login": "alice", "user_id": str(test_user.id)}
    )
    connection = crud_copilot_import_connection.get_for_account(
        db_session, account_id=test_user.account_id
    )
    assert crud_copilot_user_mapping.list_for_connection(
        db_session, connection=connection
    )

    assert client.delete(CONNECTION_URL).status_code == 204

    assert db_session.query(crud_copilot_user_mapping.model).count() == 0
    assert client.get(MAPPINGS_URL).json() == {
        "organization": None,
        "items": [],
        "total": 0,
    }
