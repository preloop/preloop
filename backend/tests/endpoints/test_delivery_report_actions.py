"""Tracker actions from the Delivery Report page and the run (#2021)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.api.endpoints import mcp
from preloop.models.crud import (
    crud_account,
    crud_delivery_report_policy,
    crud_flow_execution,
)
from preloop.models.models.audit_log import AuditLog
from preloop.services import delivery_report_actions as actions
from preloop.services import delivery_report_views as views
from preloop.sync.exceptions import TrackerResponseError
from tests.endpoints.test_delivery_reports import _flow, _run

BASE = "/api/v1/delivery-reports"
ISSUE_URL = "https://github.com/example/app/issues/7"
PR_URL = "https://github.com/example/app/pull/10"
ASSESSED_AT = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


def _fixture_report() -> Dict[str, Any]:
    """One follow-up proposal and one reopen request, as a run records them."""
    return {
        "status": "success",
        "window": {"from": "2026-10-17T07:00:00Z", "to": "2026-10-24T07:00:00Z"},
        "shipped": [
            {
                "ref": "example/app#10",
                "url": PR_URL,
                "project": "example/app",
                "head_sha": "aaa111",
                "issues": ["example/app#7"],
            }
        ],
        "closed_issues": [
            {
                "ref": "example/app#7",
                "url": ISSUE_URL,
                "updated_at": ASSESSED_AT.isoformat(),
                "closing_head_sha": "aaa111",
                "closed_by": ["example/app#10"],
                "purpose": {
                    "criteria": [
                        {"text": "Search answers in 200 ms", "met": False},
                        {"text": "Results are paginated", "met": True},
                    ]
                },
            }
        ],
        "follow_ups": [
            {
                "title": "Add a test for the search hot path",
                "reason": "The hot path changed without a test.",
                "evidence": ["backend/search.py:42"],
                "ref": "example/app#10",
            }
        ],
        "reopen_requests": [
            {
                "ref": "example/app#7",
                "url": ISSUE_URL,
                "revision": "aaa111",
                "reason": "The last comment measures 3 s.",
                "evidence": ["comment measuring 3 s"],
            }
        ],
    }


@pytest.fixture(autouse=True)
def account_owner(db_session: Session, test_user: Any) -> Any:
    """Make the caller the account's primary user, whom the OSS check admits."""
    account = crud_account.get(db_session, id=test_user.account_id)
    account.primary_user_id = test_user.id
    db_session.flush()
    return account


