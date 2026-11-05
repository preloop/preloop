"""Shared discovery and notices use generic hooks without an EE plugin."""

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from preloop.models.crud.runner_notice import record
from preloop.plugins import account_hooks
from preloop.plugins.account_hooks import (
    ACTION_RUNNER_FLOW_USE,
    ACTION_RUNNER_SESSION_START,
    Decision,
    VISIBLE_RUNNER,
    VisibilityProvider,
)
from tests.models.crud.test_runner_share import _runner, _user


@pytest.fixture(autouse=True)
def discovery_hooks(monkeypatch):
    monkeypatch.setattr(account_hooks, "_authorizer", None)
    monkeypatch.setattr(account_hooks, "_visibility_provider", None)
    monkeypatch.setattr(
        "preloop.api.endpoints.runner_sessions._is_account_admin",
        lambda db, user: False,
    )
    monkeypatch.setattr(
        "preloop.api.auth.router._is_account_admin", lambda db, user: False
    )


def _inventory():
    return json.loads(
        (
            Path(__file__).parents[1]
            / "fixtures/personal_runners/harness_inventory_copilot_signed_in.json"
        ).read_text()
    )


def _share(runner_ids, calls):
    def authorize(ctx, action, runner):
        if action not in (ACTION_RUNNER_SESSION_START, ACTION_RUNNER_FLOW_USE):
            return None
        calls.append((action, runner.id))
        if runner.id in runner_ids:
            return Decision("allow", ("test:runner-share",))
        return None

    account_hooks.register_authorizer(authorize)


def test_session_discovery_labels_grantees_and_filters_non_grants(
    client, db_session, test_user, monkeypatch
):
    owner = _user(db_session, test_user.account_id, "Jane Doe")
    shared = _runner(db_session, owner)
    mine = _runner(db_session, test_user)
    denied = _runner(db_session, owner)
    ephemeral = _runner(db_session, test_user)
    ephemeral.ephemeral = True
    calls = []
    _share({shared.id}, calls)
    response = client.get("/api/v1/runners?purpose=sessions")
    assert response.status_code == 200, response.text
    rows = {r["id"]: r for r in response.json()}
    assert set(rows) == {str(shared.id), str(mine.id)}
    assert rows[str(shared.id)]["shared_with_me"] is True
    assert rows[str(shared.id)]["owner_name"] == "Jane Doe"
    assert rows[str(mine.id)]["shared_with_me"] is False
    assert Counter(calls)[(ACTION_RUNNER_SESSION_START, shared.id)] == 1

    monkeypatch.setattr(
        "preloop.api.endpoints.runner_sessions._is_account_admin", lambda db, user: True
    )
    rows = client.get("/api/v1/runners?purpose=sessions").json()
    assert {r["id"] for r in rows} == {str(r.id) for r in (mine, shared, denied)}
    assert all(not r["shared_with_me"] for r in rows)


def test_session_discovery_filters_before_pagination(client, db_session, test_user):
    owner = _user(db_session, test_user.account_id, "Jane Doe")
    rows = [_runner(db_session, owner) for _ in range(104)]
    now = datetime.now(timezone.utc)
    for i, row in enumerate(rows):
        row.last_heartbeat = now - timedelta(seconds=i)
    db_session.flush()
    calls = []
    _share({r.id for r in rows[-2:]}, calls)
    response = client.get("/api/v1/runners?purpose=sessions&skip=1&limit=1")
    assert response.status_code == 200, response.text
    assert [r["id"] for r in response.json()] == [str(rows[-1].id)]
    assert all(count == 1 for count in Counter(calls).values())


def test_h3_session_discovery_labels_foreign_runner_without_owner_details(
    client, db_session, test_user, test_editor_user
):
    runner = _runner(db_session, test_editor_user)
    runner.harness_inventory = _inventory()
    runner.workstation_fingerprint = "a" * 64
    db_session.flush()

    class SharedRunnerVisibility(VisibilityProvider):
        def extra_visible_ids(self, db, account_id, resource_type):
            if account_id == test_user.account_id and resource_type == VISIBLE_RUNNER:
                return [runner.id]
            return []

    account_hooks.register_visibility_provider(SharedRunnerVisibility())
    calls = []
    _share({runner.id}, calls)
    response = client.get("/api/v1/runners?purpose=sessions")
    assert response.status_code == 200, response.text
    (item,) = response.json()
    assert item["shared_with_me"] is True
    assert item["owner_name"] is None
    assert item["registered_by_email"] is None
    assert item["workstation_fingerprint"] is None
    assert all(
        entry.get("version") is None and entry.get("account_host") is None
        for entry in item["harness_inventory"]["entries"]
    )
    assert Counter(calls)[(ACTION_RUNNER_SESSION_START, runner.id)] == 1


def test_flow_picker_labels_shared_runner_but_not_owner_or_admin(
    client, db_session, test_user, monkeypatch
):
    owner = _user(db_session, test_user.account_id, "Jane Doe")
    shared = _runner(db_session, owner)
    mine = _runner(db_session, test_user)
    denied = _runner(db_session, owner)
    for runner in (shared, mine, denied):
        runner.harness_inventory = _inventory()
    db_session.flush()
    _share({shared.id}, [])
    response = client.get("/api/v1/flows/harness-options")
    assert response.status_code == 200, response.text
    (option,) = response.json()["harnesses"]
    rows = {r["id"]: r for r in option["runners"]}
    assert set(rows) == {str(shared.id), str(mine.id)}
    assert rows[str(shared.id)]["shared_with_me"] is True
    assert rows[str(shared.id)]["owner_name"] == "Jane Doe"
    assert rows[str(mine.id)]["shared_with_me"] is False
    monkeypatch.setattr(
        "preloop.api.auth.router._is_account_admin", lambda db, user: True
    )
    (option,) = client.get("/api/v1/flows/harness-options").json()["harnesses"]
    assert len(option["runners"]) == 3
    assert all(not r["shared_with_me"] for r in option["runners"])


def test_owner_notice_endpoint_is_private(
    client, db_session, test_user, test_editor_user
):
    colleague = _user(db_session, test_user.account_id, "Jane Doe")
    for owner in (test_user, colleague, test_editor_user):
        runner = _runner(db_session, owner)
        record(
            db_session,
            runner=runner,
            actor=owner,
            title="Runner event",
            message=f"Notice for {owner.id}",
            href="/console/runners",
        )
    response = client.get("/api/v1/runner-notices")
    assert response.status_code == 200, response.text
    assert [n["message"] for n in response.json()] == [f"Notice for {test_user.id}"]
