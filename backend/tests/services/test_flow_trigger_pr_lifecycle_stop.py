"""Executions stop when their pull request is merged, closed or superseded.

Covers #1032 part A against a real database: ``process_event`` with a
normalized ``pull_request_merged``/``_closed`` (GitHub, GitLab, Bitbucket)
stops every run still bound to that request through the shared stop path,
and a ``pull_request_updated`` with a new head stops the older head's run
when the flow sets ``webhook_config.supersede_on_update``.
"""

import uuid
from contextlib import contextmanager
from typing import Iterable, Optional
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.sync.event_normalizer import (
    PR_STOP_SOURCE_CLOSED,
    PR_STOP_SOURCE_MERGED,
    PR_STOP_SOURCE_SUPERSEDED,
)

pytestmark = pytest.mark.asyncio

REPO = "example-org/widgets"
OLD_SHA = "a" * 40
NEW_SHA = "b" * 40


def github_pr_payload(
    *, number: int = 12, action: str = "opened", sha: str = OLD_SHA, **pr
) -> dict:
    return {
        "action": action,
        "pull_request": {"number": number, "head": {"sha": sha}, **pr},
        "repository": {"full_name": REPO},
    }


def gitlab_mr_payload(*, iid: int = 7, action: str = "open", sha: str = OLD_SHA):
    return {
        "object_kind": "merge_request",
        "object_attributes": {
            "iid": iid,
            "action": action,
            "last_commit": {"id": sha},
        },
        "project": {"path_with_namespace": "example-group/widgets"},
    }


def bitbucket_pr_payload(*, pr_id: int = 5, sha: str = OLD_SHA) -> dict:
    return {
        "pullrequest": {"id": pr_id, "source": {"commit": {"hash": sha}}},
        "repository": {"full_name": "example-ws/widgets"},
    }


def event(source: str, event_type: str, payload: dict, account_id) -> dict:
    return {
        "source": source,
        "type": event_type,
        "account_id": str(account_id),
        "delivery_id": str(uuid.uuid4()),
        "payload": payload,
    }


def make_flow(
    db: Session,
    account_id,
    *,
    name: str = "Pull Request Reviewer",
    types: Iterable[str] = ("pull_request_opened",),
    webhook_config: Optional[dict] = None,
) -> models.Flow:
    row = models.Flow(
        name=name,
        prompt_template="review {{trigger_event.payload}}",
        agent_type="codex",
        agent_config={},
        account_id=account_id,
        trigger_event_source=None,
        trigger_event_types=list(types),
        webhook_config=webhook_config,
        is_enabled=True,
    )
    db.add(row)
    db.flush()
    return row


def make_execution(
    db: Session,
    flow: models.Flow,
    *,
    source: str,
    event_type: str,
    payload: dict,
    status: str = "RUNNING",
) -> models.FlowExecution:
    row = models.FlowExecution(
        flow_id=flow.id,
        status=status,
        trigger_event_details={
            "source": source,
            "type": event_type,
            "payload": payload,
        },
    )
    db.add(row)
    db.flush()
    return row


def executions_for(db: Session, flow: models.Flow) -> list[models.FlowExecution]:
    db.expire_all()
    return (
        db.query(models.FlowExecution)
        .filter(models.FlowExecution.flow_id == flow.id)
        .order_by(models.FlowExecution.start_time.asc())
        .all()
    )


@contextmanager
def stubbed_runtime():
    """No NATS, no in-process orchestrator; capture stop commands."""
    send_command = AsyncMock()
    with (
        patch(
            "preloop.services.flow_trigger_service.get_nats_client",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "preloop.services.flow_orchestrator.FlowExecutionOrchestrator.send_command",
            new=send_command,
        ),
        patch(
            "preloop.services.flow_execution_dispatcher.flow_execution_worker_enabled",
            return_value=True,
        ),
        patch(
            "preloop.services.flow_execution_dispatcher.dispatch_execute",
            new_callable=AsyncMock,
        ),
    ):
        yield send_command


