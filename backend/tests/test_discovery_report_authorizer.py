"""Optional report identity enforcement precedes every ingest mutation."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.api.endpoints.agent_discovery import create_discovery_report
from preloop.schemas.agent_discovery import DiscoveryReportRequest


def test_no_envelope_report_is_authorized_before_candidate_write() -> None:
    """A legacy payload must not bypass the report identity extension."""
    account = SimpleNamespace(id=uuid4())
    user = SimpleNamespace(id=uuid4(), account_id=account.id)
    payload = DiscoveryReportRequest(workstation_fingerprint="a" * 64)
    authorizer = MagicMock()
    authorizer.authorize_report.side_effect = HTTPException(403, "device mismatch")
    with (
        patch("preloop.api.endpoints.agent_discovery._require_report_access"),
        patch("preloop.api.endpoints.agent_discovery.get_plugin_manager") as manager,
        patch(
            "preloop.api.endpoints.agent_discovery.crud_discovered_agent_candidate"
        ) as candidates,
    ):
        manager.return_value.get_service.return_value = authorizer
        with pytest.raises(HTTPException) as exc:
            create_discovery_report(payload, account, user, MagicMock())
        assert exc.value.status_code == 403
        assert authorizer.authorize_report.call_args.kwargs == {
            "account_id": account.id,
            "current_user": user,
            "payload": payload,
        }
        candidates.record_report.assert_not_called()


@pytest.mark.parametrize("context", [{"device_report": {}}, {"device_report": None}])
def test_bound_report_cannot_fall_back_when_authorizer_is_missing(
    context: dict,
) -> None:
    """Persisted restrictions remain fail closed even during plugin outages."""
    account = SimpleNamespace(id=uuid4())
    user = SimpleNamespace(_auth_api_key=SimpleNamespace(context_data=context))
    db = MagicMock()
    with (
        patch("preloop.api.endpoints.agent_discovery._require_report_access"),
        patch("preloop.api.endpoints.agent_discovery.get_plugin_manager") as manager,
        patch(
            "preloop.api.endpoints.agent_discovery.crud_discovered_agent_candidate"
        ) as candidates,
    ):
        manager.return_value.get_service.return_value = None
        with pytest.raises(HTTPException) as exc:
            create_discovery_report(
                DiscoveryReportRequest(workstation_fingerprint="a" * 64),
                account,
                user,
                db,
            )
        assert exc.value.status_code == 503
        candidates.record_report.assert_not_called()
        db.commit.assert_not_called()


def test_unbound_report_keeps_legacy_behavior_without_plugin() -> None:
    """The optional hook does not make unbound core reporting require EE."""
    db = MagicMock()
    with (
        patch("preloop.api.endpoints.agent_discovery._require_report_access"),
        patch("preloop.api.endpoints.agent_discovery.get_plugin_manager") as manager,
        patch(
            "preloop.api.endpoints.agent_discovery.crud_discovered_agent_candidate"
        ) as candidates,
    ):
        manager.return_value.get_service.return_value = None
        candidates.record_report.return_value = []
        result = create_discovery_report(
            DiscoveryReportRequest(workstation_fingerprint="a" * 64),
            SimpleNamespace(id=uuid4()),
            SimpleNamespace(),
            db,
        )
        assert result.received == 0
        candidates.record_report.assert_called_once()
        db.commit.assert_called_once()


def test_content_upload_is_authorized_for_the_device_it_names() -> None:
    """protects=a device-bound key uploads content only for its own device;
    fails_when=the upload's fingerprint skips the report authorizer, so a key
    bound to one device can store content for another device's hashes."""
    from preloop.api.endpoints import extension_content
    from preloop.schemas.extension_content import ContentUpload

    account = SimpleNamespace(id=uuid4())
    user = SimpleNamespace(
        _auth_api_key=SimpleNamespace(context_data={"device_report": {}})
    )
    payload = ContentUpload.model_construct(
        workstation_fingerprint="b" * 64, redaction_rules="r1", items=[]
    )
    authorizer = MagicMock()
    authorizer.authorize_report.side_effect = HTTPException(403, "device mismatch")
    with (
        patch.object(extension_content, "_require_report_access"),
        patch.object(extension_content, "get_plugin_manager") as manager,
        patch.object(extension_content, "store_collected_content") as store,
    ):
        manager.return_value.get_service.return_value = authorizer
        with pytest.raises(HTTPException) as exc:
            extension_content.upload_content(payload, account, user, MagicMock())
        assert exc.value.status_code == 403
        kwargs = authorizer.authorize_report.call_args.kwargs
        assert kwargs["account_id"] == account.id
        assert kwargs["current_user"] is user
        assert kwargs["payload"].workstation_fingerprint == "b" * 64
        store.assert_not_called()
