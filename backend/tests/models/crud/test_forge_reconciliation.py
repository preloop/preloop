"""Bounded pull request record refresh and forge delivery dedupe (#2017)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.api.endpoints import webhooks
from preloop.models import models
from preloop.models.crud import (
    crud_forge_webhook_delivery,
    crud_issue_cost,
    crud_pull_request_refresh,
)
from preloop.services import pull_request_refresh as refresh
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.sync.exceptions import TrackerRateLimitError, TrackerResponseError
from preloop.sync.trackers.utils import ConditionalResponse

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
HEAD = "b" * 40


def github_key(number: int, repo: str = "widgets") -> str:
    return f"https://github.com/acme/{repo}/pull/{number}"


def make_pr(
    db: Session, account_id: uuid.UUID, pr_key: str, **fields: Any
) -> models.IssueCostPullRequest:
    row = crud_issue_cost.get_or_create_pull_request(
        db, account_id=account_id, pr_key=pr_key
    )
    for name, value in {"state": "open", **fields}.items():
        setattr(row, name, value)
    db.commit()
    return row


class FakeForge:
    """A forge client that answers ``conditional_get`` from a table."""

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers = answers or {}
        self.reads: list[tuple[str, str | None]] = []

    async def conditional_get(self, path: str, etag: str | None) -> Any:
        self.reads.append((path, etag))
        answer = self.answers.get(path)
        if isinstance(answer, Exception):
            raise answer
        if answer == 304:
            return ConditionalResponse(304, None, etag)
        body = answer or {"state": "open", "head": {"sha": HEAD}, "merged": False}
        return ConditionalResponse(200, body, f'"{path}"')


@pytest.fixture
def forge(monkeypatch: pytest.MonkeyPatch) -> FakeForge:
    client = FakeForge()
    tracker = SimpleNamespace(
        id=uuid.uuid4(),
        tracker_type="github",
        is_active=True,
        connection_details={},
        url=None,
    )
    monkeypatch.setattr(
        refresh.crud_tracker, "get_for_account", lambda db, **kwargs: [tracker]
    )
    monkeypatch.setattr(
        refresh, "feedback_tracker_client", AsyncMock(return_value=client)
    )
    return client


def due_count(db: Session, account_id: uuid.UUID, now: datetime) -> int:
    rows = db.query(models.IssueCostPullRequest).filter_by(account_id=account_id)
    return sum(
        1 for row in rows if row.refresh_due_at is None or row.refresh_due_at <= now
    )


@pytest.mark.asyncio
async def test_fifty_open_pull_requests_stay_within_the_bound(
    db_session: Session, test_user: Any, forge: FakeForge
) -> None:
    account = test_user.account_id
    for number in range(50):
        make_pr(db_session, account, github_key(number))

    first = await refresh.refresh_pull_requests(db_session, now=NOW)
    second = await refresh.refresh_pull_requests(
        db_session, now=NOW + timedelta(minutes=1)
    )
    third = await refresh.refresh_pull_requests(
        db_session, now=NOW + timedelta(minutes=2)
    )

    assert [first["read"], second["read"], third["read"]] == [25, 25, 0]
    assert first["claimed"] == second["claimed"] == refresh.REFRESH_BATCH
    # Every pull request was read exactly once in the re-arm window.
    assert len({path for path, _ in forge.reads}) == 50 == len(forge.reads)

    # Five minutes after its read a record is due again, with its validator.
    fourth = await refresh.refresh_pull_requests(
        db_session, now=NOW + timedelta(minutes=5)
    )
    assert fourth["read"] == 25
    assert all(etag for _, etag in forge.reads[50:])


@pytest.mark.asyncio
async def test_not_modified_changes_only_the_bookkeeping(
    db_session: Session, test_user: Any, forge: FakeForge
) -> None:
    row = make_pr(
        db_session,
        test_user.account_id,
        github_key(1),
        head_sha="c" * 40,
        refresh_etag='"v1"',
    )
    forge.answers["/repos/acme/widgets/pulls/1"] = 304

    counts = await refresh.refresh_pull_requests(db_session, now=NOW)

    db_session.refresh(row)
    assert counts["not_modified"] == 1
    assert forge.reads == [("/repos/acme/widgets/pulls/1", '"v1"')]
    assert (row.state, row.head_sha, row.refresh_etag) == ("open", "c" * 40, '"v1"')
    assert row.refreshed_at == NOW
    assert row.refresh_due_at == NOW + timedelta(minutes=5)


@pytest.mark.asyncio
async def test_merge_is_recorded_and_never_regressed_by_a_later_read(
    db_session: Session, test_user: Any, forge: FakeForge
) -> None:
    row = make_pr(db_session, test_user.account_id, github_key(2))
    path = "/repos/acme/widgets/pulls/2"
    forge.answers[path] = {
        "state": "closed",
        "merged": True,
        "merged_at": "2026-10-25T11:00:00Z",
        "head": {"sha": HEAD},
    }

    await refresh.refresh_pull_requests(db_session, now=NOW)
    db_session.refresh(row)
    assert (row.state, row.head_sha) == ("merged", HEAD)
    assert row.merged_at == datetime(2026, 10, 25, 11, 0, tzinfo=UTC)
    # A merged record is not scheduled again by the cadence.
    assert row.refresh_due_at is None
    assert (await refresh.refresh_pull_requests(db_session, now=NOW))["read"] == 0

    # An out-of-order webhook makes it due; a stale read cannot unmerge it.
    assert crud_pull_request_refresh.request(
        db_session, account_id=test_user.account_id, pr_key=github_key(2), now=NOW
    )
    forge.answers[path] = {"state": "open", "merged": False, "head": {"sha": "d" * 40}}
    await refresh.refresh_pull_requests(db_session, now=NOW + timedelta(seconds=1))
    db_session.refresh(row)
    assert (row.state, row.head_sha) == ("merged", HEAD)
    assert row.merged_at == datetime(2026, 10, 25, 11, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_a_closed_pull_request_reopened_is_seen_after_its_webhook(
    db_session: Session, test_user: Any, forge: FakeForge
) -> None:
    row = make_pr(db_session, test_user.account_id, github_key(3), state="closed")
    assert (await refresh.refresh_pull_requests(db_session, now=NOW))["read"] == 0

    event = {
        "account_id": str(test_user.account_id),
        "type": "pull_request_reopened",
        "payload": {"pull_request": {"html_url": github_key(3)}},
    }
    assert refresh.accelerate(db_session, event)
    counts = await refresh.refresh_pull_requests(
        db_session, now=datetime.now(UTC) + timedelta(seconds=1)
    )

    db_session.refresh(row)
    assert counts["updated"] == 1
    assert row.state == "open"
    assert row.refresh_due_at is not None


@pytest.mark.asyncio
async def test_webhook_for_an_unknown_pull_request_creates_nothing(
    db_session: Session, test_user: Any
) -> None:
    event = {
        "account_id": str(test_user.account_id),
        "type": "pull_request_updated",
        "payload": {"pull_request": {"html_url": github_key(99)}},
    }
    assert refresh.accelerate(db_session, event) is False
    assert (
        crud_issue_cost.get_pull_request(
            db_session, account_id=test_user.account_id, pr_key=github_key(99)
        )
        is None
    )


@pytest.mark.asyncio
async def test_throttling_defers_the_forge_and_keeps_other_forges_reading(
    db_session: Session, test_user: Any, forge: FakeForge, monkeypatch: Any
) -> None:
    account = test_user.account_id
    for number in range(3):
        make_pr(db_session, account, github_key(number))
    gitlab_key = "https://gitlab.com/acme/app/-/merge_requests/5"
    gitlab_row = make_pr(db_session, account, gitlab_key)
    gitlab = FakeForge(
        {"/projects/acme%2Fapp/merge_requests/5": {"state": "merged", "sha": HEAD}}
    )
    trackers = [
        SimpleNamespace(
            id=uuid.uuid4(),
            tracker_type="github",
            is_active=True,
            connection_details={},
            url=None,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            tracker_type="gitlab",
            is_active=True,
            connection_details={},
            url="https://gitlab.com",
        ),
    ]
    monkeypatch.setattr(
        refresh.crud_tracker, "get_for_account", lambda db, **kwargs: trackers
    )
    monkeypatch.setattr(
        refresh,
        "feedback_tracker_client",
        AsyncMock(
            side_effect=lambda db, tracker: forge if tracker is trackers[0] else gitlab
        ),
    )
    for number in range(3):
        forge.answers[f"/repos/acme/widgets/pulls/{number}"] = TrackerRateLimitError(
            "slow", retry_after=600
        )

    counts = await refresh.refresh_pull_requests(db_session, now=NOW)

    # One throttled GitHub read; the other GitHub rows are not even tried.
    assert len(forge.reads) == 1
    assert counts["deferred"] == 3
    rows = (
        db_session.query(models.IssueCostPullRequest)
        .filter(models.IssueCostPullRequest.pr_key.like("https://github.com/%"))
        .all()
    )
    assert {row.refresh_due_at for row in rows} == {NOW + timedelta(seconds=600)}
    db_session.refresh(gitlab_row)
    assert gitlab_row.state == "merged"


@pytest.mark.asyncio
async def test_an_unreadable_pull_request_backs_off_for_a_day(
    db_session: Session, test_user: Any, forge: FakeForge
) -> None:
    row = make_pr(db_session, test_user.account_id, github_key(4))
    forge.answers["/repos/acme/widgets/pulls/4"] = TrackerResponseError(
        "gone", status_code=404
    )

    counts = await refresh.refresh_pull_requests(db_session, now=NOW)

    db_session.refresh(row)
    assert counts["unreadable"] == 1
    assert row.refresh_due_at == NOW + timedelta(days=1)


@pytest.mark.asyncio
async def test_old_unobserved_and_merged_records_are_not_refreshed(
    db_session: Session, test_user: Any, forge: FakeForge
) -> None:
    account = test_user.account_id
    make_pr(db_session, account, github_key(5), state=None, merged_at=NOW)
    old = make_pr(db_session, account, github_key(6), state=None)
    old.created_at = (NOW - timedelta(days=60)).replace(tzinfo=None)
    db_session.commit()
    make_pr(db_session, account, github_key(7), state=None)

    counts = await refresh.refresh_pull_requests(db_session, now=NOW)

    assert counts["read"] == 1
    assert forge.reads[0][0] == "/repos/acme/widgets/pulls/7"


# --- delivery dedupe --------------------------------------------------------


def test_a_delivery_id_is_claimed_once_per_tracker(
    db_session: Session, test_user: Any
) -> None:
    tracker = models.Tracker(
        name="forge",
        tracker_type="github",
        account_id=test_user.account_id,
        connection_details={},
    )
    db_session.add(tracker)
    db_session.commit()
    claim = dict(tracker_id=tracker.id, delivery_id="delivery-1", now=NOW)

    assert crud_forge_webhook_delivery.claim(db_session, **claim) is True
    assert crud_forge_webhook_delivery.claim(db_session, **claim) is False
    crud_forge_webhook_delivery.release(
        db_session, tracker_id=tracker.id, delivery_id="delivery-1"
    )
    assert crud_forge_webhook_delivery.claim(db_session, **claim) is True

    assert (
        crud_forge_webhook_delivery.prune(
            db_session, before=NOW + timedelta(seconds=1), limit=10
        )
        == 1
    )
    assert crud_forge_webhook_delivery.claim(db_session, **claim) is True


@pytest.mark.asyncio
async def test_ingress_queues_a_forge_delivery_once_and_releases_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker_id = str(uuid.uuid4())
    claimed: set[str] = set()
    order: list[str] = []

    def claim(db: Any, *, tracker_id: Any, delivery_id: str, now: Any) -> bool:
        order.append(f"claim:{delivery_id}")
        if delivery_id in claimed:
            return False
        claimed.add(delivery_id)
        return True

    def release(db: Any, *, tracker_id: Any, delivery_id: str) -> None:
        order.append(f"release:{delivery_id}")
        claimed.discard(delivery_id)

    monkeypatch.setattr(
        webhooks,
        "crud_forge_webhook_delivery",
        SimpleNamespace(
            claim=claim,
            release=release,
            mark_published=lambda db, **kwargs: order.append(
                f"published:{kwargs['delivery_id']}"
            ),
        ),
    )

    def prepared(
        tracker_type: str,
        organization_id: str,
        raw_body: bytes,
        headers: Any,
        db: Any,
        plan: Any,
    ) -> dict[str, Any]:
        plan.tracker_id = tracker_id
        plan.organization_id = organization_id
        plan.queue_task(
            "process_webhook_event",
            tracker_type=tracker_type,
            delivery_id=headers.get("X-GitHub-Delivery"),
        )
        return {"status": "success", "tracker_id": tracker_id}

    monkeypatch.setattr(webhooks, "_prepare_webhook", prepared)

    async def inline(fn: Any) -> Any:
        return fn()

    monkeypatch.setattr(webhooks, "run_db_off_loop", inline)
    monkeypatch.setattr(webhooks, "_safe_close_db_session", lambda db: None)
    acks: list[Any] = [None, object(), object()]
    published: list[str] = []

    async def publish(name: str, **kwargs: Any) -> Any:
        published.append(kwargs["delivery_id"])
        order.append(f"publish:{kwargs['delivery_id']}")
        return acks.pop(0)

    def request(delivery: str) -> Any:
        return MagicMock(
            body=AsyncMock(return_value=b"{}"),
            headers={"X-GitHub-Delivery": delivery, "X-GitHub-Event": "ping"},
        )

    bus = MagicMock(publish_task=publish)
    # First attempt: NATS does not acknowledge, so the claim is released.
    with pytest.raises(HTTPException) as error:
        await webhooks.receive_webhook(
            "github", "org", request("d-1"), MagicMock(), bus
        )
    assert error.value.status_code == 503
    # The forge's redelivery is queued, and the next one is a duplicate.
    await webhooks.receive_webhook("github", "org", request("d-1"), MagicMock(), bus)
    duplicate = await webhooks.receive_webhook(
        "github", "org", request("d-1"), MagicMock(), bus
    )
    assert duplicate["status"] == "duplicate"
    # A different delivery of the same event type is its own message.
    await webhooks.receive_webhook("github", "org", request("d-2"), MagicMock(), bus)

    assert published == ["d-1", "d-1", "d-2"]
    assert order[:7] == [
        "claim:d-1",
        "publish:d-1",
        "release:d-1",
        "claim:d-1",
        "publish:d-1",
        "published:d-1",
        "claim:d-1",
    ]


# --- review follow-ups -------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "event_type", "payload", "pr_key"),
    [
        (
            "github",
            "pull_request_reopened",
            {"pull_request": {"html_url": "https://github.com/acme/widgets/pull/8"}},
            "https://github.com/acme/widgets/pull/8",
        ),
        (
            "gitlab",
            "merge_request_reopened",
            {
                "object_attributes": {
                    "url": "https://gitlab.com/acme/app/-/merge_requests/8"
                }
            },
            "https://gitlab.com/acme/app/-/merge_requests/8",
        ),
    ],
)
async def test_reopen_event_through_the_trigger_service_makes_a_closed_record_due(
    db_session: Session,
    test_user: Any,
    source: str,
    event_type: str,
    payload: dict[str, Any],
    pr_key: str,
) -> None:
    row = make_pr(db_session, test_user.account_id, pr_key, state="closed")
    assert row.refresh_due_at is None

    await FlowTriggerService(db_session).process_event(
        {
            "source": source,
            "type": event_type,
            "account_id": str(test_user.account_id),
            "tracker_id": str(uuid.uuid4()),
            "payload": payload,
        }
    )

    db_session.refresh(row)
    assert row.refresh_due_at is not None
    assert row.refresh_due_at <= datetime.now(UTC)


@pytest.mark.asyncio
async def test_a_webhook_mark_during_the_read_survives_the_record(
    db_session: Session, test_user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = make_pr(db_session, test_user.account_id, github_key(9), state="closed")
    crud_pull_request_refresh.request(
        db_session, account_id=test_user.account_id, pr_key=github_key(9), now=NOW
    )
    marked = NOW + timedelta(seconds=30)

    class MarkingForge(FakeForge):
        async def conditional_get(self, path: str, etag: str | None) -> Any:
            # The reopen webhook lands while the stale read is in flight.
            crud_pull_request_refresh.request(
                db_session,
                account_id=test_user.account_id,
                pr_key=github_key(9),
                now=marked,
            )
            return ConditionalResponse(
                200, {"state": "closed", "merged": False, "head": {"sha": HEAD}}, None
            )

    tracker = SimpleNamespace(
        id=uuid.uuid4(),
        tracker_type="github",
        is_active=True,
        connection_details={},
        url=None,
    )
    monkeypatch.setattr(
        refresh.crud_tracker, "get_for_account", lambda db, **kwargs: [tracker]
    )
    monkeypatch.setattr(
        refresh, "feedback_tracker_client", AsyncMock(return_value=MarkingForge())
    )

    await refresh.refresh_pull_requests(db_session, now=NOW + timedelta(seconds=1))

    db_session.refresh(row)
    assert row.state == "closed"
    # The webhook's mark is kept, so the next tick reads the reopen.
    assert row.refresh_due_at == marked


@pytest.mark.asyncio
async def test_an_unchanged_bitbucket_short_head_keeps_the_full_hash(
    db_session: Session, test_user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    full = "e" * 40
    key = "https://bitbucket.org/ws/repo/pull-requests/4"
    row = make_pr(db_session, test_user.account_id, key)
    client = FakeForge(
        {
            "/repositories/ws/repo/pullrequests/4": {
                "state": "OPEN",
                "source": {"commit": {"hash": full[:12]}},
            }
        }
    )
    resolved: list[str] = []

    async def canonical(client: Any, repository: str, sha: str) -> str:
        resolved.append(sha)
        return full

    tracker = SimpleNamespace(
        id=uuid.uuid4(),
        tracker_type="bitbucket",
        is_active=True,
        connection_details={},
        url=None,
    )
    monkeypatch.setattr(
        refresh.crud_tracker, "get_for_account", lambda db, **kwargs: [tracker]
    )
    monkeypatch.setattr(
        refresh, "feedback_tracker_client", AsyncMock(return_value=client)
    )
    monkeypatch.setattr(refresh, "canonical_bitbucket_head", canonical)

    await refresh.refresh_pull_requests(db_session, now=NOW)
    db_session.refresh(row)
    assert row.head_sha == full
    second = await refresh.refresh_pull_requests(
        db_session, now=NOW + timedelta(minutes=5)
    )

    db_session.refresh(row)
    assert second["updated"] == 1
    assert row.head_sha == full
    # The unchanged head cost no second commit lookup.
    assert resolved == [full[:12]]


def test_an_unpublished_claim_is_taken_over_after_the_grace_period(
    db_session: Session, test_user: Any
) -> None:
    tracker = models.Tracker(
        name="forge",
        tracker_type="github",
        account_id=test_user.account_id,
        connection_details={},
    )
    db_session.add(tracker)
    db_session.commit()
    key = dict(tracker_id=tracker.id, delivery_id="delivery-2")

    assert crud_forge_webhook_delivery.claim(db_session, now=NOW, **key)
    # A concurrent duplicate inside the grace period is still a duplicate.
    assert not crud_forge_webhook_delivery.claim(
        db_session, now=NOW + timedelta(seconds=30), **key
    )
    # The first holder died before publishing: the forge retry is queued.
    assert crud_forge_webhook_delivery.claim(
        db_session, now=NOW + timedelta(minutes=3), **key
    )
    crud_forge_webhook_delivery.mark_published(
        db_session, now=NOW + timedelta(minutes=3), **key
    )
    # Once published, a redelivery is a duplicate however late it comes.
    assert not crud_forge_webhook_delivery.claim(
        db_session, now=NOW + timedelta(days=1), **key
    )