async def deliver(db: Session, evt: dict, *, flows: list[models.Flow]) -> AsyncMock:
    """``process_event`` with the matched flows fixed, as the worker runs it."""
    with (
        stubbed_runtime() as send_command,
        patch(
            "preloop.services.flow_trigger_service.crud_flow.get_by_trigger",
            return_value=flows,
        ),
    ):
        await FlowTriggerService(db).process_event(dict(evt))
    return send_command


# --------------------------------------------------------------------------
# Merged and closed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("merged", "event_type", "stop_source", "outcome"),
    [
        (True, "pull_request_merged", PR_STOP_SOURCE_MERGED, "was merged"),
        (
            False,
            "pull_request_closed",
            PR_STOP_SOURCE_CLOSED,
            "was closed without merging",
        ),
    ],
)
async def test_github_close_stops_every_run_bound_to_the_pr(
    db_session: Session, test_user, merged, event_type, stop_source, outcome
) -> None:
    account = test_user.account_id
    reviewer = make_flow(db_session, account)
    other = make_flow(db_session, account, name="Docs checker")
    running = make_execution(
        db_session,
        reviewer,
        source="github",
        event_type="pull_request_opened",
        payload=github_pr_payload(),
    )
    queued = make_execution(
        db_session,
        other,
        source="github",
        event_type="pull_request_opened",
        payload=github_pr_payload(),
        status="PENDING",
    )
    # A comment on the PR resumed a run: it is working on the PR too.
    resumed = make_execution(
        db_session,
        other,
        source="github",
        event_type="comment_created",
        payload={
            "action": "created",
            "issue": {"number": 12, "pull_request": {"url": "https://example.com"}},
            "comment": {"body": "please look again"},
            "repository": {"full_name": REPO},
        },
        status="WAITING_FOR_HUMAN",
    )
    other_pr = make_execution(
        db_session,
        reviewer,
        source="github",
        event_type="pull_request_opened",
        payload=github_pr_payload(number=13),
    )
    issue_run = make_execution(
        db_session,
        other,
        source="github",
        event_type="issue_labeled",
        payload={"issue": {"number": 12}, "repository": {"full_name": REPO}},
    )
    finished = make_execution(
        db_session,
        reviewer,
        source="github",
        event_type="pull_request_opened",
        payload=github_pr_payload(),
        status="SUCCEEDED",
    )
    db_session.commit()

    send_command = await deliver(
        db_session,
        event(
            "github",
            event_type,
            github_pr_payload(action="closed", merged=merged),
            account,
        ),
        flows=[],
    )

    reason = f"Stopped because pull request {REPO}#12 {outcome}"
    for row in (running, queued, resumed):
        db_session.refresh(row)
        assert row.status == "STOPPED"
        assert row.stop_source == stop_source
        assert row.stop_reason == reason
        assert row.error_message == reason
        assert row.end_time is not None
        # Durable like every stop; stop_source says it was not a kill
        # switch.
        assert row.stop_requested_at is not None
    for row, status in ((other_pr, "RUNNING"), (issue_run, "RUNNING")):
        db_session.refresh(row)
        assert row.status == status
        assert row.stop_source is None
    db_session.refresh(finished)
    assert finished.status == "SUCCEEDED"
    assert finished.stop_reason is None

    stopped_ids = {call.kwargs["execution_id"] for call in send_command.await_args_list}
    assert stopped_ids == {str(running.id), str(queued.id), str(resumed.id)}

    audit = (
        db_session.query(models.Event)
        .filter(models.Event.event_type == "flow_execution_stopped_for_pull_request")
        .all()
    )
    assert {row.event_data["execution_id"] for row in audit} == stopped_ids
    assert {row.event_data["stop_source"] for row in audit} == {stop_source}


