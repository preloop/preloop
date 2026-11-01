"""Extension points for runner sharing (EE #186) in the runner session API.

The EE plugin grants access through the H4 authorizer. These tests stand in
for it with small authorizers and check what the core does with the answer:
scope attributes reach the authorizer, a grantee sees and drives only their
own sessions, the per-start consent waiver, heartbeat re-authorization and
the flow pin gate.
"""

from typing import Any, Dict, List

import pytest

from preloop.api.endpoints import runner_sessions
from preloop.api.endpoints.flows import _caller_may_use_runner
from preloop.models import models
from preloop.plugins import account_hooks
from preloop.plugins.account_hooks import (
    ACTION_RUNNER_FLOW_USE,
    ACTION_RUNNER_SESSION_START,
    AuthorizationContext,
    Decision,
)
from preloop.utils import permissions

from tests.endpoints.test_runner_sessions_api import (
    FakeService,
    _assert_audited,
    _client,
    _member,
    _runner,
    _start_body,
)


@pytest.fixture
def service():
    fake = FakeService()
    runner_sessions.register_runner_session_service(fake)
    yield fake
    runner_sessions.register_runner_session_service(None)


@pytest.fixture(autouse=True)
def _no_authorizer():
    account_hooks.register_authorizer(None)
    yield
    account_hooks.register_authorizer(None)


@pytest.fixture(autouse=True)
def _rbac(monkeypatch):
    # Owner-only access is an RBAC-plugin (EE) rule. Core admits every member.
    monkeypatch.setattr(permissions, "_plugin_require_permission", object())


@pytest.fixture(autouse=True)
def _no_waiver():
    account_hooks.register_start_consent_waiver(None)
    yield
    account_hooks.register_start_consent_waiver(None)


def _scoped_share(grantee_id: str, harness: str, directory: str):
    """Authorizer granting one user sessions on one harness in one directory."""
    seen: List[Dict[str, Any]] = []

    def authorizer(ctx: AuthorizationContext, action: str, resource: Any):
        if action != ACTION_RUNNER_SESSION_START:
            return None
        seen.append(dict(ctx.attributes))
        if ctx.attributes.get("default_allowed"):
            return None
        if str(ctx.user.id) != grantee_id:
            return None
        attrs = ctx.attributes
        if attrs.get("harness") not in (None, harness):
            return Decision("deny", ("ee:share",), "runner_share_harness")
        if attrs.get("workspace_kind") == "tracker_checkout":
            return Decision("deny", ("ee:share",), "runner_share_directory")
        if attrs.get("workspace_id") not in (None, directory):
            return Decision("deny", ("ee:share",), "runner_share_directory")
        return Decision("allow", ("ee:share",))

    return authorizer, seen


def _waive(db, user, runner):
    return "admin_override"


def test_start_passes_scope_attributes(db_session, test_user, service):
    runner = _runner(db_session, test_user)
    member = _member(db_session, test_user, "member")
    authorizer, seen = _scoped_share(str(member.id), "copilot_cli", "dir_api")
    account_hooks.register_authorizer(authorizer)
    account_hooks.register_start_consent_waiver(_waive)
    response = _client(db_session, member).post(
        f"/api/v1/runners/{runner.id}/sessions", json=_start_body()
    )
    assert response.status_code == 202, response.text
    start = [a for a in seen if a["operation"] == "start"][0]
    assert start["harness"] == "copilot_cli"
    assert start["workspace_kind"] == "authorized_directory"
    assert start["workspace_id"] == "dir_api"
    assert start["default_allowed"] is False
    row = _assert_audited(db_session, runner, member, "runner_session.start_requested")
    assert row.details["access"] == "share"
    assert row.details["access_rule_ids"] == ["ee:share"]
    assert row.details["owner_user_id"] == str(test_user.id)
    assert row.details["consent_decision"] == "waived"


@pytest.mark.parametrize(
    "workspace, harness, reason",
    [
        ({"kind": "authorized_directory", "id": "dir_docs"}, "copilot_cli", "dir"),
        ({"kind": "authorized_directory", "id": "dir_api"}, "cursor_cli", "harness"),
    ],
)
def test_start_outside_the_share_is_refused(
    db_session, test_user, service, workspace, harness, reason
):
    runner = _runner(db_session, test_user)
    member = _member(db_session, test_user, "member")
    authorizer, _seen = _scoped_share(str(member.id), "copilot_cli", "dir_api")
    account_hooks.register_authorizer(authorizer)
    account_hooks.register_start_consent_waiver(_waive)
    response = _client(db_session, member).post(
        f"/api/v1/runners/{runner.id}/sessions",
        json=_start_body(workspace=workspace, harness=harness),
    )
    assert response.status_code == 403
    assert reason in response.json()["detail"]["code"]
    assert not service.plans


def test_options_show_only_the_shared_scope(db_session, test_user, service):
    runner = _runner(db_session, test_user)
    member = _member(db_session, test_user, "member")
    authorizer, _seen = _scoped_share(str(member.id), "copilot_cli", "dir_api")
    account_hooks.register_authorizer(authorizer)
    body = (
        _client(db_session, member)
        .get(f"/api/v1/runners/{runner.id}/session-options")
        .json()
    )
    assert [h["harness"] for h in body["harnesses"]] == ["copilot_cli"]
    assert [d["id"] for d in body["authorized_directories"]] == ["dir_api"]
    assert body["checkout_sources"] == []
    owner_body = (
        _client(db_session, test_user)
        .get(f"/api/v1/runners/{runner.id}/session-options")
        .json()
    )
    assert len(owner_body["authorized_directories"]) == 3


