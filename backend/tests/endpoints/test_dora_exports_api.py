"""HTTP edge of the DORA exports: format, headers, period, gate, audit."""

from __future__ import annotations

import base64
import csv
import hashlib
import inspect
import io
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException, status

from preloop.api.endpoints import exports
from preloop.cra.evidence_pack import canonical_manifest_json
from preloop.models import models
from preloop.utils import permissions as perms

ASSET_URL = "/api/v1/exports/asset-register"
INCIDENT_URL = "/api/v1/exports/incident-candidates"


def _naive(hours_ago: float) -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=hours_ago)


@pytest.fixture
def estate(db_session, test_user):
    """One agent and one halt, enough to exercise both endpoints."""
    agent = models.ManagedAgent(
        id=uuid4(),
        account_id=test_user.account_id,
        agent_kind="cursor",
        session_source_type="cursor",
        session_source_id="cursor-1",
        display_name="Payments agent",
        enrolled_via="runtime_session_token",
        owner_user_id=test_user.id,
        lifecycle_state="active",
        lifecycle_updated_at=_naive(50),
        last_seen_at=_naive(1),
    )
    halt = models.AuditLog(
        id=uuid4(),
        account_id=str(test_user.account_id),
        user_id=test_user.id,
        action="kill_switch_activated",
        resource_type="account",
        resource_id=str(test_user.account_id),
        status="success",
        details={"scope": "all", "reason": "provider incident"},
        timestamp=_naive(2),
    )
    db_session.add_all([agent, halt])
    db_session.commit()
    return {"agent": agent, "halt": halt}


class TestAssetRegisterEndpoint:
    def test_csv_is_the_default_and_carries_its_manifest_in_headers(
        self, client, estate
    ):
        response = client.get(ASSET_URL)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "asset-register" in response.headers["content-disposition"]

        body = response.content
        assert response.headers["X-Preloop-Export-Sha256"] == (
            hashlib.sha256(body).hexdigest()
        )
        manifest = json.loads(
            base64.b64decode(response.headers["X-Preloop-Export-Manifest"])
        )
        assert manifest["members"][0]["sha256"] == hashlib.sha256(body).hexdigest()
        assert (
            manifest["members_digest"] == response.headers["X-Preloop-Members-Digest"]
        )
        assert manifest["schema"] == "preloop.dora.asset_register_manifest/v1"

        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8"))))
        assert any(row["name"] == "Payments agent" for row in rows)

    def test_json_puts_the_manifest_next_to_the_rows(self, client, estate):
        response = client.get(ASSET_URL, params={"format": "json"})
        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"manifest", "rows"}
        member = payload["manifest"]["members"][0]
        assert member["name"] == "asset-register.json"
        assert (
            hashlib.sha256(canonical_manifest_json(payload["rows"])).hexdigest()
            == member["sha256"]
        ), "the rows as served rehash to the member digest"
        assert (
            response.headers["X-Preloop-Export-Sha256"]
            == hashlib.sha256(response.content).hexdigest()
        ), "the header must hash the JSON envelope, not the rows member"

    def test_unknown_format_is_a_400(self, client, estate):
        response = client.get(ASSET_URL, params={"format": "pdf"})
        assert response.status_code == 400
        assert "csv" in response.json()["detail"]

    def test_the_export_is_itself_audited(self, client, db_session, estate, test_user):
        client.get(ASSET_URL)
        row = (
            db_session.query(models.AuditLog)
            .filter(models.AuditLog.action == "dora_asset_register_export")
            .one()
        )
        assert row.status == "success"
        assert row.details["format"] == "csv"
        assert row.details["body_sha256"]
        assert row.details["total_rows"] >= 1

    def test_json_audit_records_the_served_envelope_digest(
        self, client, db_session, estate
    ):
        response = client.get(ASSET_URL, params={"format": "json"})
        assert response.status_code == 200
        row = (
            db_session.query(models.AuditLog)
            .filter(models.AuditLog.action == "dora_asset_register_export")
            .one()
        )
        assert row.details["format"] == "json"
        assert row.details["body_sha256"] == response.headers["X-Preloop-Export-Sha256"]
        assert row.details["size_bytes"] == len(response.content)