async def test_merge_stops_a_run_parked_on_its_children_with_the_source(
    db_session: Session, test_user
) -> None:
    """The park close writes the terminal row; it carries the cause too."""
    from datetime import datetime, timedelta, timezone

    from preloop.models.crud import crud_flow_execution

    account = test_user.account_id
    reviewer = make_flow(db_session, account)
    workers = make_flow(db_session, account, name="Shard worker")
    parent = make_execution(
        db_session,
        reviewer,
        source="github",
        event_type="pull_request_opened",
        payload=github_pr_payload(),
    )
    child = models.FlowExecution(
        flow_id=workers.id,
        status="RUNNING",
        parent_execution_id=parent.id,
        root_execution_id=parent.id,
        delegation_depth=1,
        trigger_event_details={
            "source": "flow_delegation",
            "payload": {},
            "delegation": {"parent_execution_id": str(parent.id), "depth": 1},
        },
    )
    db_session.add(child)
    db_session.flush()
    crud_flow_execution.request_park(
        db_session,
        execution_id=parent.id,
        approval_request_id=uuid.uuid4(),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        kind="children",
    )
    crud_flow_execution.confirm_park(
        db_session, execution_id=parent.id, compute_seconds=60, kind="children"
    )
    db_session.commit()
    db_session.refresh(parent)
    assert parent.status == "WAITING_FOR_CHILDREN"

    await deliver(
        db_session,
        event(
            "github",
            "pull_request_merged",
            github_pr_payload(action="closed", merged=True),
            account,
        ),
        flows=[],
    )

    db_session.expire_all()
    db_session.refresh(parent)
    assert parent.status == "STOPPED"
    assert parent.stop_source == PR_STOP_SOURCE_MERGED
    assert parent.stop_reason == f"Stopped because pull request {REPO}#12 was merged"
    assert parent.park_expires_at is None
    db_session.refresh(child)
    assert child.status == "STOPPED"


@pytest.mark.parametrize(
    ("action", "event_type", "stop_source"),
    [
        ("merge", "merge_request_merged", PR_STOP_SOURCE_MERGED),
        ("close", "merge_request_closed", PR_STOP_SOURCE_CLOSED),
    ],
)
async def test_gitlab_merge_request_end_stops_its_runs(
    db_session: Session, test_user, action, event_type, stop_source
) -> None:
    reviewer = make_flow(db_session, test_user.account_id)
    run = make_execution(
        db_session,
        reviewer,
        source="gitlab",
        event_type="merge_request_opened",
        payload=gitlab_mr_payload(),
    )
    note_run = make_execution(
        db_session,
        reviewer,
        source="gitlab",
        event_type="comment_created",
        payload={
            "object_kind": "note",
            "object_attributes": {"id": 991, "note": "again please"},
            "merge_request": {"iid": 7},
            "project": {"path_with_namespace": "example-group/widgets"},
        },
    )
    other_mr = make_execution(
        db_session,
        reviewer,
        source="gitlab",
        event_type="merge_request_opened",
        payload=gitlab_mr_payload(iid=8),
    )
    db_session.commit()

    await deliver(
        db_session,
        event(
            "gitlab",
            event_type,
            gitlab_mr_payload(action=action),
            test_user.account_id,
        ),
        flows=[],
    )

    for row in (run, note_run):
        db_session.refresh(row)
        assert row.status == "STOPPED"
        assert row.stop_source == stop_source
        assert "merge request example-group/widgets!7" in row.stop_reason
    db_session.refresh(other_mr)
    assert other_mr.status == "RUNNING"


@pytest.mark.parametrize(
    ("event_type", "stop_source"),
    [
        ("pull_request_merged", PR_STOP_SOURCE_MERGED),
        ("pull_request_closed", PR_STOP_SOURCE_CLOSED),
    ],
)
async def test_bitbucket_fulfilled_or_declined_stops_its_runs(
    db_session: Session, test_user, event_type, stop_source
) -> None:
    reviewer = make_flow(db_session, test_user.account_id)
    run = make_execution(
        db_session,
        reviewer,
        source="bitbucket",
        event_type="pull_request_opened",
        payload=bitbucket_pr_payload(),
    )
    other_pr = make_execution(
        db_session,
        reviewer,
        source="bitbucket",
        event_type="pull_request_opened",
        payload=bitbucket_pr_payload(pr_id=6),
    )
    db_session.commit()

    await deliver(
        db_session,
        event("bitbucket", event_type, bitbucket_pr_payload(), test_user.account_id),
        flows=[],
    )

    db_session.refresh(run)
    assert run.status == "STOPPED"
    assert run.stop_source == stop_source
    assert "pull request example-ws/widgets#5" in run.stop_reason
    db_session.refresh(other_pr)
    assert other_pr.status == "RUNNING"


