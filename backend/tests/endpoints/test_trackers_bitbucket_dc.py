"""DC endpoint policy and encrypted persistence regression tests (no live host)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.api.endpoints import trackers
from preloop.models import models
from preloop.models.crud.tracker import crud_tracker
from preloop.schemas.tracker import TrackerTestRequest, TrackerUpdate
from preloop.sync.scanner.core import TrackerClient
from preloop.sync.trackers.factory import create_tracker_client, tracker_class_for_type

INSTANCE = "https://bitbucket.example.com/scm"
DETAILS = {"instance_url": INSTANCE, "version": "10.2"}


@pytest.fixture(autouse=True)
def dc_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRELOOP_BITBUCKET_DC_ENABLED", "true")
    monkeypatch.setenv("PRELOOP_BITBUCKET_DC_INSTANCES", '["' + INSTANCE + '"]')


def request(**overrides: object) -> TrackerTestRequest:
    data = {
        "tracker_type": "bitbucket_dc",
        "api_key": "synthetic-pat",
        "url": INSTANCE,
        "connection_details": dict(DETAILS),
    }
    data.update(overrides)
    return TrackerTestRequest(**data)


def stored_tracker() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        tracker_type="bitbucket_dc",
        auth_type="api_token",
        url=INSTANCE,
        connection_details=dict(DETAILS),
        resolved_api_key="synthetic-stored-pat",
    )


def test_schema_and_factory_keep_cloud_distinct() -> None:
    assert models.TrackerType.BITBUCKET_DC.value == "bitbucket_dc"
    assert models.TrackerType.BITBUCKET.value == "bitbucket"
    dc = tracker_class_for_type("bitbucket_dc")
    assert dc is not tracker_class_for_type("bitbucket")
    assert dc.hosts_repositories
    assert not dc.hosts_issues


@pytest.mark.asyncio
async def test_factory_and_direct_scanner_use_same_dc_adapter() -> None:
    tracker = stored_tracker()
    factory_client = await create_tracker_client(
        "bitbucket_dc", str(tracker.id), tracker.resolved_api_key, dict(DETAILS)
    )
    direct_client = TrackerClient(tracker).client
    assert type(factory_client) is type(direct_client)
    assert direct_client.api_key == "synthetic-stored-pat"


def test_disabled_config_fails_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRELOOP_BITBUCKET_DC_ENABLED")
    with pytest.raises(HTTPException) as exc:
        trackers._bitbucket_auth_details(
            models.TrackerType.BITBUCKET_DC, request(), None
        )
    assert exc.value.status_code == 400


@pytest.mark.parametrize("auth", ["oauth_token", "app_password", "basic"])
def test_only_pat_auth_is_accepted(auth: str) -> None:
    with pytest.raises(HTTPException) as exc:
        trackers._bitbucket_auth_details(
            models.TrackerType.BITBUCKET_DC, request(auth_type=auth), None
        )
    assert exc.value.status_code == 400


def test_url_mismatch_rejected_before_network() -> None:
    with pytest.raises(HTTPException, match="Instance URL must match"):
        trackers._bitbucket_auth_details(
            models.TrackerType.BITBUCKET_DC,
            request(url="https://other.example.com"),
            None,
        )


def test_stored_pat_is_reused_only_for_same_instance() -> None:
    data = request(api_key="unchanged")
    trackers._apply_tracker_auth(stored_tracker(), data)
    assert data.api_key == "synthetic-stored-pat"
    assert trackers._bitbucket_auth_details(
        models.TrackerType.BITBUCKET_DC, data, stored_tracker()
    ) == {"auth_type": "api_token"}


@pytest.mark.parametrize("provider", ["github", "bitbucket", "bitbucket_dc"])
def test_stored_pat_cannot_cross_provider_or_instance(provider: str) -> None:
    data = request(
        tracker_type=provider,
        api_key="unchanged",
        url="https://other.example.com",
        connection_details={"instance_url": "https://other.example.com"},
    )
    with pytest.raises(HTTPException):
        trackers._apply_tracker_auth(stored_tracker(), data)
    assert data.api_key == "unchanged"


@pytest.mark.asyncio
async def test_invalid_update_does_not_mutate_scope_rules() -> None:
    tracker = stored_tracker()
    user = SimpleNamespace(account_id=tracker.account_id)
    db = MagicMock()
    with (
        patch.object(
            trackers.crud_account,
            "get",
            return_value=SimpleNamespace(id=tracker.account_id),
        ),
        patch.object(
            trackers.crud_tracker, "get_by_id_and_account", return_value=tracker
        ),
        patch.object(trackers.crud_tracker, "delete_scope_rules") as delete_scopes,
        patch.object(trackers.crud_tracker, "update") as update,
    ):
        with pytest.raises(HTTPException):
            await trackers.update_tracker(
                tracker.id,
                TrackerUpdate(
                    connection_details={"instance_url": "https://other.example.com"},
                    scope_rules=[],
                ),
                current_user=user,
                db=db,
            )
        delete_scopes.assert_not_called()
        update.assert_not_called()


def test_dc_create_routes_pat_to_secret_reference() -> None:
    """Exercise CRUD's actual encryption dispatch, not an endpoint mock."""
    secret_id = uuid4()
    account_id = uuid4()
    with (
        patch(
            "preloop.models.crud.tracker.store_tracker_secret", return_value=secret_id
        ) as encrypt,
        patch(
            "preloop.models.crud.base.CRUDBase.create", return_value=MagicMock()
        ) as insert,
    ):
        crud_tracker.create(
            MagicMock(),
            obj_in={
                "name": "DC",
                "tracker_type": "bitbucket_dc",
                "account_id": account_id,
                "api_key": "synthetic-pat",
                "connection_details": dict(DETAILS),
            },
        )
    assert encrypt.call_args.kwargs["secret_value"] == "synthetic-pat"
    assert encrypt.call_args.kwargs["account_id"] == account_id
    persisted = insert.call_args.kwargs["obj_in"]
    assert persisted["api_key"] is None
    assert persisted["credentials_secret_id"] == secret_id
    assert "synthetic-pat" not in str(persisted)


