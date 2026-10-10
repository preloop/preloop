"""Endpoint tests for the Anthropic usage import API (#1413)."""

from __future__ import annotations

import logging
import uuid
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.app import create_app
from preloop.api.auth import get_current_active_user
from preloop.models.crud import (
    crud_anthropic_import_connection,
    crud_secret_reference,
)
from preloop.models.db.session import get_db_session as get_db
from preloop.services.anthropic_usage_import import ANTHROPIC_IMPORT_SECRET_KIND

SUMMARY_URL = "/api/v1/anthropic-usage"
CONNECTION_URL = "/api/v1/anthropic-usage/connection"
SYNC_URL = "/api/v1/anthropic-usage/sync"
MAPPINGS_URL = "/api/v1/anthropic-usage/mappings"
PUBLISH = "preloop.api.endpoints.anthropic_usage.event_bus_service.publish_task"
ADMIN_KEY = "sk-ant-admin01-endpoint-secret-value-AbCd"


def _client_as(db_session, user) -> TestClient:
    app: FastAPI = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def test_create_requires_a_key(client) -> None:
    assert client.put(CONNECTION_URL, json={}).status_code == 422


def test_key_is_never_returned_or_logged(client, db_session, test_user, caplog):
    with caplog.at_level(logging.DEBUG):
        created = client.put(
            CONNECTION_URL,
            json={"admin_key": ADMIN_KEY, "gateway_key_names": [" gw ", "gw"]},
        )
        summary = client.get(SUMMARY_URL)
        updated = client.put(CONNECTION_URL, json={"is_active": False})

    assert created.status_code == 200
    body = created.json()
    assert body["has_key"] is True
    assert body["key_hint"] == "AbCd"
    assert body["gateway_key_names"] == ["gw"]
    for response in (created, summary, updated):
        assert ADMIN_KEY not in response.text
    assert ADMIN_KEY not in caplog.text
    assert updated.json()["is_active"] is False

    connection = crud_anthropic_import_connection.get_for_account(
        db_session, account_id=test_user.account_id
    )
    secret = crud_secret_reference.get(db_session, id=connection.secret_reference_id)
    assert secret.secret_kind == ANTHROPIC_IMPORT_SECRET_KIND
    assert secret.encrypted_value != ADMIN_KEY


def test_summary_without_connection_carries_marker(client) -> None:
    body = client.get(SUMMARY_URL).json()
    assert body["metered_by_gateway"] is False
    assert body["marker"] == "Not metered by the gateway"
    assert body["connection"] is None
    assert body["total_estimated_cost"] is None
    assert len(body["not_attributable"]) == 4


def test_delete_removes_connection_and_key(client, db_session, test_user) -> None:
    client.put(CONNECTION_URL, json={"admin_key": ADMIN_KEY})
    connection = crud_anthropic_import_connection.get_for_account(
        db_session, account_id=test_user.account_id
    )
    secret_id = connection.secret_reference_id
    assert client.delete(CONNECTION_URL).status_code == 204
    assert crud_secret_reference.get(db_session, id=secret_id) is None
    assert client.delete(CONNECTION_URL).status_code == 404


def test_sync_without_connection_is_404(client) -> None:
    assert client.post(SYNC_URL).status_code == 404


def test_sync_queues_the_import_for_this_account(client, test_user) -> None:
    client.put(CONNECTION_URL, json={"admin_key": ADMIN_KEY})
    with patch(PUBLISH, new=AsyncMock(return_value=object())) as publish:
        response = client.post(SYNC_URL)
    assert response.status_code == 202
    assert response.json() == {"status": "queued"}
    publish.assert_awaited_once_with(
        "ingest_anthropic_usage", account_id=str(test_user.account_id)
    )


def test_sync_reports_unavailable_task_bus(client) -> None:
    client.put(CONNECTION_URL, json={"admin_key": ADMIN_KEY})
    with patch(PUBLISH, new=AsyncMock(return_value=None)):
        assert client.post(SYNC_URL).status_code == 503


def test_sync_of_paused_connection_is_409(client) -> None:
    client.put(CONNECTION_URL, json={"admin_key": ADMIN_KEY, "is_active": False})
    assert client.post(SYNC_URL).status_code == 409


def test_mapping_round_trip(client, test_user) -> None:
    client.put(CONNECTION_URL, json={"admin_key": ADMIN_KEY})
    written = client.put(
        MAPPINGS_URL, json={"actor": "Dev@Corp.example", "user_id": str(test_user.id)}
    )
    assert written.status_code == 200
    assert written.json()["actor"] == "dev@corp.example"
    assert client.get(MAPPINGS_URL).json()["total"] == 1
    unknown = client.put(
        MAPPINGS_URL, json={"actor": "x@corp.example", "user_id": str(uuid.uuid4())}
    )
    assert unknown.status_code == 404
    assert client.delete(f"{MAPPINGS_URL}/dev@corp.example").status_code == 204
    assert client.delete(f"{MAPPINGS_URL}/dev@corp.example").status_code == 404


def test_non_admin_cannot_write_or_sync(db_session, test_viewer_user) -> None:
    with _client_as(db_session, test_viewer_user) as viewer:
        put = viewer.put(CONNECTION_URL, json={"admin_key": ADMIN_KEY})
        delete = viewer.delete(CONNECTION_URL)
        sync = viewer.post(SYNC_URL)
        test = viewer.post(f"{CONNECTION_URL}/test")
        mapping = viewer.put(
            MAPPINGS_URL,
            json={"actor": "a@b.example", "user_id": str(test_viewer_user.id)},
        )
    for response in (put, delete, sync, test, mapping):
        assert response.status_code == 403
        assert "manage_budgets" in response.json()["detail"]
    assert (
        crud_anthropic_import_connection.get_for_account(
            db_session, account_id=test_viewer_user.account_id
        )
        is None
    )