async def test_close_with_nothing_bound_is_a_no_op(
    db_session: Session, test_user
) -> None:
    reviewer = make_flow(db_session, test_user.account_id)
    elsewhere = make_execution(
        db_session,
        reviewer,
        source="github",
        event_type="pull_request_opened",
        payload=github_pr_payload(number=99),
    )
    db_session.commit()

    evt = event(
        "github",
        "pull_request_merged",
        github_pr_payload(action="closed", merged=True),
        test_user.account_id,
    )
    with stubbed_runtime() as send_command:
        stopped = await FlowTriggerService(
            db_session
        ).stop_executions_for_ended_pull_request(evt)
    assert stopped == []
    send_command.assert_not_awaited()
    db_session.refresh(elsewhere)
    assert elsewhere.status == "RUNNING"
    assert (
        db_session.query(models.Event)
        .filter(models.Event.event_type == "flow_execution_stopped_for_pull_request")
        .count()
        == 0
    )


async def test_close_leaves_other_accounts_alone(
    db_session: Session, test_user
) -> None:
    stranger = crud_account.create(
        db_session, obj_in={"organization_name": "Other org", "is_active": True}
    )
    their_flow = make_flow(db_session, stranger.id)
    theirs = make_execution(
        db_session,
        their_flow,
        source="github",
        event_type="pull_request_opened",
        payload=github_pr_payload(),
    )
    db_session.commit()

    await deliver(
        db_session,
        event(
            "github",
            "pull_request_merged",
            github_pr_payload(action="closed", merged=True),
            test_user.account_id,
        ),
        flows=[],
    )

    db_session.refresh(theirs)
    assert theirs.status == "RUNNING"


async def test_flow_that_filters_on_merge_is_not_stopped_and_still_starts(
    db_session: Session, test_user
) -> None:
    account = test_user.account_id
    reviewer = make_flow(db_session, account)
    release_notes = make_flow(
        db_session,
        account,
        name="Release notes",
        types=("pull_request_opened", "pull_request_merged"),
    )
    review_run = make_execution(
        db_session,
        reviewer,
        source="github",
        event_type="pull_request_opened",
        payload=github_pr_payload(),
    )
    notes_run = make_execution(
        db_session,
        release_notes,
        source="github",
        event_type="pull_request_opened",
        payload=github_pr_payload(),
    )
    db_session.commit()

    await deliver(
        db_session,
        event(
            "github",
            "pull_request_merged",
            github_pr_payload(action="closed", merged=True),
            account,
        ),
        flows=[release_notes],
    )

    db_session.refresh(review_run)
    assert review_run.status == "STOPPED"
    db_session.refresh(notes_run)
    assert notes_run.status == "RUNNING"
    assert notes_run.stop_source is None


async def test_merge_starts_only_flows_that_subscribe_to_it(
    db_session: Session, test_user
) -> None:
    """The merge event itself only starts flows that filter on it."""
    account = test_user.account_id
    reviewer = make_flow(db_session, account)
    release_notes = make_flow(
        db_session, account, name="Release notes", types=("pull_request_merged",)
    )
    db_session.commit()

    with stubbed_runtime():
        await FlowTriggerService(db_session).process_event(
            event(
                "github",
                "pull_request_merged",
                github_pr_payload(action="closed", merged=True),
                account,
            )
        )

    assert executions_for(db_session, reviewer) == []
    started = executions_for(db_session, release_notes)
    assert len(started) == 1
    assert started[0].trigger_event_details["type"] == "pull_request_merged"