@pytest.mark.parametrize(
    "injected",
    [
        {"api_key": "secret"},
        {"verify": False},
        {"private_networks": ["0.0.0.0/0"]},
        {"repository_id": True},
    ],
)
def test_config_cannot_store_secret_or_override_deployment_trust(
    injected: dict,
) -> None:
    with pytest.raises(HTTPException) as exc:
        trackers._bitbucket_auth_details(
            models.TrackerType.BITBUCKET_DC,
            request(connection_details={**DETAILS, **injected}),
            None,
        )
    assert exc.value.status_code == 400
    assert "secret" not in str(exc.value.detail)


def test_dc_rotation_reuses_secret_reference_and_clears_plaintext() -> None:
    tracker = stored_tracker()
    tracker.name = "DC"
    tracker.credentials_secret_id = uuid4()
    tracker.webhook_secret_id = None
    with (
        patch(
            "preloop.models.crud.tracker.store_tracker_secret",
            return_value=tracker.credentials_secret_id,
        ) as encrypt,
        patch(
            "preloop.models.crud.base.CRUDBase.update", return_value=tracker
        ) as update,
    ):
        crud_tracker.update(
            MagicMock(), db_obj=tracker, obj_in={"api_key": "rotated-pat"}
        )
    assert (
        encrypt.call_args.kwargs["existing_secret_id"] == tracker.credentials_secret_id
    )
    assert encrypt.call_args.kwargs["secret_value"] == "rotated-pat"
    assert update.call_args.kwargs["obj_in"]["api_key"] is None


