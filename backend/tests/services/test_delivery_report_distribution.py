"""Delivery Report distribution and stored PR details (#2020).

Covers the deterministic report reading PR title, body summary and file
list from the ledger, the webhook and forge writers that fill them, the
recipient rules with per-person opt-out, the email with its opt-out link,
and the opt-out and subscription routes.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_delivery_report_policy,
    crud_organization,
    crud_project,
    crud_tracker,
)
from preloop.services import delivery_report as dr
from preloop.services import delivery_report_email as dre
from preloop.services import delivery_report_facts as facts
from preloop.utils import tokens

REPO = "example-org/example-repo"
PR_URL = f"https://github.com/{REPO}/pull/7"
WINDOW = {"from": "2026-10-17T00:00:00+00:00", "to": "2026-10-24T00:00:00+00:00"}
BODY = """Adds CSV export to the reports page so teams can share a week.

<!-- PRELOOP_BRIEF -->
### What changed
- backend/preloop/api/export.py: new route
### Not verified
- large exports were not timed
<!-- /PRELOOP_BRIEF -->

Closes #3
"""
FILES = [
    {"filename": "backend/preloop/api/export.py", "status": "added", "additions": 40},
    {"filename": "backend/tests/test_export.py", "status": "added", "additions": 30},
    {
        "filename": "backend/preloop/models/alembic/versions/0001_x.py",
        "status": "added",
        "additions": 12,
        "patch": "+def upgrade():\n+    pass\n+def downgrade():\n+    pass\n",
    },
]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestBodySummary:
    def test_first_prose_paragraph_skips_brief_and_closing_lines(self) -> None:
        body = "## Summary\n\n<!-- PRELOOP_BRIEF -->\n- x\n<!-- /PRELOOP_BRIEF -->\n"
        body += "\nCloses #3\n\nThe real description.\n"
        assert facts.body_summary(body) == "The real description."

    def test_long_paragraph_is_cut_at_a_word(self) -> None:
        summary = facts.body_summary("word " * 100, limit=40)
        assert summary is not None
        assert len(summary) <= 40
        assert summary.endswith("…")

    def test_empty_body_has_no_summary(self) -> None:
        assert facts.body_summary(None) is None
        assert facts.body_summary("<!-- only a comment -->\n\nFixes #1") is None


class TestStoredFiles:
    def test_patch_kept_only_on_migrations(self) -> None:
        files = facts.stored_files(
            [
                {"filename": "app.py", "patch": "+x", "additions": 1},
                {"new_path": "db/migrations/0002.py", "diff": "+y", "new_file": True},
            ]
        )
        assert files == [
            {"filename": "app.py", "additions": 1},
            {"filename": "db/migrations/0002.py", "status": "added", "patch": "+y"},
        ]

    def test_details_from_forge_keeps_the_stated_count(self) -> None:
        details = facts.details_from_forge(
            {
                "title": "Export",
                "description": "Body",
                "author": "jane-doe",
                "author_id": "42",
                "changes": {"files_changed": 5, "changed_files": FILES[:1]},
            }
        )
        assert details["files_changed"] == 5
        assert details["author_login"] == "jane-doe"
        assert details["author_id"] == "42"
        assert [f["filename"] for f in details["changed_files"]] == [
            FILES[0]["filename"]
        ]


class TestDetailsFromWebhook:
    @pytest.mark.parametrize(
        ("payload", "login"),
        [
            (
                {"pull_request": {"title": "T", "body": "B", "user": {"login": "a"}}},
                "a",
            ),
            (
                {
                    "object_attributes": {
                        "title": "T",
                        "description": "B",
                        "author": {"username": "b"},
                    },
                    "user": {"username": "merger"},
                },
                "b",
            ),
            (
                {
                    "pullrequest": {
                        "title": "T",
                        "description": "B",
                        "author": {"nickname": "c"},
                    }
                },
                "c",
            ),
        ],
    )
    def test_each_forge(self, payload: Dict[str, Any], login: str) -> None:
        assert facts.details_from_webhook({"payload": payload}) == {
            "title": "T",
            "body": "B",
            "author_login": login,
        }

    def test_gitlab_actor_is_not_taken_for_the_author(self) -> None:
        details = facts.details_from_webhook(
            {
                "payload": {
                    "object_attributes": {"title": "T", "description": "B"},
                    "user": {"username": "merger"},
                }
            }
        )
        assert "author_login" not in details

    def test_unknown_payload_states_nothing(self) -> None:
        assert facts.details_from_webhook({"payload": {"issue": {}}}) == {}


def test_report_carries_body_summary_files_and_file_flags() -> None:
    report = dr.assemble_report(
        window=WINDOW,
        pull_requests=[
            {
                "url": PR_URL,
                "title": "Add CSV export",
                "description": BODY,
                "merged_at": "2026-10-20T10:00:00+00:00",
                "changes": {"changed_files": FILES, "files_changed": 3},
            }
        ],
        issues=[],
    )
    change = report["shipped"][0]
    assert change["body_summary"].startswith("Adds CSV export")
    assert change["files"] == [f["filename"] for f in FILES]
    assert change["files_changed"] == 3
    flags = {f["flag"] for f in change["risk_flags"]}
    assert dr.RISK_MIGRATION_WITHOUT_DOWNGRADE in flags
    assert change["evidence_gaps"] == ["large exports were not timed"]
    assert "Files (3): backend/preloop/api/export.py" in report["report_markdown"]


def test_report_without_details_lists_no_files() -> None:
    report = dr.assemble_report(
        window=WINDOW,
        pull_requests=[{"url": PR_URL, "merged_at": "2026-10-20T10:00:00+00:00"}],
        issues=[],
    )
    change = report["shipped"][0]
    assert change["files"] == []
    assert change["files_changed"] is None
    assert change["body_summary"] is None
    assert change["files_observed"] is False
    assert "File list not read" in report["report_markdown"]


def test_an_observed_empty_file_list_is_not_reported_as_unread() -> None:
    report = dr.assemble_report(
        window=WINDOW,
        pull_requests=[
            {
                "url": PR_URL,
                "merged_at": "2026-10-20T10:00:00+00:00",
                "changes": {"changed_files": []},
            }
        ],
        issues=[],
    )
    assert report["shipped"][0]["files_observed"] is True
    assert "File list not read" not in report["report_markdown"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (PR_URL, (REPO, 7)),
        (
            "https://gitlab.com/group/sub/project/-/merge_requests/7",
            ("group/sub/project", 7),
        ),
        ("https://gitlab.com/group/project/-/merge_requests/9", ("group/project", 9)),
        ("https://example.com/not-a-pr", None),
    ],
)
def test_parse_pr_key(url: str, expected: Any) -> None:
    assert facts.parse_pr_key(url) == expected


# ---------------------------------------------------------------------------
# Ledger writers and readers
# ---------------------------------------------------------------------------


def _project(db: Session, account_id: Any) -> models.Project:
    suffix = uuid.uuid4().hex[:8]
    tracker = crud_tracker.create(
        db,
        obj_in={
            "name": f"GitHub {suffix}",
            "tracker_type": "github",
            "account_id": account_id,
            "api_key": "test_api_key",
            "url": "https://github.com",
            "is_active": True,
        },
    )
    organization = crud_organization.create(
        db,
        obj_in={
            "name": f"Org {suffix}",
            "identifier": f"org-{suffix}",
            "tracker_id": tracker.id,
            "is_active": True,
        },
    )
    return crud_project.create(
        db,
        obj_in={
            "name": f"Project {suffix}",
            "identifier": f"project-{suffix}",
            "slug": REPO,
            "organization_id": organization.id,
        },
    )


def _merged_pr(db: Session, account_id: Any, **values: Any) -> Any:
    pr = models.IssueCostPullRequest(
        account_id=account_id,
        pr_key=PR_URL,
        merged_at=datetime(2026, 10, 20, 10, 0, tzinfo=UTC),
        state="merged",
        **values,
    )
    db.add(pr)
    db.flush()
    return pr


@pytest.fixture
def owner(db_session: Session, test_user: Any) -> Any:
    account = crud_account.get(db_session, id=test_user.account_id)
    account.primary_user_id = test_user.id
    db_session.flush()
    return test_user


def test_deterministic_report_reads_title_body_and_files_from_the_ledger(
    db_session: Session, owner: Any
) -> None:
    _merged_pr(
        db_session,
        owner.account_id,
        title="Add CSV export",
        body=BODY,
        author_login="jane-doe",
        author_id="42",
        changed_files=FILES,
        files_changed=3,
    )
    flow = SimpleNamespace(
        id=uuid.uuid4(),
        account_id=owner.account_id,
        agent_config={"report_kind": "delivery_report"},
    )
    run = dr.PolicyRun(decision="deterministic", policy=dr.DEFAULT_POLICY)
    report = dr.deterministic_report(
        db_session,
        flow=flow,
        execution=SimpleNamespace(id=None),
        run=run,
        window=WINDOW,
    )
    change = report["shipped"][0]
    assert change["title"] == "Add CSV export"
    assert change["author"] == "jane-doe"
    assert change["author_id"] == "42"
    assert change["forge"] == "github"
    assert change["body_summary"].startswith("Adds CSV export")
    assert len(change["files"]) == 3
    assert dr.RISK_MIGRATION_WITHOUT_DOWNGRADE in {
        f["flag"] for f in change["risk_flags"]
    }


def test_merge_webhook_stores_title_and_body(db_session: Session, owner: Any) -> None:
    from preloop.services.issue_cost_rollup import record_pull_request_event

    pull = record_pull_request_event(
        db_session,
        {
            "type": "pull_request_merged",
            "source": "github",
            "account_id": str(owner.account_id),
            "payload": {
                "action": "closed",
                "pull_request": {
                    "html_url": PR_URL,
                    "number": 7,
                    "title": "Add CSV export",
                    "body": BODY,
                    "merged": True,
                    "merged_at": "2026-10-20T10:00:00Z",
                    "user": {"login": "jane-doe"},
                },
                "repository": {"full_name": REPO},
            },
        },
    )
    assert pull is not None
    assert pull.title == "Add CSV export"
    assert pull.body == BODY
    assert pull.author_login == "jane-doe"
    assert pull.details_observed_at is None


def test_forge_refresh_fills_files_once(db_session: Session, owner: Any) -> None:
    _project(db_session, owner.account_id)
    pr = _merged_pr(db_session, owner.account_id)
    calls: List[str] = []

    async def fake_read(db: Any, *, row: Any, owner: Any) -> Dict[str, Any]:
        calls.append(row.pr_key)
        return {
            "title": "Add CSV export",
            "description": BODY,
            "changes": {"changed_files": FILES, "files_changed": 3},
        }

    end = datetime(2026, 10, 24, tzinfo=UTC)
    start = datetime(2026, 10, 17, tzinfo=UTC)
    with (
        patch.object(facts, "_read_forge", fake_read),
        patch.object(db_session, "commit", db_session.flush),
    ):
        filled = asyncio.run(
            facts.refresh_pull_request_details(
                db_session, account_id=owner.account_id, start=start, end=end
            )
        )
        again = asyncio.run(
            facts.refresh_pull_request_details(
                db_session, account_id=owner.account_id, start=start, end=end
            )
        )
    assert (filled, again) == (1, 0)
    assert calls == [PR_URL]
    assert pr.files_changed == 3
    assert pr.changed_files[2]["patch"].startswith("+def upgrade")
    assert "patch" not in pr.changed_files[0]


def test_forge_refresh_failure_is_tried_once(db_session: Session, owner: Any) -> None:
    pr = _merged_pr(db_session, owner.account_id)

    async def failing(db: Any, *, row: Any, owner: Any) -> Dict[str, Any]:
        raise RuntimeError("forge down")

    with (
        patch.object(facts, "_read_forge", failing),
        patch.object(db_session, "commit", db_session.flush),
    ):
        filled = asyncio.run(
            facts.refresh_pull_request_details(
                db_session,
                account_id=owner.account_id,
                start=None,
                end=datetime(2026, 10, 24, tzinfo=UTC),
            )
        )
    assert filled == 0
    assert pr.changed_files is None
    assert pr.details_observed_at is not None


def test_project_for_repository_matches_the_slug(
    db_session: Session, owner: Any
) -> None:
    project = _project(db_session, owner.account_id)
    found = crud_delivery_report_policy.project_for_repository(
        db_session, account_id=owner.account_id, repository=REPO.upper()
    )
    assert found is not None and found[0].id == project.id
    assert (
        crud_delivery_report_policy.project_for_repository(
            db_session, account_id=uuid.uuid4(), repository=REPO
        )
        is None
    )


# ---------------------------------------------------------------------------
# Recipients and opt-out
# ---------------------------------------------------------------------------


def _member(db: Session, account_id: Any, *, active: bool = True) -> models.User:
    suffix = uuid.uuid4().hex[:8]
    user = models.User(
        account_id=account_id,
        username=f"lead-{suffix}",
        email=f"lead-{suffix}@example.com",
        full_name="Jane Doe",
        is_active=active,
        hashed_password="x",
    )
    db.add(user)
    db.flush()
    return user


class TestRecipients:
    def _recipients(self, db: Session, owner: Any, **policy: Any) -> List[Any]:
        resolved = dr.DeliveryReportPolicy(**policy)
        return crud_delivery_report_policy.report_recipients(
            db,
            account_id=owner.account_id,
            recipient_rule=resolved.recipients,
            sponsoring_lead_user_id=resolved.sponsoring_lead_user_id,
            opted_out_user_ids=resolved.opted_out_user_ids,
        )

    def test_defaults_to_the_account_owner(
        self, db_session: Session, owner: Any
    ) -> None:
        assert [u.id for u in self._recipients(db_session, owner)] == [owner.id]

    def test_an_opted_out_owner_gets_nothing(
        self, db_session: Session, owner: Any
    ) -> None:
        assert (
            self._recipients(db_session, owner, opted_out_user_ids=[str(owner.id)])
            == []
        )

    def test_sponsoring_lead(self, db_session: Session, owner: Any) -> None:
        lead = _member(db_session, owner.account_id)
        got = self._recipients(
            db_session,
            owner,
            recipients="sponsoring_lead",
            sponsoring_lead_user_id=str(lead.id),
        )
        assert [u.id for u in got] == [lead.id]

    def test_inactive_lead_falls_back_to_the_owner(
        self, db_session: Session, owner: Any
    ) -> None:
        lead = _member(db_session, owner.account_id, active=False)
        got = self._recipients(
            db_session,
            owner,
            recipients="sponsoring_lead",
            sponsoring_lead_user_id=str(lead.id),
        )
        assert [u.id for u in got] == [owner.id]

    def test_lead_from_another_account_is_ignored(
        self, db_session: Session, owner: Any
    ) -> None:
        other = crud_account.create(
            db_session, obj_in={"organization_name": "Other", "is_active": True}
        )
        lead = _member(db_session, other.id)
        got = self._recipients(
            db_session,
            owner,
            recipients="sponsoring_lead",
            sponsoring_lead_user_id=str(lead.id),
        )
        assert [u.id for u in got] == [owner.id]


def test_opt_out_token_round_trip_and_type() -> None:
    user_id, account_id = uuid.uuid4(), uuid.uuid4()
    token = tokens.create_report_opt_out_token(
        user_id=user_id, account_id=account_id, email="jane@example.com"
    )
    claims = tokens.verify_report_opt_out_token(token)
    assert (claims.user_id, claims.account_id) == (user_id, account_id)
    reset = tokens.create_password_reset_token("jane@example.com", user_id=user_id)
    with pytest.raises(tokens.TokenError):
        tokens.verify_report_opt_out_token(reset)


# ---------------------------------------------------------------------------
# The email
# ---------------------------------------------------------------------------


def _report() -> Dict[str, Any]:
    return dr.assemble_report(
        window=WINDOW,
        pull_requests=[
            {
                "url": PR_URL,
                "title": "Add <b>CSV</b> export",
                "description": BODY,
                "merged_at": "2026-10-20T10:00:00+00:00",
                "changes": {"changed_files": FILES},
            }
        ],
        issues=[],
        assessment="skipped_no_credit",
    )


class TestDistribute:
    def _send(self, db: Session, owner: Any, report: Any, **policy: Any) -> Any:
        sent: List[Dict[str, Any]] = []

        def fake_send(to: str, subject: str, text: str, body: str, **kw: Any) -> bool:
            sent.append(
                {"to": to, "subject": subject, "text": text, "html": body, **kw}
            )
            return True

        with patch("preloop.utils.email.send_email", fake_send):
            record = asyncio.run(
                dre.distribute_report(
                    db,
                    flow=SimpleNamespace(account_id=owner.account_id),
                    execution=SimpleNamespace(id="exec-1"),
                    report=report,
                    policy=dr.DeliveryReportPolicy(**policy),
                )
            )
        return record, sent

    def test_owner_gets_the_report_with_links(
        self, db_session: Session, owner: Any
    ) -> None:
        record, sent = self._send(db_session, owner, _report())
        assert record["status"] == "sent"
        assert record["sent_to"] == [str(owner.id)]
        [mail] = sent
        assert mail["to"] == owner.email
        assert mail["subject"].startswith("Delivery report 2026-10-17 to 2026-10-24")
        assert "/console/delivery-reports/exec-1" in mail["text"]
        assert "Adds CSV export" in mail["text"]
        assert "Files (3)" in mail["text"]
        assert "no credit left" in mail["text"]
        assert "<b>CSV</b>" not in mail["html"]
        unsubscribe = mail["headers"]["List-Unsubscribe"].strip("<>")
        assert unsubscribe in mail["text"]
        assert mail["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        token = parse_qs(urlparse(unsubscribe).query)["token"][0]
        claims = tokens.verify_report_opt_out_token(token)
        assert claims.user_id == owner.id

    def test_opted_out_owner_is_not_emailed(
        self, db_session: Session, owner: Any
    ) -> None:
        crud_delivery_report_policy.set_user_opt_out(
            db_session, account_id=owner.account_id, user_id=owner.id, opted_out=True
        )
        record, sent = self._send(db_session, owner, _report())
        assert sent == []
        assert record["status"] == "no_recipients"

    def test_opt_out_during_the_run_is_honoured(
        self, db_session: Session, owner: Any
    ) -> None:
        # The run started with a snapshot that still lists the owner; the
        # opt-out stored since then wins.
        crud_delivery_report_policy.set_user_opt_out(
            db_session, account_id=owner.account_id, user_id=owner.id, opted_out=True
        )
        record, sent = self._send(db_session, owner, _report(), opted_out_user_ids=[])
        assert (record["status"], sent) == ("no_recipients", [])

    def test_turned_off_during_the_run_is_not_sent(
        self, db_session: Session, owner: Any
    ) -> None:
        crud_delivery_report_policy.save_overrides(
            db_session, account_id=owner.account_id, overrides={"enabled": False}
        )
        record, sent = self._send(db_session, owner, _report(), enabled=True)
        assert (record["status"], sent) == ("disabled", [])

    def test_sent_once(self, db_session: Session, owner: Any) -> None:
        report = {**_report(), dre.DISTRIBUTION_KEY: {"status": "sent"}}
        record, sent = self._send(db_session, owner, report)
        assert (record, sent) == (None, [])

    def test_failed_or_disabled_reports_are_not_sent(
        self, db_session: Session, owner: Any
    ) -> None:
        for report in ({"status": "error"}, {"status": "disabled"}):
            record, sent = self._send(db_session, owner, report)
            assert (record, sent) == (None, [])

    def test_unconfigured_smtp_is_not_reported_as_sent(
        self, db_session: Session, owner: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("preloop.utils.email.SMTP_USERNAME", "")
        monkeypatch.setattr("preloop.utils.email.SMTP_PASSWORD", "")
        record = asyncio.run(
            dre.distribute_report(
                db_session,
                flow=SimpleNamespace(account_id=owner.account_id),
                execution=SimpleNamespace(id="exec-1"),
                report=_report(),
                policy=None,
            )
        )
        assert record["status"] == "not_configured"
        assert record["sent_to"] == []

    def test_partial_delivery_preserves_each_recipient_outcome(
        self, db_session: Session, owner: Any
    ) -> None:
        from preloop.utils.email import EmailError

        second = SimpleNamespace(id=uuid.uuid4(), email="second@example.com")
        with (
            patch.object(
                crud_delivery_report_policy,
                "report_recipients",
                return_value=[owner, second],
            ),
            patch(
                "preloop.utils.email.send_email",
                side_effect=[True, EmailError("smtp down")],
            ),
        ):
            record = asyncio.run(
                dre.distribute_report(
                    db_session,
                    flow=SimpleNamespace(account_id=owner.account_id),
                    execution=SimpleNamespace(id="exec-1"),
                    report=_report(),
                    policy=None,
                )
            )
        assert record["status"] == "failed"
        assert record["sent_to"] == [str(owner.id)]
        assert record["failed"] == [str(second.id)]

    def test_a_send_failure_is_recorded(self, db_session: Session, owner: Any) -> None:
        from preloop.utils.email import EmailError

        def failing(*args: Any, **kwargs: Any) -> None:
            raise EmailError("smtp down")

        with patch("preloop.utils.email.send_email", failing):
            record = asyncio.run(
                dre.distribute_report(
                    db_session,
                    flow=SimpleNamespace(account_id=owner.account_id),
                    execution=SimpleNamespace(id="exec-1"),
                    report=_report(),
                    policy=None,
                )
            )
        assert record["status"] == "failed"
        assert record["failed"] == [str(owner.id)]


def test_orchestrator_stores_the_distribution_record() -> None:
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.db = object()
    orchestrator.execution_log = SimpleNamespace(id="exec-1")
    orchestrator.flow = SimpleNamespace(id="flow-1", account_id="acct-1")
    with patch(
        "preloop.services.delivery_report_email.distribute_report",
        new=AsyncMock(return_value={"status": "sent", "sent_to": ["u"]}),
    ) as distribute:
        result = asyncio.run(
            orchestrator._distribute_delivery_report({"status": "success"}, None)
        )
    assert result[dre.DISTRIBUTION_KEY]["status"] == "sent"
    assert distribute.call_args.kwargs["policy"] is None


def test_orchestrator_uses_plugin_distributor_instead_of_core_email() -> None:
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.db = object()
    orchestrator.execution_log = SimpleNamespace(id="exec-1")
    orchestrator.flow = SimpleNamespace(id="flow-1", account_id="acct-1")
    extension = AsyncMock(return_value={"status": "sent", "sent_to": ["u"]})
    with (
        patch("preloop.plugins.get_plugin_manager") as manager,
        patch(
            "preloop.services.delivery_report_email.distribute_report", new=AsyncMock()
        ) as core,
    ):
        manager.return_value.get_service.return_value = extension
        result = asyncio.run(
            orchestrator._distribute_delivery_report({"status": "success"}, None)
        )
    assert result[dre.DISTRIBUTION_KEY]["sent_to"] == ["u"]
    extension.assert_awaited_once()
    core.assert_not_awaited()


def test_forge_engagement_keeps_only_ids_and_in_window_events() -> None:
    pr = {
        "_forge": "github",
        "reviews": [
            {"user_id": "202", "submitted_at": "2026-10-18T10:00:00Z"},
            {"user_id": "202", "submitted_at": "2026-10-19T10:00:00Z"},
            {"user_id": "999", "submitted_at": "2026-10-01T10:00:00Z"},
        ],
        "comments": [
            {
                "author": "Display Name",
                "author_id": "303",
                "created_at": "2026-10-20T10:00:00Z",
            },
            {"author": "Name Only", "created_at": "2026-10-20T10:00:00Z"},
        ],
    }
    rows = facts.engagement_from_forge(
        pr,
        ref="example-org/example-repo#7",
        area="billing",
        start=datetime.fromisoformat(WINDOW["from"]),
        end=datetime.fromisoformat(WINDOW["to"]),
    )
    assert rows == [
        {
            "user_id": "202",
            "forge": "github",
            "kind": "review",
            "ref": "example-org/example-repo#7",
            "area": "billing",
        },
        {
            "user_id": "303",
            "forge": "github",
            "kind": "comment",
            "ref": "example-org/example-repo#7",
            "area": "billing",
        },
    ]


def test_orchestrator_sends_only_a_finalized_report() -> None:
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    orchestrator = object.__new__(FlowExecutionOrchestrator)
    with patch(
        "preloop.services.delivery_report_email.distribute_report", new=AsyncMock()
    ) as distribute:
        original = {"status": "success"}
        result = asyncio.run(orchestrator._send_finalized_delivery_report(original))
    assert result is original
    distribute.assert_not_called()


def test_finalized_report_passes_forge_engagement_to_distribution() -> None:
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.db = object()
    orchestrator.flow = SimpleNamespace(account_id="acct-1")
    orchestrator._delivery_report_finalized = True
    engagement = [
        {
            "user_id": "42",
            "forge": "github",
            "kind": "review",
            "ref": "acme/app#1",
            "area": "billing",
        }
    ]
    with (
        patch(
            "preloop.services.delivery_report_facts.collect_forge_engagement",
            new=AsyncMock(return_value=engagement),
        ) as read,
        patch.object(
            orchestrator,
            "_distribute_delivery_report",
            new=AsyncMock(side_effect=lambda report, run: report),
        ) as distribute,
    ):
        result = asyncio.run(
            orchestrator._send_finalized_delivery_report({"status": "success"})
        )
    assert result["engagement"] == engagement
    read.assert_awaited_once()
    assert distribute.await_args.args[0]["engagement"] == engagement


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

OPT_OUT = "/api/v1/delivery-report/opt-out"
SUBSCRIPTION = "/api/v1/delivery-report/subscription"


def _token(owner: Any) -> str:
    return tokens.create_report_opt_out_token(
        user_id=owner.id, account_id=owner.account_id, email=owner.email
    )


def _opted_out(db: Session, owner: Any) -> List[str]:
    stored = crud_delivery_report_policy.get_overrides(db, account_id=owner.account_id)
    return list((stored or {}).get("opted_out_user_ids") or [])


def test_opt_out_link_asks_before_acting(
    client: TestClient, db_session: Session, owner: Any
) -> None:
    response = client.get(OPT_OUT, params={"token": _token(owner)})
    assert response.status_code == 200
    assert 'method="post"' in response.text
    assert _opted_out(db_session, owner) == []


def test_opt_out_post_opts_out(
    client: TestClient, db_session: Session, owner: Any
) -> None:
    response = client.post(
        OPT_OUT,
        params={"token": _token(owner)},
        data={"List-Unsubscribe": "One-Click"},
    )
    assert response.status_code == 200
    assert _opted_out(db_session, owner) == [str(owner.id)]
    # Following the link twice keeps one entry.
    client.post(OPT_OUT, params={"token": _token(owner)})
    assert _opted_out(db_session, owner) == [str(owner.id)]


def test_opt_out_with_a_bad_token_changes_nothing(
    client: TestClient, db_session: Session, owner: Any
) -> None:
    response = client.post(OPT_OUT, params={"token": "not-a-token"})
    assert response.status_code == 400
    assert _opted_out(db_session, owner) == []


def test_opt_out_token_for_a_user_outside_the_account_is_refused(
    client: TestClient, db_session: Session, owner: Any
) -> None:
    token = tokens.create_report_opt_out_token(
        user_id=owner.id, account_id=uuid.uuid4(), email=owner.email
    )
    assert client.post(OPT_OUT, params={"token": token}).status_code == 400


@pytest.fixture
def anonymous_client(app: Any) -> Any:
    """A client with no signed-in user: the opt-out link must work without one."""
    from preloop.api.auth import get_current_active_user

    app.dependency_overrides.pop(get_current_active_user, None)
    with TestClient(app) as anonymous:
        yield anonymous


def test_opt_out_needs_no_session(
    anonymous_client: TestClient, db_session: Session, owner: Any
) -> None:
    assert anonymous_client.get(SUBSCRIPTION).status_code == 401
    page = anonymous_client.get(OPT_OUT, params={"token": _token(owner)})
    assert page.status_code == 200
    response = anonymous_client.post(
        OPT_OUT,
        params={"token": _token(owner)},
        data={"List-Unsubscribe": "One-Click"},
    )
    assert response.status_code == 200
    assert _opted_out(db_session, owner) == [str(owner.id)]


def test_subscription_round_trip(
    client: TestClient, db_session: Session, owner: Any
) -> None:
    body = client.get(SUBSCRIPTION).json()
    assert body == {"opted_out": False, "is_recipient": True, "report_enabled": True}
    body = client.put(SUBSCRIPTION, json={"opted_out": True}).json()
    assert body["opted_out"] is True
    assert _opted_out(db_session, owner) == [str(owner.id)]
    body = client.put(SUBSCRIPTION, json={"opted_out": False}).json()
    assert body["opted_out"] is False
    assert _opted_out(db_session, owner) == []


def test_owner_sees_the_toggle_when_the_named_lead_is_inactive(
    client: TestClient, db_session: Session, owner: Any
) -> None:
    lead = _member(db_session, owner.account_id, active=False)
    crud_delivery_report_policy.save_overrides(
        db_session,
        account_id=owner.account_id,
        overrides={
            "recipients": "sponsoring_lead",
            "sponsoring_lead_user_id": str(lead.id),
        },
    )
    assert client.get(SUBSCRIPTION).json()["is_recipient"] is True


def test_opted_out_recipient_still_sees_the_toggle(
    client: TestClient, db_session: Session, owner: Any
) -> None:
    client.put(SUBSCRIPTION, json={"opted_out": True})
    body = client.get(SUBSCRIPTION).json()
    assert (body["opted_out"], body["is_recipient"]) == (True, True)


def test_account_policy_save_keeps_personal_opt_outs(
    client: TestClient, db_session: Session, owner: Any
) -> None:
    client.put(SUBSCRIPTION, json={"opted_out": True})
    response = client.put(
        "/api/v1/delivery-report/policy",
        json={"overrides": {"pr_ready_days": 3, "opted_out_user_ids": []}},
    )
    assert response.status_code == 200
    assert _opted_out(db_session, owner) == [str(owner.id)]
    assert response.json()["effective"]["pr_ready_days"] == 3


@pytest.mark.parametrize("enabled", [True, False])
def test_subgroup_report_scope_and_project_override(
    enabled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.models.crud import crud_delivery_report_policy, crud_flow_execution

    project = "group/sub/project"
    pr = {
        "url": f"https://gitlab.com/{project}/-/merge_requests/7",
        "number": 7,
        "title": "Change",
        "state": "merged",
        "merged_at": "2026-10-23T12:00:00Z",
        "changes": {"changed_files": []},
    }
    monkeypatch.setattr(
        crud_delivery_report_policy, "pull_request_facts", lambda *args, **kwargs: [pr]
    )
    monkeypatch.setattr(
        crud_delivery_report_policy, "issue_facts", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        crud_flow_execution, "latest_with_result", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(dr, "load_merge_audits", lambda *args, **kwargs: {"audits": []})
    report = dr.deterministic_report(
        None,
        flow=SimpleNamespace(id=uuid.uuid4(), account_id=uuid.uuid4()),
        execution=SimpleNamespace(id=None),
        window=WINDOW,
        run=dr.PolicyRun(
            decision="deterministic",
            policy=dr.DeliveryReportPolicy(projects=[project]),
            project_policies={project: dr.DeliveryReportPolicy(enabled=enabled)},
        ),
    )
    assert [change["ref"] for change in report["shipped"]] == (
        [f"{project}#7"] if enabled else []
    )


@pytest.mark.parametrize(
    ("verdict", "label"),
    [
        (dr.PURPOSE_PARTIAL, "Closed, partly covered"),
        (dr.PURPOSE_UNVERIFIED, "Closed with an evidence gap"),
    ],
)
def test_email_distinguishes_shown_misses_from_evidence_gaps(
    verdict: str, label: str
) -> None:
    report = _report()
    report["needs_you"] = [
        {
            "kind": "closed_unverified",
            "verdict": verdict,
            "ref": "example/app#12",
            "title": "CSV export",
            "unmet_criteria": ["Exports respect the date filter"],
        }
    ]
    _, text, body = dre.render_email(
        report, link="https://example.com/report", opt_out="https://example.com/opt-out"
    )
    for rendered in (text, body):
        assert f"{label}: example/app#12 CSV export" in rendered
        if verdict == dr.PURPOSE_PARTIAL:
            assert "Not shown met: Exports respect the date filter" in rendered
            assert "Closed with an evidence gap" not in rendered
        else:
            assert "Closed, partly covered" not in rendered