async def test_a_second_close_delivery_changes_nothing(
    db_session: Session, test_user
) -> None:
    reviewer = make_flow(db_session, test_user.account_id)
    run = make_execution(
        db_session,
        reviewer,
        source="github",
        event_type="pull_request_opened",
        payload=github_pr_payload(),
    )
    db_session.commit()
    evt = event(
        "github",
        "pull_request_closed",
        github_pr_payload(action="closed", merged=False),
        test_user.account_id,
    )

    await deliver(db_session, evt, flows=[])
    db_session.refresh(run)
    first_end = run.end_time
    second = await deliver(db_session, {**evt, "delivery_id": "again"}, flows=[])

    db_session.refresh(run)
    assert run.status == "STOPPED"
    assert run.end_time == first_end
    second.assert_not_awaited()


# --------------------------------------------------------------------------
# Superseded by a new head
# --------------------------------------------------------------------------

SUPERSEDING = {"supersede_on_update": True}
UPDATE_TYPES = ("pull_request_opened", "pull_request_updated")


async def test_new_head_stops_the_older_run_before_the_new_one_starts(
    db_session: Session, test_user
) -> None:
    reviewer = make_flow(
        db_session, test_user.account_id, types=UPDATE_TYPES, webhook_config=SUPERSEDING
    )
    old = make_execution(
        db_session,
        reviewer,
        source="github",
        event_type="pull_request_opened",
        payload=github_pr_payload(),
    )
    db_session.commit()

    send_command = await deliver(
        db_session,
        event(
            "github",
            "pull_request_updated",
            github_pr_payload(action="synchronize", sha=NEW_SHA),
            test_user.account_id,
        ),
        flows=[reviewer],
    )

    rows = executions_for(db_session, reviewer)
    assert [row.id for row in rows][0] == old.id
    db_session.refresh(old)
    assert old.status == "STOPPED"
    assert old.stop_source == PR_STOP_SOURCE_SUPERSEDED
    assert old.stop_reason == (
        f"Stopped because pull request {REPO}#12 got a new head bbbbbbbb; "
        "this run on aaaaaaaa was superseded"
    )
    new = [row for row in rows if row.id != old.id]
    assert len(new) == 1
    assert new[0].trigger_event_details["payload"]["pull_request"]["head"]["sha"] == (
        NEW_SHA
    )
    assert new[0].status != "STOPPED"
    assert [call.kwargs["execution_id"] for call in send_command.await_args_list] == [
        str(old.id)
    ]


async def test_new_gitlab_head_supersedes_too(db_session: Session, test_user) -> None:
    reviewer = make_flow(
        db_session,
        test_user.account_id,
        types=("merge_request_opened", "merge_request_updated"),
        webhook_config=SUPERSEDING,
    )
    old = make_execution(
        db_session,
        reviewer,
        source="gitlab",
        event_type="merge_request_opened",
        payload=gitlab_mr_payload(),
    )
    db_session.commit()

    await deliver(
        db_session,
        event(
            "gitlab",
            "merge_request_updated",
            gitlab_mr_payload(action="update", sha=NEW_SHA),
            test_user.account_id,
        ),
        flows=[reviewer],
    )

    db_session.refresh(old)
    assert old.status == "STOPPED"
    assert old.stop_source == PR_STOP_SOURCE_SUPERSEDED
    assert len(executions_for(db_session, reviewer)) == 2


async def test_new_bitbucket_head_supersedes_too(
    db_session: Session, test_user
) -> None:
    reviewer = make_flow(
        db_session, test_user.account_id, types=UPDATE_TYPES, webhook_config=SUPERSEDING
    )
    old = make_execution(
        db_session,
        reviewer,
        source="bitbucket",
        event_type="pull_request_opened",
        payload=bitbucket_pr_payload(),
    )
    db_session.commit()

    await deliver(
        db_session,
        event(
            "bitbucket",
            "pull_request_updated",
            bitbucket_pr_payload(sha=NEW_SHA),
            test_user.account_id,
        ),
        flows=[reviewer],
    )

    db_session.refresh(old)
    assert old.status == "STOPPED"
    assert old.stop_source == PR_STOP_SOURCE_SUPERSEDED
    assert len(executions_for(db_session, reviewer)) == 2