@pytest.mark.asyncio
async def test_discovery_checks_account_ownership_before_secret_resolution() -> None:
    data = request(tracker_id=str(uuid4()), api_key="unchanged")
    user = SimpleNamespace(account_id=uuid4(), username="tester")
    with (
        patch.object(
            trackers.crud_account,
            "get",
            return_value=SimpleNamespace(id=user.account_id),
        ),
        patch.object(
            trackers.crud_tracker, "get_by_id_and_account", return_value=None
        ) as lookup,
        patch.object(
            trackers, "create_tracker_client", new_callable=AsyncMock
        ) as create,
    ):
        with pytest.raises(HTTPException) as exc:
            await trackers.test_connection_and_list_orgs(
                data, current_user=user, db=MagicMock()
            )
    assert exc.value.status_code == 404
    assert lookup.call_args.kwargs["account_id"] == user.account_id
    assert data.api_key == "unchanged"
    create.assert_not_called()


@pytest.mark.asyncio
async def test_dc_scanner_skips_jira_issue_sync() -> None:
    scanner = TrackerClient(stored_tracker())
    scanner.client = AsyncMock()
    issues, embeddings = await scanner.scan_issues(
        MagicMock(), MagicMock(), MagicMock()
    )
    assert (issues, embeddings) == ([], 0)
    scanner.client.get_issues.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "details",
    [
        {"instance_url": "http://bitbucket.example.com"},
        {"instance_url": "https://other.example.com"},
        {"instance_url": INSTANCE, "version": "7.21"},
        {"instance_url": INSTANCE, "api_key": "synthetic-pat"},
    ],
)
async def test_registration_rejects_invalid_dc_before_factory(details: dict) -> None:
    body = AsyncMock()
    body.json.return_value = {
        "name": "DC",
        "type": "bitbucket_dc",
        "api_key": "synthetic-pat",
        "auth_type": "api_token",
        "connection_details": details,
    }
    with patch.object(
        trackers, "create_tracker_client", new_callable=AsyncMock
    ) as create:
        with pytest.raises(HTTPException) as exc:
            await trackers.register_tracker(
                body,
                MagicMock(),
                current_user=SimpleNamespace(account_id=uuid4()),
                db=MagicMock(),
            )
    assert exc.value.status_code == 400
    create.assert_not_called()


@pytest.mark.asyncio
async def test_registration_persists_discovered_immutable_repository_id() -> None:
    body = AsyncMock()
    body.json.return_value = {
        "name": "DC",
        "type": "bitbucket_dc",
        "api_key": "synthetic-pat",
        "connection_details": {
            **DETAILS,
            "project_key": "PRJ",
            "repository_slug": "repo",
        },
    }
    adapter = AsyncMock()
    adapter.test_connection.return_value = SimpleNamespace(
        connected=True, message="Validated"
    )
    adapter.connection_details = {
        **DETAILS,
        "project_key": "PRJ",
        "repository_slug": "repo",
        "repository_id": 42,
        "auth_type": "api_token",
        "url": INSTANCE,
    }
    account_id = uuid4()
    created = SimpleNamespace(id=uuid4(), name="DC", tracker_type="bitbucket_dc")
    user = SimpleNamespace(account_id=account_id, email=None)
    with (
        patch.object(
            trackers,
            "create_tracker_client",
            new_callable=AsyncMock,
            return_value=adapter,
        ),
        patch.object(
            trackers.crud_account, "get", return_value=SimpleNamespace(id=account_id)
        ),
        patch.object(trackers.crud_tracker, "get_by_name", return_value=None),
        patch.object(trackers.crud_tracker, "create", return_value=created) as create,
        patch.object(trackers, "has_tracker", return_value=False),
        patch.object(trackers, "get_tracker_types", return_value=[]),
        patch.object(
            trackers.crud_tool_configuration, "get_multi_by_account", return_value=[]
        ),
        patch.object(trackers, "log_config_change"),
        patch.object(
            trackers.event_bus_service, "publish_task", new_callable=AsyncMock
        ),
    ):
        result = await trackers.register_tracker(
            body, MagicMock(), current_user=user, db=MagicMock()
        )
    assert result.id == str(created.id)
    persisted = create.call_args.kwargs["obj_in"]
    assert persisted["connection_details"] == {
        **DETAILS,
        "project_key": "PRJ",
        "repository_slug": "repo",
        "repository_id": 42,
    }
    assert persisted["auth_type"] == "api_token"