def test_grantee_sees_and_drives_only_own_sessions(db_session, test_user, service):
    runner = _runner(db_session, test_user)
    member = _member(db_session, test_user, "member")
    authorizer, _seen = _scoped_share(str(member.id), "copilot_cli", "dir_api")
    account_hooks.register_authorizer(authorizer)
    account_hooks.register_start_consent_waiver(_waive)
    from tests.permission_helpers import grant_permission

    grant_permission(db_session, test_user, "execute_flows")
    owner_start = _client(db_session, test_user).post(
        f"/api/v1/runners/{runner.id}/sessions", json=_start_body()
    )
    assert owner_start.status_code == 202, owner_start.text
    owner_session = owner_start.json()["session_id"]
    member_client = _client(db_session, member)
    started = member_client.post(
        f"/api/v1/runners/{runner.id}/sessions", json=_start_body()
    )
    assert started.status_code == 202, started.text
    mine = started.json()["session_id"]
    listed = member_client.get(f"/api/v1/runners/{runner.id}/sessions").json()
    assert [item["session_id"] for item in listed] == [mine]
    for path, body in (("turns", {"text": "hi"}), ("stop", {"mode": "kill"})):
        response = member_client.post(
            f"/api/v1/runner-sessions/{owner_session}/{path}", json=body
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "not_session_actor"
    assert (
        member_client.post(
            f"/api/v1/runner-sessions/{mine}/turns", json={"text": "hi"}
        ).status_code
        == 202
    )


def test_without_waiver_owner_approval_is_still_required(
    db_session, test_user, service
):
    runner = _runner(db_session, test_user)
    member = _member(db_session, test_user, "member")
    authorizer, _seen = _scoped_share(str(member.id), "copilot_cli", "dir_api")
    account_hooks.register_authorizer(authorizer)
    response = _client(db_session, member).post(
        f"/api/v1/runners/{runner.id}/sessions", json=_start_body()
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "consent_request_id_required"


def test_no_authorizer_keeps_runner_owner_only(db_session, test_user, service):
    runner = _runner(db_session, test_user)
    member = _member(db_session, test_user, "member")
    account_hooks.register_start_consent_waiver(_waive)
    response = _client(db_session, member).post(
        f"/api/v1/runners/{runner.id}/sessions", json=_start_body()
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "not_runner_owner"


def _live_row(db, runner, actor) -> models.RunnerRemoteSession:
    from datetime import datetime, timezone

    row = models.RunnerRemoteSession(
        account_id=runner.account_id,
        runner_id=runner.id,
        actor_user_id=actor.id,
        harness="copilot_cli",
        workspace={"kind": "authorized_directory", "id": "dir_api", "label": "api"},
        workspace_kind="authorized_directory",
        state="running",
        start_sent_at=datetime.now(timezone.utc),
        requested_at=datetime.now(timezone.utc),
        idle_timeout_seconds=1800,
    )
    db.add(row)
    db.flush()
    return row


def test_heartbeat_stops_sessions_after_revoke(db_session, test_user):
    runner = _runner(db_session, test_user)
    member = _member(db_session, test_user, "member")
    granted = {"value": True}

    def authorizer(ctx, action, resource):
        if action != ACTION_RUNNER_SESSION_START or ctx.attributes["default_allowed"]:
            return None
        assert ctx.attributes["operation"] == "heartbeat"
        assert ctx.attributes["workspace_id"] == "dir_api"
        if granted["value"]:
            return Decision("allow", ("ee:share",))
        return Decision("deny", ("ee:share",), "runner_share_revoked")

    account_hooks.register_authorizer(authorizer)
    owner_row = _live_row(db_session, runner, test_user)
    grantee_row = _live_row(db_session, runner, member)

    assert runner_sessions.reauthorize_live_sessions(db_session, runner) == []
    granted["value"] = False
    stopped = runner_sessions.reauthorize_live_sessions(db_session, runner)
    assert stopped == [str(grantee_row.id)]
    db_session.refresh(grantee_row)
    db_session.refresh(owner_row)
    assert grantee_row.stop_mode == "kill"
    assert grantee_row.stop_reason == "access_revoked"
    assert owner_row.stop_mode is None
    # Already stopping: the next beat does not ask again.
    assert runner_sessions.reauthorize_live_sessions(db_session, runner) == []


def test_flow_pin_gate_asks_flow_use(db_session, test_user, monkeypatch):
    monkeypatch.setattr(
        "preloop.api.auth.router._is_account_admin", lambda db, user: False
    )

    runner = _runner(db_session, test_user)
    member = _member(db_session, test_user, "member")
    assert _caller_may_use_runner(db_session, member, runner) is False

    seen: List[Dict[str, Any]] = []

    def authorizer(ctx, action, resource):
        if action != ACTION_RUNNER_FLOW_USE:
            return None
        seen.append(dict(ctx.attributes))
        return Decision("allow", ("ee:share",))

    account_hooks.register_authorizer(authorizer)
    assert (
        _caller_may_use_runner(db_session, member, runner, harness="claude_code")
        is True
    )
    assert seen[0]["harness"] == "claude_code"
    assert seen[0]["operation"] == "pin"
    # An allow without rule ids does not widen.
    account_hooks.register_authorizer(lambda ctx, action, res: Decision("allow"))
    assert _caller_may_use_runner(db_session, member, runner) is False


def test_start_consent_waiver_defaults_to_none(db_session, test_user):
    runner = _runner(db_session, test_user)
    assert account_hooks.start_consent_waiver(db_session, test_user, runner) is None
    account_hooks.register_start_consent_waiver(lambda db, u, r: "admin_override")
    assert (
        account_hooks.start_consent_waiver(db_session, test_user, runner)
        == "admin_override"
    )