async def test_without_the_flag_the_older_run_is_left_alone(
    db_session: Session, test_user
) -> None:
    """Today's behaviour: the older head keeps running.

    The one-active-run-per-object guard then coalesces the new head into
    it rather than starting a second run, as it did before #1032.
    """
    reviewer = make_flow(db_session, test_user.account_id, types=UPDATE_TYPES)
    old = make_execution(
        db_session,
        reviewer,
        source="github",
        event_type="pull_request_opened",
        payload=github_pr_payload(),
    )
    db_session.commit()

    send_command = await deliver(
        db_session,
        event(
            "github",
            "pull_request_updated",
            github_pr_payload(action="synchronize", sha=NEW_SHA),
            test_user.account_id,
        ),
        flows=[reviewer],
    )

    db_session.refresh(old)
    assert old.status == "RUNNING"
    assert old.stop_source is None
    send_command.assert_not_awaited()
    assert [row.id for row in executions_for(db_session, reviewer)] == [old.id]


async def test_same_head_is_not_superseded(db_session: Session, test_user) -> None:
    """An ``edited`` PR (title, body) keeps its head: nothing is stopped."""
    reviewer = make_flow(
        db_session, test_user.account_id, types=UPDATE_TYPES, webhook_config=SUPERSEDING
    )
    old = make_execution(
        db_session,
        reviewer,
        source="github",
        event_type="pull_request_opened",
        payload=github_pr_payload(),
    )
    db_session.commit()

    await deliver(
        db_session,
        event(
            "github",
            "pull_request_updated",
            github_pr_payload(action="edited", sha=OLD_SHA),
            test_user.account_id,
        ),
        flows=[reviewer],
    )

    db_session.refresh(old)
    assert old.status == "RUNNING"
    assert [row.id for row in executions_for(db_session, reviewer)] == [old.id]


async def test_supersede_only_touches_the_same_flow_and_pr(
    db_session: Session, test_user
) -> None:
    account = test_user.account_id
    reviewer = make_flow(
        db_session, account, types=UPDATE_TYPES, webhook_config=SUPERSEDING
    )
    other_flow = make_flow(db_session, account, name="Docs checker", types=UPDATE_TYPES)
    other_flow_run = make_execution(
        db_session,
        other_flow,
        source="github",
        event_type="pull_request_opened",
        payload=github_pr_payload(),
    )
    other_pr_run = make_execution(
        db_session,
        reviewer,
        source="github",
        event_type="pull_request_opened",
        payload=github_pr_payload(number=13),
    )
    comment_run = make_execution(
        db_session,
        reviewer,
        source="github",
        event_type="comment_created",
        payload={
            "action": "created",
            "pull_request": {"number": 12},
            "comment": {"body": "fixed"},
            "repository": {"full_name": REPO},
        },
    )
    db_session.commit()

    await deliver(
        db_session,
        event(
            "github",
            "pull_request_updated",
            github_pr_payload(action="synchronize", sha=NEW_SHA),
            account,
        ),
        flows=[reviewer],
    )

    for row in (other_flow_run, other_pr_run, comment_run):
        db_session.refresh(row)
        assert row.status == "RUNNING"


async def test_a_failing_stop_never_blocks_triggering(
    db_session: Session, test_user
) -> None:
    account = test_user.account_id
    reviewer = make_flow(db_session, account)
    release_notes = make_flow(
        db_session, account, name="Release notes", types=("pull_request_merged",)
    )
    make_execution(
        db_session,
        reviewer,
        source="github",
        event_type="pull_request_opened",
        payload=github_pr_payload(),
    )
    db_session.commit()

    with patch(
        "preloop.services.flow_execution_stop.stop_execution",
        new=AsyncMock(side_effect=RuntimeError("runtime unreachable")),
    ):
        await deliver(
            db_session,
            event(
                "github",
                "pull_request_merged",
                github_pr_payload(action="closed", merged=True),
                account,
            ),
            flows=[release_notes],
        )

    assert len(executions_for(db_session, release_notes)) == 1
