"""Endpoint tests for the GitHub Copilot usage import API (issue #788)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from preloop.models.crud import crud_copilot_import_connection, crud_secret_reference
from preloop.services.copilot_usage_import import COPILOT_IMPORT_SECRET_KIND

SUMMARY_URL = "/api/v1/cost/copilot"
CONNECTION_URL = "/api/v1/cost/copilot/connection"
SYNC_URL = "/api/v1/cost/copilot/connection/sync"
PUBLISH = "preloop.api.endpoints.copilot_usage.event_bus_service.publish_task"


def _connect(client, **overrides):
    payload = {
        "organization": "example-org",
        "token": "org-secret-token",
        "seat_price_monthly": 19.0,
        **overrides,
    }
    return client.put(CONNECTION_URL, json=payload)


def test_create_requires_a_token(client) -> None:
    response = client.put(CONNECTION_URL, json={"organization": "example-org"})
    assert response.status_code == 422


def test_rejects_invalid_organization_slug(client) -> None:
    response = _connect(client, organization="not a slug/..")
    assert response.status_code == 422


def test_rejects_negative_seat_price(client) -> None:
    response = _connect(client, seat_price_monthly=-1)
    assert response.status_code == 422


def test_create_and_update_never_return_tokens(client, db_session, test_user) -> None:
    response = _connect(client, enterprise="example-ent", enterprise_token="ent-tok")
    assert response.status_code == 200
    body = response.json()
    assert "org-secret-token" not in response.text
    assert "ent-tok" not in response.text
    assert body["organization"] == "example-org"
    assert body["enterprise"] == "example-ent"
    assert body["has_enterprise_token"] is True
    assert body["seat_price_monthly"] == 19.0

    connection = crud_copilot_import_connection.get_for_account(
        db_session, account_id=test_user.account_id
    )
    secret = crud_secret_reference.get(db_session, id=connection.secret_reference_id)
    assert secret.secret_kind == COPILOT_IMPORT_SECRET_KIND
    assert secret.encrypted_value != "org-secret-token"

    # Updating without a token keeps the stored one; a null price clears it.
    response = client.put(
        CONNECTION_URL,
        json={
            "organization": "example-org",
            "enterprise": "example-ent",
            "seat_price_monthly": None,
        },
    )
    assert response.status_code == 200
    assert response.json()["seat_price_monthly"] is None
    db_session.refresh(connection)
    assert connection.secret_reference_id == secret.id

    summary = client.get(SUMMARY_URL).json()
    assert summary["seats"]["seat_price_monthly"] is None
    assert summary["seats"]["monthly_seat_estimate"] is None


def test_clear_enterprise_token_deletes_the_secret(
    client, db_session, test_user
) -> None:
    _connect(client, enterprise="example-ent", enterprise_token="ent-tok")
    connection = crud_copilot_import_connection.get_for_account(
        db_session, account_id=test_user.account_id
    )
    ent_secret_id = connection.enterprise_secret_reference_id
    response = client.put(
        CONNECTION_URL,
        json={"organization": "example-org", "clear_enterprise_token": True},
    )
    assert response.status_code == 200
    assert response.json()["has_enterprise_token"] is False
    assert crud_secret_reference.get(db_session, id=ent_secret_id) is None


def test_changing_organization_resets_sync_position(
    client, db_session, test_user
) -> None:
    from datetime import date

    _connect(client)
    connection = crud_copilot_import_connection.get_for_account(
        db_session, account_id=test_user.account_id
    )
    crud_copilot_import_connection.update(
        db_session, db_obj=connection, obj_in={"last_synced_day": date(2026, 9, 1)}
    )
    response = _connect(client, organization="other-org", token=None)
    assert response.status_code == 200
    assert response.json()["last_synced_day"] is None


def test_summary_always_carries_the_not_metered_marker(client) -> None:
    response = client.get(SUMMARY_URL)
    assert response.status_code == 200
    body = response.json()
    assert body["metered_by_gateway"] is False
    assert body["marker"] == "Not metered by the gateway"
    assert body["connection"] is None


def test_summary_rejects_inverted_window(client) -> None:
    response = client.get(
        SUMMARY_URL,
        params={
            "start_date": "2026-09-10T00:00:00Z",
            "end_date": "2026-09-01T00:00:00Z",
        },
    )
    assert response.status_code == 422


def test_delete_removes_connection_and_secrets(client, db_session, test_user) -> None:
    _connect(client)
    connection = crud_copilot_import_connection.get_for_account(
        db_session, account_id=test_user.account_id
    )
    secret_id = connection.secret_reference_id
    assert client.delete(CONNECTION_URL).status_code == 204
    assert (
        crud_copilot_import_connection.get_for_account(
            db_session, account_id=test_user.account_id
        )
        is None
    )
    assert crud_secret_reference.get(db_session, id=secret_id) is None
    assert client.delete(CONNECTION_URL).status_code == 404


def test_sync_without_connection_is_404(client) -> None:
    with patch(PUBLISH, new_callable=AsyncMock) as publish:
        response = client.post(SYNC_URL)
    assert response.status_code == 404
    publish.assert_not_called()


def test_sync_queues_the_import_for_this_account(client, test_user) -> None:
    _connect(client)
    with patch(PUBLISH, new_callable=AsyncMock, return_value=object()) as publish:
        response = client.post(SYNC_URL)
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    publish.assert_awaited_once_with(
        "ingest_copilot_usage", account_id=str(test_user.account_id)
    )


def test_sync_reports_unavailable_task_bus(client) -> None:
    _connect(client)
    with patch(PUBLISH, new_callable=AsyncMock, return_value=None):
        assert client.post(SYNC_URL).status_code == 503
    with patch(PUBLISH, new_callable=AsyncMock, side_effect=RuntimeError("down")):
        assert client.post(SYNC_URL).status_code == 503