class FakeTracker:
    """Records every tracker call instead of making it."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def create_issue(self, project_key: str, issue_data: Any) -> Any:
        self.calls.append(("create_issue", project_key, issue_data))
        return SimpleNamespace(url="https://github.com/example/app/issues/99")

    async def update_issue(self, issue_id: str, issue_data: Any) -> Any:
        self.calls.append(("update_issue", issue_id, issue_data))
        return SimpleNamespace()

    async def add_comment(self, issue_id: str, comment: str) -> Any:
        self.calls.append(("add_comment", issue_id, comment))
        return SimpleNamespace()


@pytest.fixture
def tracker(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """A fake tracker plus the current issue and closing PR state."""
    fake = FakeTracker()
    state: Dict[str, Any] = {"fake": fake, "issue_updated": ASSESSED_AT, "head": None}
    project = SimpleNamespace(
        id=uuid.uuid4(), organization_id=uuid.uuid4(), identifier="example/app"
    )

    async def client(*args: Any, **kwargs: Any) -> FakeTracker:
        return fake

    monkeypatch.setattr(actions, "get_tracker_client", client)
    monkeypatch.setattr(
        actions.crud_project,
        "get_by_slug_or_identifier",
        lambda db, *, slug_or_identifier, account_id=None: (
            project if slug_or_identifier == "example/app" else None
        ),
    )
    monkeypatch.setattr(
        views.crud_issue,
        "get_by_external_url",
        lambda db, *, external_url, account_id=None: SimpleNamespace(
            key="example/app#7",
            external_id="7",
            project=project,
            project_id=project.id,
            updated_at=state["issue_updated"],
        ),
    )
    monkeypatch.setattr(
        views.crud_issue_cost,
        "get_pull_request",
        lambda db, *, account_id, pr_key: (
            SimpleNamespace(head_sha=state["head"]) if state["head"] else None
        ),
    )
    return state


def _audit_rows(db: Session, account_id: Any) -> List[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.account_id == str(account_id),
            AuditLog.action == actions.AUDIT_ACTION,
        )
        .order_by(AuditLog.timestamp)
        .all()
    )


def _report_run(db: Session, account_id: Any) -> Any:
    return _run(
        db, _flow(db, account_id, report=True), hours=0, result=_fixture_report()
    )


def test_default_policy_keeps_both_proposals_in_the_report(
    client, db_session: Session, test_user, tracker: Dict[str, Any]
) -> None:
    run = _report_run(db_session, test_user.account_id)
    for tool, status in (("create_issue", None), ("update_issue", "open")):
        allowed, _ = actions.run_write_decision(
            db_session,
            account_id=test_user.account_id,
            user_id=test_user.id,
            execution_id=run.id,
            tool=tool,
            target="example/app#7",
            status=status,
        )
        assert allowed is False

    body = client.get(f"{BASE}/{run.id}").json()
    assert len(body["report"]["follow_ups"]) == 1
    assert len(body["report"]["reopen_requests"]) == 1
    assert body["stale_reopen_requests"] == {}
    assert tracker["fake"].calls == []
    denied = [
        r.details["denied_by"] for r in _audit_rows(db_session, test_user.account_id)
    ]
    assert denied == [
        "tracker_writes.create_follow_up=deny",
        "tracker_writes.reopen=approval",
    ]


def test_follow_up_fallback_uses_shipped_scope_despite_other_open_work(
    client, db_session: Session, test_user, tracker: Dict[str, Any]
) -> None:
    report = _fixture_report()
    report["follow_ups"][0].pop("ref")
    report["open_past_window"] = [{"ref": "example/other#20"}]
    run = _run(
        db_session,
        _flow(db_session, test_user.account_id, report=True),
        hours=0,
        result=report,
    )
    response = client.post(
        f"{BASE}/{run.id}/actions", json={"kind": "open_issue", "index": 0}
    )
    assert response.status_code == 200, response.text
    assert tracker["fake"].calls[0][:2] == ("create_issue", "example/app")


def test_open_issue_click_creates_one_labelled_issue(
    client, db_session: Session, test_user, tracker: Dict[str, Any]
) -> None:
    run = _report_run(db_session, test_user.account_id)
    response = client.post(
        f"{BASE}/{run.id}/actions", json={"kind": "open_issue", "index": 0}
    )
    assert response.status_code == 200, response.text
    decision = response.json()["decision"]
    assert decision["status"] == "applied"
    assert decision["issue_url"] == "https://github.com/example/app/issues/99"

    [(name, project_key, data)] = tracker["fake"].calls
    assert (name, project_key) == ("create_issue", "example/app")
    assert data.labels == ["preloop:proposed"]
    assert "backend/search.py:42" in data.description
    assert f"/console/delivery-reports/{run.id}" in data.description

    again = client.post(
        f"{BASE}/{run.id}/actions", json={"kind": "open_issue", "index": 0}
    )
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "already_decided"
    assert len(tracker["fake"].calls) == 1

    db_session.refresh(run)
    assert run.result["proposal_actions"]["follow_up:0"]["status"] == "applied"
    [row] = _audit_rows(db_session, test_user.account_id)
    assert row.status == "success"
    assert row.details["allowed_by"] == "click"


def test_reopen_click_reopens_with_the_revision_and_unmet_criterion(
    client, db_session: Session, test_user, tracker: Dict[str, Any]
) -> None:
    run = _report_run(db_session, test_user.account_id)
    tracker["head"] = "aaa111"
    response = client.post(
        f"{BASE}/{run.id}/actions", json={"kind": "reopen", "ref": "example/app#7"}
    )
    assert response.status_code == 200, response.text

    update, comment = tracker["fake"].calls
    assert update[0] == "update_issue" and update[2].status == "open"
    assert comment[0] == "add_comment" and comment[1] == "example/app#7"
    assert "`aaa111`" in comment[2]
    assert "Search answers in 200 ms" in comment[2]
    assert "Results are paginated" not in comment[2]
    [row] = _audit_rows(db_session, test_user.account_id)
    assert (row.status, row.details["revision"]) == ("success", "aaa111")


@pytest.mark.parametrize("change", ["new_commit", "issue_edit"])
def test_stale_reopen_request_cannot_be_applied(
    change: str, client, db_session: Session, test_user, tracker: Dict[str, Any]
) -> None:
    run = _report_run(db_session, test_user.account_id)
    if change == "new_commit":
        tracker["head"] = "bbb222"
    else:
        tracker["issue_updated"] = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)

    assert (
        "example/app#7"
        in client.get(f"{BASE}/{run.id}").json()["stale_reopen_requests"]
    )
    response = client.post(
        f"{BASE}/{run.id}/actions", json={"kind": "reopen", "ref": "example/app#7"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale"
    assert tracker["fake"].calls == []
    [row] = _audit_rows(db_session, test_user.account_id)
    assert row.status == "denied"


def test_close_is_refused_and_logged(
    client, db_session: Session, test_user, tracker: Dict[str, Any]
) -> None:
    run = _report_run(db_session, test_user.account_id)
    response = client.post(
        f"{BASE}/{run.id}/actions", json={"kind": "close", "ref": "example/app#7"}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "close_denied"
    assert tracker["fake"].calls == []
    [row] = _audit_rows(db_session, test_user.account_id)
    assert (row.status, row.details["kind"]) == ("denied", "close")


def test_dismiss_needs_a_reason_and_blocks_the_action(
    client, db_session: Session, test_user, tracker: Dict[str, Any]
) -> None:
    run = _report_run(db_session, test_user.account_id)
    url = f"{BASE}/{run.id}/actions"
    missing = client.post(url, json={"kind": "dismiss", "key": "reopen:example/app#7"})
    assert missing.status_code == 422
    response = client.post(
        url,
        json={"kind": "dismiss", "key": "reopen:example/app#7", "reason": "Known"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["decision"]["status"] == "dismissed"
    assert (
        _audit_rows(db_session, test_user.account_id)[0].details["allowed_by"]
        == "click"
    )
    blocked = client.post(url, json={"kind": "reopen", "ref": "example/app#7"})
    assert blocked.status_code == 409
    assert tracker["fake"].calls == []


@pytest.mark.parametrize("plugin_present", [False, True])
@pytest.mark.parametrize(
    "action",
    [
        {"kind": "open_issue", "index": 0},
        {"kind": "reopen", "ref": "example/app#7"},
        {"kind": "dismiss", "key": "follow_up:0", "reason": "Known"},
    ],
)
def test_member_actions_follow_edition_permissions(
    db_session: Session,
    test_viewer_user: Any,
    tracker: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    plugin_present: bool,
    action: dict,
) -> None:
    from fastapi.testclient import TestClient

    from preloop.api.app import create_app
    from preloop.api.auth import get_current_active_user
    from preloop.models.db.session import get_db_session

    from preloop.utils import permissions

    checked = []
    if plugin_present:

        def plugin(permission):
            def decorate(function):
                def wrapper(*args, **kwargs):
                    checked.append(permission)
                    if not permissions.user_holds_permission(
                        kwargs["db"], kwargs["current_user"], permission
                    ):
                        raise HTTPException(status_code=403, detail="RBAC denied")
                    return function(*args, **kwargs)

                return wrapper

            return decorate

        monkeypatch.setattr(permissions, "_plugin_require_permission", plugin)
        monkeypatch.setattr(permissions, "_rbac_checks_enabled", lambda: True)

    # The viewer's own account, so only the missing permission can refuse it.
    run = _report_run(db_session, test_viewer_user.account_id)
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_viewer_user
    with TestClient(app) as viewer_client:
        response = viewer_client.post(f"{BASE}/{run.id}/actions", json=action)
    assert response.status_code == (403 if plugin_present else 200), response.text
    if plugin_present:
        assert tracker["fake"].calls == []
        assert (
            "create_issues" if action["kind"] == "open_issue" else "edit_issues"
        ) in checked

    else:
        decision = response.json()["decision"]
        assert decision["user_id"] == str(test_viewer_user.id)
        assert decision["status"] == (
            "dismissed" if action["kind"] == "dismiss" else "applied"
        )
        assert bool(tracker["fake"].calls) is (action["kind"] != "dismiss")


def test_actions_are_404_for_a_foreign_or_unknown_run(
    client, db_session: Session, test_user, tracker: Dict[str, Any]
) -> None:
    other = _run(
        db_session,
        _flow(db_session, test_user.account_id, report=False),
        hours=0,
        result=_fixture_report(),
    )
    for execution_id in (other.id, uuid.uuid4()):
        response = client.post(
            f"{BASE}/{execution_id}/actions", json={"kind": "open_issue", "index": 0}
        )
        assert response.status_code == 404
    run = _report_run(db_session, test_user.account_id)
    unknown = client.post(
        f"{BASE}/{run.id}/actions", json={"kind": "open_issue", "index": 5}
    )
    assert unknown.status_code == 404


@pytest.mark.parametrize("key", ["follow_up:00", "follow_up:+0"])
def test_a_non_canonical_dismiss_key_blocks_the_canonical_action(
    key: str, client, db_session: Session, test_user, tracker: Dict[str, Any]
) -> None:
    run = _report_run(db_session, test_user.account_id)
    url = f"{BASE}/{run.id}/actions"
    response = client.post(url, json={"kind": "dismiss", "key": key, "reason": "No"})
    if key == "follow_up:+0":
        assert response.status_code == 404
        return
    assert response.status_code == 200, response.text
    db_session.refresh(run)
    assert list(run.result["proposal_actions"]) == ["follow_up:0"]
    blocked = client.post(url, json={"kind": "open_issue", "index": 0})
    assert blocked.status_code == 409
    assert tracker["fake"].calls == []


def test_a_non_ascii_digit_dismiss_key_is_not_found(
    client, db_session: Session, test_user, tracker: Dict[str, Any]
) -> None:
    run = _report_run(db_session, test_user.account_id)
    response = client.post(
        f"{BASE}/{run.id}/actions",
        json={"kind": "dismiss", "key": "follow_up:\u00b2", "reason": "No"},
    )
    assert response.status_code == 404


def test_a_definite_refusal_frees_the_proposal_for_another_click(
    client, db_session: Session, test_user, tracker: Dict[str, Any]
) -> None:
    run = _report_run(db_session, test_user.account_id)
    url = f"{BASE}/{run.id}/actions"
    tracker["fake"].create_issue = AsyncMock(
        side_effect=TrackerResponseError("Validation failed", status_code=422)
    )
    failed = client.post(url, json={"kind": "open_issue", "index": 0})
    assert failed.status_code == 502
    db_session.refresh(run)
    assert "follow_up:0" not in (run.result.get("proposal_actions") or {})
    del tracker["fake"].create_issue
    assert client.post(url, json={"kind": "open_issue", "index": 0}).status_code == 200


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("timed out"),
        TrackerResponseError("Bad gateway", status_code=502),
        TrackerResponseError("No status"),
    ],
    ids=["timeout", "5xx", "no_status"],
)
@pytest.mark.parametrize("kind", ["open_issue", "reopen"])
def test_an_unknown_tracker_outcome_is_kept_until_dismissed(
    kind: str,
    error: Exception,
    client,
    db_session: Session,
    test_user,
    tracker: Dict[str, Any],
) -> None:
    run = _report_run(db_session, test_user.account_id)
    url = f"{BASE}/{run.id}/actions"
    method = "create_issue" if kind == "open_issue" else "update_issue"
    setattr(tracker["fake"], method, AsyncMock(side_effect=error))
    body = (
        {"kind": "open_issue", "index": 0}
        if kind == "open_issue"
        else {"kind": "reopen", "ref": "example/app#7"}
    )
    key = "follow_up:0" if kind == "open_issue" else "reopen:example/app#7"
    assert client.post(url, json=body).status_code == 502
    db_session.refresh(run)
    assert run.result["proposal_actions"][key]["status"] == "unconfirmed"

    retry = client.post(url, json=body)
    assert retry.status_code == 409
    assert retry.json()["detail"]["code"] == "unconfirmed"
    assert getattr(tracker["fake"], method).await_count == 1

    dismissed = client.post(
        url, json={"kind": "dismiss", "key": key, "reason": "It landed"}
    )
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["decision"]["status"] == "dismissed"


def test_a_stuck_pending_claim_can_only_be_dismissed_once_old(
    client, db_session: Session, test_user, tracker: Dict[str, Any]
) -> None:
    run = _report_run(db_session, test_user.account_id)
    url = f"{BASE}/{run.id}/actions"
    dismiss = {"kind": "dismiss", "key": "follow_up:0", "reason": "Stuck"}
    for at, code in (
        (datetime.now(timezone.utc), "in_progress"),
        (datetime.now(timezone.utc) - actions.STUCK_PENDING_AFTER, None),
    ):
        crud_flow_execution.set_delivery_report_action(
            db_session,
            execution_id=run.id,
            key="follow_up:0",
            record={"kind": "open_issue", "status": "pending", "at": at.isoformat()},
        )
        click = client.post(url, json={"kind": "open_issue", "index": 0})
        assert click.status_code == 409
        response = client.post(url, json=dismiss)
        if code:
            assert response.json()["detail"]["code"] == code
        else:
            assert response.status_code == 200, response.text
    assert tracker["fake"].calls == []


class TestRunWrites:
    """Writes the report run makes through the MCP tools."""

    def _guard(
        self,
        db: Session,
        user: Any,
        monkeypatch: pytest.MonkeyPatch,
        run: Any,
        **kwargs: Any,
    ) -> Any:
        monkeypatch.setattr(
            mcp, "_delivery_report_execution_id", lambda db, current_user: str(run.id)
        )
        return mcp._guard_delivery_report_write(db, user, **kwargs)

    def test_automatic_creation_runs_under_the_policy(
        self, db_session: Session, test_user, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = _report_run(db_session, test_user.account_id)
        with pytest.raises(HTTPException) as refused:
            self._guard(
                db_session,
                test_user,
                monkeypatch,
                run,
                tool="create_issue",
                target="example/app",
            )
        assert refused.value.status_code == 403

        crud_delivery_report_policy.save_overrides(
            db_session,
            account_id=test_user.account_id,
            overrides={"tracker_writes": {"create_follow_up": "allow"}},
        )
        assert self._guard(
            db_session,
            test_user,
            monkeypatch,
            run,
            tool="create_issue",
            target="example/app",
        ) == str(run.id)
        last = _audit_rows(db_session, test_user.account_id)[-1]
        assert last.details["allowed_by"] == "tracker_writes.create_follow_up=allow"

    @pytest.mark.asyncio
    async def test_automatic_creation_tool_adds_label_and_report_link(
        self, db_session: Session, test_user: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An allowed report tool call creates a labelled, linked follow-up."""
        run = _report_run(db_session, test_user.account_id)
        crud_delivery_report_policy.save_overrides(
            db_session,
            account_id=test_user.account_id,
            overrides={"tracker_writes": {"create_follow_up": "allow"}},
        )
        project = SimpleNamespace(
            id=uuid.uuid4(), slug="example/app", identifier="example/app"
        )
        create = AsyncMock(
            return_value=SimpleNamespace(
                id="synthetic-99", url="https://github.com/example/app/issues/99"
            )
        )
        monkeypatch.setattr(mcp, "_get_tool_db", lambda: db_session)
        monkeypatch.setattr(
            mcp,
            "get_http_request",
            lambda: SimpleNamespace(headers={"authorization": "Bearer test-token"}),
        )
        monkeypatch.setattr(
            mcp, "get_user_from_token_if_valid", AsyncMock(return_value=test_user)
        )
        monkeypatch.setattr(
            mcp.crud_project,
            "get_by_slug_or_identifier",
            lambda db, *, slug_or_identifier, account_id: project,
        )
        monkeypatch.setattr(
            mcp, "_delivery_report_execution_id", lambda db, current_user: str(run.id)
        )
        monkeypatch.setattr(mcp, "api_create_issue", create)

        result = await mcp.create_issue.__wrapped__(  # type: ignore[attr-defined]
            project="example/app",
            title="Add a test for the search hot path",
            description="The hot path changed without a test. backend/search.py:42",
            prevent_duplicates=False,
        )

        assert result.status == "created"
        assert create.await_args is not None
        issue = create.await_args.kwargs["issue"]
        assert issue.labels == ["preloop:proposed"]
        assert "backend/search.py:42" in issue.description
        assert f"/console/delivery-reports/{run.id}" in issue.description
        last = _audit_rows(db_session, test_user.account_id)[-1]
        assert isinstance(last.details, dict)
        assert last.details["allowed_by"] == "tracker_writes.create_follow_up=allow"

    @pytest.mark.parametrize("status", ["closed", "Done", "resolved"])
    def test_close_from_a_run_is_always_refused(
        self,
        status: str,
        db_session: Session,
        test_user,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run = _report_run(db_session, test_user.account_id)
        crud_delivery_report_policy.save_overrides(
            db_session,
            account_id=test_user.account_id,
            overrides={
                "tracker_writes": {"create_follow_up": "allow", "reopen": "allow"}
            },
        )
        with pytest.raises(HTTPException) as refused:
            self._guard(
                db_session,
                test_user,
                monkeypatch,
                run,
                tool="update_issue",
                target="example/app#7",
                status=status,
            )
        assert refused.value.status_code == 403
        [row] = _audit_rows(db_session, test_user.account_id)
        assert (row.status, row.details["denied_by"]) == (
            "denied",
            "tracker_writes.close=deny",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "edit",
        [
            {"title": "Retitled"},
            {"description": "Replaced"},
            {"labels": ["x"]},
            {"assignee": "someone"},
            {"priority": "high"},
        ],
    )
    async def test_reopen_cannot_carry_another_edit(
        self,
        edit: Dict[str, Any],
        db_session: Session,
        test_user,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run = _report_run(db_session, test_user.account_id)
        crud_delivery_report_policy.save_overrides(
            db_session,
            account_id=test_user.account_id,
            overrides={"tracker_writes": {"reopen": "allow"}},
        )
        monkeypatch.setattr(
            mcp, "_delivery_report_execution_id", lambda db, current_user: str(run.id)
        )
        monkeypatch.setattr(mcp, "_get_tool_db", lambda: db_session)
        monkeypatch.setattr(mcp, "_tool_user", AsyncMock(return_value=test_user))
        with pytest.raises(HTTPException) as refused:
            await mcp.update_issue.__wrapped__(
                issue="example/app#7", status="open", **edit
            )
        assert refused.value.status_code == 403
        [row] = _audit_rows(db_session, test_user.account_id)
        assert row.status == "denied"
        assert row.details["denied_by"].startswith("a report run may only reopen")

    def test_an_allowed_write_is_logged_as_a_decision_not_an_outcome(
        self, db_session: Session, test_user, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = _report_run(db_session, test_user.account_id)
        crud_delivery_report_policy.save_overrides(
            db_session,
            account_id=test_user.account_id,
            overrides={"tracker_writes": {"reopen": "allow"}},
        )
        self._guard(
            db_session,
            test_user,
            monkeypatch,
            run,
            tool="update_issue",
            target="example/app#7",
            status="open",
        )
        [row] = _audit_rows(db_session, test_user.account_id)
        assert row.status == "allowed"
        assert row.details["allowed_by"] == "tracker_writes.reopen=allow"

    def test_calls_outside_a_report_run_are_not_gated(
        self, db_session: Session, test_user, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            mcp, "_delivery_report_execution_id", lambda db, current_user: None
        )
        assert (
            mcp._guard_delivery_report_write(
                db_session, test_user, tool="update_issue", target="x", status="closed"
            )
            is None
        )
        assert _audit_rows(db_session, test_user.account_id) == []

    def test_execution_id_resolves_only_report_runs(
        self, db_session: Session, test_user, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = _report_run(db_session, test_user.account_id)
        other = _run(
            db_session,
            _flow(db_session, test_user.account_id, report=False),
            hours=0,
            result={},
        )
        import preloop.services.dynamic_fastmcp_http as http

        for execution, expected in ((run, str(run.id)), (other, None)):
            monkeypatch.setattr(
                http,
                "get_current_user_context",
                lambda execution=execution: SimpleNamespace(
                    flow_execution_id=str(execution.id)
                ),
            )
            assert mcp._delivery_report_execution_id(db_session, test_user) == expected


def test_set_action_keeps_other_result_keys(db_session: Session, test_user) -> None:
    run = _report_run(db_session, test_user.account_id)
    run.result = {**run.result, "_private_publication": {"phase": "complete"}}
    db_session.commit()
    crud_flow_execution.set_delivery_report_action(
        db_session, execution_id=run.id, key="follow_up:0", record={"status": "x"}
    )
    db_session.refresh(run)
    assert run.result["_private_publication"] == {"phase": "complete"}
    assert run.result["follow_ups"][0]["title"].startswith("Add a test")


@pytest.mark.parametrize(
    "proposal, action",
    [
        ("follow_ups", {"kind": "dismiss", "key": "follow_up:0", "reason": "Known"}),
        (
            "reopen_requests",
            {"kind": "dismiss", "key": "reopen:example/app#7", "reason": "Known"},
        ),
        ("reopen_requests", {"kind": "reopen", "ref": "example/app#7"}),
    ],
)
def test_run_applied_proposals_cannot_be_decided_again(
    client,
    db_session: Session,
    test_user,
    tracker: Dict[str, Any],
    proposal: str,
    action: dict,
) -> None:
    run = _report_run(db_session, test_user.account_id)
    report = _fixture_report()
    report[proposal][0].update(
        {"issue_url": ISSUE_URL} if proposal == "follow_ups" else {"applied": True}
    )
    run.result = report
    db_session.commit()
    response = client.post(f"{BASE}/{run.id}/actions", json=action)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "already_decided"
    assert tracker["fake"].calls == []


def test_report_links_use_instance_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actions.settings, "preloop_url", "https://internal.example/")
    monkeypatch.delenv("PRELOOP_URL", raising=False)
    assert (
        actions.report_link("run")
        == "https://internal.example/console/delivery-reports/run"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["closed", " Done ", "resolved", "completed", "close"]
)
async def test_report_cannot_create_an_already_closed_issue(
    db_session: Session,
    test_user,
    tracker: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    run = _report_run(db_session, test_user.account_id)
    crud_delivery_report_policy.save_overrides(
        db_session,
        account_id=test_user.account_id,
        overrides={"tracker_writes": {"create_follow_up": "allow"}},
    )
    monkeypatch.setattr(
        mcp, "_delivery_report_execution_id", lambda db, user: str(run.id)
    )
    monkeypatch.setattr(mcp, "_get_tool_db", lambda: db_session)
    monkeypatch.setattr(
        mcp,
        "get_http_request",
        lambda: SimpleNamespace(headers={"authorization": "Bearer test"}),
    )
    monkeypatch.setattr(
        mcp, "get_user_from_token_if_valid", AsyncMock(return_value=test_user)
    )
    monkeypatch.setattr(
        mcp.crud_project,
        "get_by_slug_or_identifier",
        lambda *args, **kwargs: SimpleNamespace(id=uuid.uuid4()),
    )
    client = AsyncMock()
    monkeypatch.setattr(mcp, "get_tracker_client", client)
    with pytest.raises(HTTPException) as refused:
        await mcp.create_issue.__wrapped__(
            project="example/app",
            title="Follow-up",
            description="Evidence",
            status=status,
        )
    assert refused.value.status_code == 403
    assert tracker["fake"].calls == []
    client.assert_not_awaited()
    [audit] = _audit_rows(db_session, test_user.account_id)
    assert audit.status == "denied"
    assert audit.details["denied_by"] == "tracker_writes.close=deny"


@pytest.mark.parametrize("kind", ["open_issue", "reopen"])
@pytest.mark.parametrize("status", [403, 404, 500])
def test_client_setup_failure_allows_retry(
    kind: str,
    status: int,
    client,
    db_session: Session,
    test_user,
    tracker: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _report_run(db_session, test_user.account_id)
    setup = AsyncMock(
        side_effect=HTTPException(status_code=status, detail="setup failed")
    )
    monkeypatch.setattr(actions, "get_tracker_client", setup)
    body = (
        {"kind": "open_issue", "index": 0}
        if kind == "open_issue"
        else {"kind": "reopen", "ref": "example/app#7"}
    )
    key = "follow_up:0" if kind == "open_issue" else "reopen:example/app#7"
    url = f"{BASE}/{run.id}/actions"
    assert client.post(url, json=body).status_code == 502
    db_session.refresh(run)
    assert key not in (run.result.get("proposal_actions") or {})
    assert tracker["fake"].calls == []
    setup.side_effect = None
    setup.return_value = tracker["fake"]
    assert client.post(url, json=body).status_code == 200
