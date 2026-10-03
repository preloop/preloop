"""Discovered DC repositories retain account/scope and immutable identity binding."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from preloop.api import common
from preloop.sync.trackers.bitbucket_dc import BitbucketDCTracker


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "binding",
    [
        {},
        {"project_key": "PRJ"},
        {"project_key": "PRJ", "repository_id": 17, "repository_slug": "old"},
    ],
)
@pytest.mark.parametrize(
    "violation", [None, "account", "scope", "project", "repository", "instance"]
)
async def test_discovered_repository_client(monkeypatch, binding, violation):
    monkeypatch.setenv("PRELOOP_BITBUCKET_DC_ENABLED", "true")
    monkeypatch.setenv(
        "PRELOOP_BITBUCKET_DC_INSTANCES", '["https://bitbucket.example.com"]'
    )
    account_id, org_id, project_id = uuid4(), uuid4(), uuid4()
    details = {"instance_url": "https://bitbucket.example.com", **binding}
    tracker = SimpleNamespace(
        id=uuid4(),
        account_id=account_id,
        tracker_type="bitbucket_dc",
        url=None,
        connection_details=details,
        auth_type="api_token",
        resolved_api_key="synthetic-pat",
    )
    org = SimpleNamespace(id=org_id, identifier="PRJ", tracker=tracker)
    meta = {
        "project_key": "PRJ",
        "repository_slug": "new",
        "repository_id": 17,
        "instance_url": details["instance_url"],
    }
    project = SimpleNamespace(
        id=project_id,
        organization_id=org_id,
        identifier="17",
        tracker_settings={
            "instance_url": "https://unapproved.example.com",
            "repository_id": 99,
        },
        meta_data=meta,
    )
    user = SimpleNamespace(account_id=account_id, username="tester")
    if violation == "account":
        user.account_id = uuid4()
    elif violation == "project":
        meta["project_key"] = "OTHER"
    elif violation == "repository":
        tracker.connection_details = {**details, "repository_id": 99}
    elif violation == "instance":
        meta["instance_url"] = "https://other.example.com"
    monkeypatch.setattr(
        common, "crud_organization", SimpleNamespace(get=Mock(return_value=org))
    )
    monkeypatch.setattr(
        common, "crud_project", SimpleNamespace(get=Mock(return_value=project))
    )
    rules = (
        []
        if violation == "scope"
        else [
            SimpleNamespace(
                scope_type="ORGANIZATION", rule_type="INCLUDE", identifier="PRJ"
            )
        ]
    )
    monkeypatch.setattr(
        common,
        "crud_tracker_scope_rule",
        SimpleNamespace(get_by_tracker=Mock(return_value=rules)),
    )
    requests = []

    def transport(request):
        requests.append(request)
        assert request.url.host == "bitbucket.example.com"
        assert "/projects/PRJ/repos/new" in request.url.path
        if request.url.path.endswith("/pull-requests"):
            return httpx.Response(200, json={"values": [], "isLastPage": True})
        return httpx.Response(
            200, json={"id": 17, "slug": "new", "project": {"key": "PRJ"}}
        )

    async def factory(**kwargs):
        return BitbucketDCTracker(
            kwargs["tracker_id"],
            kwargs["api_key"],
            kwargs["connection_details"],
            transport=httpx.MockTransport(transport),
        )

    create = AsyncMock(side_effect=factory)
    monkeypatch.setattr(common, "create_tracker_client", create)
    if violation:
        with pytest.raises(HTTPException) as exc:
            await common.get_tracker_client(org_id, project_id, Mock(), user)
        assert exc.value.status_code == 403
        create.assert_not_called()
    else:
        client = await common.get_tracker_client(org_id, project_id, Mock(), user)
        assert client.repository_id == 17
        assert (await client.list_pull_requests())["items"] == []
        assert len(requests) == 2
        assert tracker.connection_details == details