class TestIncidentCandidatesEndpoint:
    def test_period_can_be_given_as_plain_dates(self, client, estate):
        today = datetime.now(UTC).date()
        response = client.get(
            INCIDENT_URL,
            params={
                "from": str(today - timedelta(days=1)),
                "to": str(today + timedelta(days=1)),
                "format": "json",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["manifest"]["counts"]["kill_switch_activation"] == 1
        assert payload["manifest"]["period"]["boundary"].startswith("start inclusive")

    def test_a_period_that_excludes_the_event_returns_an_empty_file(
        self, client, estate
    ):
        response = client.get(
            INCIDENT_URL,
            params={"from": "2020-01-01", "to": "2020-02-01", "format": "json"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["rows"] == []
        assert payload["manifest"]["total_rows"] == 0
        assert payload["manifest"]["columns"], "columns survive an empty period"

    def test_default_period_is_the_last_month(self, client, estate):
        response = client.get(INCIDENT_URL, params={"format": "json"})
        assert response.status_code == 200
        period = response.json()["manifest"]["period"]
        start = datetime.strptime(period["start"], "%Y-%m-%dT%H:%M:%SZ")
        end = datetime.strptime(period["end"], "%Y-%m-%dT%H:%M:%SZ")
        assert (end - start).days == exports.DEFAULT_PERIOD_DAYS

    def test_csv_carries_the_classification_note_in_its_manifest(self, client, estate):
        response = client.get(INCIDENT_URL)
        assert response.status_code == 200
        manifest = json.loads(
            base64.b64decode(response.headers["X-Preloop-Export-Manifest"])
        )
        assert "classification" in manifest
        assert "Art. 17" in " ".join(manifest["feeds"])

    def test_unparseable_dates_are_a_400(self, client, estate):
        response = client.get(INCIDENT_URL, params={"from": "last tuesday"})
        assert response.status_code == 400
        assert "YYYY-MM-DD" in response.json()["detail"]

    def test_inverted_period_is_a_400(self, client, estate):
        response = client.get(
            INCIDENT_URL, params={"from": "2026-02-01", "to": "2026-01-01"}
        )
        assert response.status_code == 400

    def test_an_absurd_period_is_refused(self, client, estate):
        response = client.get(
            INCIDENT_URL, params={"from": "2000-01-01", "to": "2026-01-01"}
        )
        assert response.status_code == 400
        assert "period" in response.json()["detail"]

    def test_the_export_is_itself_audited(self, client, db_session, estate):
        client.get(INCIDENT_URL)
        row = (
            db_session.query(models.AuditLog)
            .filter(models.AuditLog.action == "dora_incident_candidates_export")
            .one()
        )
        assert row.details["period_start"]
        assert row.details["members_digest"]


class TestPermissionGate:
    """Both exports sit behind the audit-trail permission.

    OSS has no RBAC plugin, so the decorator is a no-op at import time. These
    tests install a fake plugin and re-wrap, which is how the rest of the
    suite proves an endpoint is gated (see tests/endpoints/test_policies.py).
    """

    def test_both_endpoints_require_view_audit_logs(self, mocker):
        seen = []

        def fake_plugin_require(permission_name):
            seen.append(permission_name)

            def decorator(func):
                return func

            return decorator

        mocker.patch(
            "preloop.utils.permissions._plugin_require_permission",
            fake_plugin_require,
        )
        mocker.patch(
            "preloop.utils.permissions._rbac_checks_enabled", return_value=True
        )
        for handler in (
            exports.export_asset_register,
            exports.export_incident_candidates,
        ):
            perms.require_permission(exports.EXPORT_PERMISSION)(inspect.unwrap(handler))
        assert seen == ["view_audit_logs", "view_audit_logs"]

    def test_a_denied_permission_stops_the_export(self, mocker):
        def fake_plugin_require(permission_name):
            def decorator(func):
                def wrapper(*args, **kwargs):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Missing {permission_name}",
                    )

                return wrapper

            return decorator

        mocker.patch(
            "preloop.utils.permissions._plugin_require_permission",
            fake_plugin_require,
        )
        mocker.patch(
            "preloop.utils.permissions._rbac_checks_enabled", return_value=True
        )
        wrapped = perms.require_permission(exports.EXPORT_PERMISSION)(
            inspect.unwrap(exports.export_asset_register)
        )
        with pytest.raises(HTTPException) as error:
            wrapped(current_user=object(), db=object(), account=object())
        assert error.value.status_code == status.HTTP_403_FORBIDDEN
        assert "view_audit_logs" in error.value.detail

    def test_the_gate_fails_closed_without_auth_dependencies(self, mocker):
        def fake_plugin_require(permission_name):
            def decorator(func):
                return func

            return decorator

        mocker.patch(
            "preloop.utils.permissions._plugin_require_permission",
            fake_plugin_require,
        )
        mocker.patch(
            "preloop.utils.permissions._rbac_checks_enabled", return_value=True
        )
        wrapped = perms.require_permission(exports.EXPORT_PERMISSION)(
            inspect.unwrap(exports.export_incident_candidates)
        )
        with pytest.raises(HTTPException) as error:
            wrapped(account=object())
        assert error.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
