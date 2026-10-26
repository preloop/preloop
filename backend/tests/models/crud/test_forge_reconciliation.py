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
@pytest.mark.parametrize("provider", ["github", "gitlab", "bitbucket"])
async def test_fifty_prs_bound_includes_fallback_and_commit_reads(
    db_session: Session,
    test_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    paths = {
        "github": "https://github.com/example/repo/pull/",
        "gitlab": "https://gitlab.com/example/repo/-/merge_requests/",
        "bitbucket": "https://bitbucket.org/example/repo/pull-requests/",
    }
    for number in range(50):
        make_pr(db_session, test_user.account_id, paths[provider] + str(number))
    trackers = [
        SimpleNamespace(
            id=uuid.uuid4(),
            tracker_type=provider,
            is_active=True,
            connection_details={},
            url=None,
        )
        for _ in range(2)
    ]
    reads: list[str] = []

    async def denied(path: str, etag: str | None) -> Any:
        reads.append(path)
        raise TrackerResponseError("Not visible to this tracker", status_code=404)

    async def read(path: str, etag: str | None) -> ConditionalResponse:
        reads.append(path)
        return ConditionalResponse(
            200,
            {
                "state": "OPEN" if provider == "bitbucket" else "open",
                "head": {"sha": HEAD},
                "sha": HEAD,
                "source": {"commit": {"hash": HEAD[:12]}},
            },
            None,
        )

    async def canonical(client: Any, repository: str, sha: str) -> str:
        reads.append(repository + "/commit/" + sha)
        return HEAD

    clients = {
        trackers[0].id: SimpleNamespace(conditional_get=denied),
        trackers[1].id: SimpleNamespace(conditional_get=read),
    }
    monkeypatch.setattr(
        refresh.crud_tracker, "get_for_account", lambda *a, **k: trackers
    )
    monkeypatch.setattr(
        refresh,
        "feedback_tracker_client",
        AsyncMock(side_effect=lambda db, tracker: clients[tracker.id]),
    )
    monkeypatch.setattr(refresh, "canonical_bitbucket_head", canonical)
    # Drain the initial population before any successfully read row re-arms.
    for minute in range(5):
        before = len(reads)
        counts = await refresh.refresh_pull_requests(
            db_session, now=NOW + timedelta(minutes=minute)
        )
        assert counts["read"] == len(reads) - before <= refresh.REFRESH_BATCH
    rows = (
        db_session.query(models.IssueCostPullRequest)
        .filter_by(account_id=test_user.account_id)
        .all()
    )
    # Bitbucket needs three reads per row initially; it can take more ticks.
    assert sum(row.refreshed_at is not None for row in rows) >= (
        40 if provider == "bitbucket" else 50
    )
    assert all(row.head_sha == HEAD for row in rows if row.refreshed_at is not None)


def test_plain_200_discards_an_obsolete_validator(
    db_session: Session, test_user: Any
) -> None:
    row = make_pr(db_session, test_user.account_id, github_key(1), refresh_etag='"old"')
    crud_pull_request_refresh.record(
        db_session, pull_request_id=row.id, now=NOW, etag=None, state="open"
    )
    db_session.refresh(row)
    assert row.refresh_etag is None


@pytest.mark.asyncio
async def test_bitbucket_commit_throttle_defers_remaining_claims(
    db_session: Session, test_user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        make_pr(
            db_session,
            test_user.account_id,
            f"https://bitbucket.org/example/repo/pull-requests/{number}",
        )
        for number in range(3)
    ]
    forge = FakeForge(
        {
            f"/repositories/example/repo/pullrequests/{number}": {
                "state": "OPEN",
                "source": {"commit": {"hash": HEAD[:12]}},
            }
            for number in range(3)
        }
    )
    tracker = SimpleNamespace(
        id=uuid.uuid4(),
        tracker_type="bitbucket",
        is_active=True,
        connection_details={},
        url=None,
    )
    monkeypatch.setattr(
        refresh.crud_tracker, "get_for_account", lambda *a, **k: [tracker]
    )
    monkeypatch.setattr(
        refresh, "feedback_tracker_client", AsyncMock(return_value=forge)
    )
    canonical = AsyncMock(side_effect=TrackerRateLimitError("wait", retry_after=900))
    monkeypatch.setattr(refresh, "canonical_bitbucket_head", canonical)
    counts = await refresh.refresh_pull_requests(db_session, now=NOW, limit=2)
    assert counts["read"] == 2
    assert counts["deferred"] == 2
    assert len(forge.reads) == canonical.await_count == 1
    for row in rows:
        db_session.refresh(row)
    claimed = [row for row in rows if row.refresh_due_at is not None]
    assert len(claimed) == 2
    assert all(row.refresh_due_at == NOW + timedelta(minutes=15) for row in claimed)
    assert all(row.refreshed_at is row.refresh_etag is None for row in claimed)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["OPEN", "MERGED", "DECLINED"])
@pytest.mark.parametrize(
    "error",
    [TrackerResponseError("unavailable", status_code=503), ValueError("bad hash")],
    ids=["provider-error", "invalid-head"],
)
async def test_failed_bitbucket_head_lookup_retries_before_caching_validator(
    db_session: Session,
    test_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    error: Exception,
) -> None:
    row = make_pr(
        db_session,
        test_user.account_id,
        "https://bitbucket.org/example/repo/pull-requests/1",
        head_sha="c" * 40,
    )
    body = {
        "state": state,
        "updated_on": NOW.isoformat(),
        "source": {"commit": {"hash": HEAD[:12]}},
    }

    async def read(path: str, etag: str | None) -> ConditionalResponse:
        # Caching this validator after a failed canonical lookup would hide
        # the changed head forever, or stop scheduling a terminal PR entirely.
        return (
            ConditionalResponse(304, None, etag)
            if etag == '"new"'
            else ConditionalResponse(200, body, '"new"')
        )

    client = SimpleNamespace(conditional_get=AsyncMock(side_effect=read))
    tracker = SimpleNamespace(
        id=uuid.uuid4(),
        tracker_type="bitbucket",
        is_active=True,
        connection_details={},
        url=None,
    )
    monkeypatch.setattr(
        refresh.crud_tracker, "get_for_account", lambda *a, **k: [tracker]
    )
    monkeypatch.setattr(
        refresh, "feedback_tracker_client", AsyncMock(return_value=client)
    )
    canonical = AsyncMock(side_effect=[error, HEAD])
    monkeypatch.setattr(refresh, "canonical_bitbucket_head", canonical)
    first = await refresh.refresh_pull_requests(db_session, now=NOW)
    db_session.refresh(row)
    assert first["read"] == 2
    assert first["unreadable"] == 1
    assert row.refresh_etag is row.refreshed_at is None
    assert row.refresh_due_at == NOW + timedelta(minutes=5)
    assert row.head_sha == "c" * 40
    assert row.state == "open"

    second = await refresh.refresh_pull_requests(
        db_session, now=NOW + timedelta(minutes=5)
    )
    db_session.refresh(row)
    assert second["read"] == 2
    assert second["updated"] == 1
    assert client.conditional_get.await_args.args[1] is None
    assert canonical.await_count == 2
    assert row.head_sha == HEAD
    assert row.refresh_etag == '"new"'
    assert (
        row.state == {"OPEN": "open", "MERGED": "merged", "DECLINED": "closed"}[state]
    )
    assert row.merged_at == (NOW if state == "MERGED" else None)


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
        db_session, tracker_id=tracker.id, delivery_id="delivery-1", claimed_at=NOW
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
@pytest.mark.parametrize(
    ("provider", "delivery_header"),
    [
        ("github", "X-GitHub-Delivery"),
        ("gitlab", "X-Gitlab-Event-UUID"),
        ("bitbucket", "X-Request-UUID"),
    ],
)
async def test_ingress_queues_a_forge_delivery_once_and_releases_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    delivery_header: str,
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

    def release(
        db: Any, *, tracker_id: Any, delivery_id: str, claimed_at: datetime
    ) -> None:
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
            delivery_id=headers.get(delivery_header),
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
            headers={delivery_header: delivery},
        )

    bus = MagicMock(publish_task=publish)
    # First attempt: NATS does not acknowledge, so the claim is released.
    with pytest.raises(HTTPException) as error:
        await webhooks.receive_webhook(
            provider, "org", request("d-1"), MagicMock(), bus
        )
    assert error.value.status_code == 503
    # The forge's redelivery is queued, and the next one is a duplicate.
    await webhooks.receive_webhook(provider, "org", request("d-1"), MagicMock(), bus)
    duplicate = await webhooks.receive_webhook(
        provider, "org", request("d-1"), MagicMock(), bus
    )
    assert duplicate["status"] == "duplicate"
    # A different delivery of the same event type is its own message.
    await webhooks.receive_webhook(provider, "org", request("d-2"), MagicMock(), bus)

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
        db_session,
        now=NOW + timedelta(minutes=3),
        claimed_at=NOW + timedelta(minutes=3),
        **key,
    )
    # Once published, a redelivery is a duplicate however late it comes.
    assert not crud_forge_webhook_delivery.claim(
        db_session, now=NOW + timedelta(days=1), **key
    )


@pytest.mark.parametrize("late_action", ["release", "mark_published"])
def test_late_publisher_cannot_mutate_a_taken_over_claim(
    db_session: Session, test_user: Any, late_action: str
) -> None:
    tracker = models.Tracker(
        name="forge",
        tracker_type="github",
        account_id=test_user.account_id,
        connection_details={},
    )
    db_session.add(tracker)
    db_session.commit()
    key = dict(tracker_id=tracker.id, delivery_id="takeover")
    takeover = NOW + timedelta(minutes=3)
    assert crud_forge_webhook_delivery.claim(db_session, now=NOW, **key)
    assert crud_forge_webhook_delivery.claim(db_session, now=takeover, **key)
    if late_action == "release":
        crud_forge_webhook_delivery.mark_published(
            db_session, now=takeover, claimed_at=takeover, **key
        )
        crud_forge_webhook_delivery.release(db_session, claimed_at=NOW, **key)
        assert not crud_forge_webhook_delivery.claim(
            db_session, now=takeover + timedelta(days=1), **key
        )
    else:
        crud_forge_webhook_delivery.mark_published(
            db_session, now=takeover, claimed_at=NOW, **key
        )
        # A stale acknowledgement must not prevent recovery of the new holder.
        assert crud_forge_webhook_delivery.claim(
            db_session, now=takeover + timedelta(minutes=3), **key
        )


@pytest.mark.asyncio
async def test_refresh_only_merge_updates_rollup_and_readiness(
    db_session: Session,
    test_user: Any,
    forge: FakeForge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.services import fleet_code_activity
    from preloop.services.readiness import scheduler

    tracker = models.Tracker(
        name="refresh-merge",
        tracker_type="github",
        api_key="test",
        url="https://github.com",
        account_id=test_user.account_id,
    )
    db_session.add(tracker)
    db_session.flush()
    rollup = crud_issue_cost.get_or_create_rollup(
        db_session,
        account_id=test_user.account_id,
        tracker_id=tracker.id,
        issue_key="acme/widgets#42",
    )
    row = make_pr(db_session, test_user.account_id, github_key(42), rollup_id=rollup.id)
    assert rollup.merged_at is None
    forge.answers["/repos/acme/widgets/pulls/42"] = {
        "state": "closed",
        "merged": True,
        "merged_at": NOW.isoformat(),
        "head": {"sha": HEAD},
    }
    emit = MagicMock()
    monkeypatch.setattr(fleet_code_activity, "emit_merge_event", emit)
    monkeypatch.setattr(scheduler.settings, "ticket_readiness_enabled", True)
    await refresh.refresh_pull_requests(db_session, now=NOW)
    db_session.refresh(rollup)
    assert rollup.merged_at == NOW
    job = db_session.query(models.ReadinessJob).filter_by(pr_id=row.id).one()
    assert job.closed is True
    emit.assert_called_once_with(
        db_session,
        account_id=test_user.account_id,
        pr_key=github_key(42),
        at=NOW,
    )
    assert (
        await refresh.refresh_pull_requests(db_session, now=NOW + timedelta(minutes=5))
    )["read"] == 0
    assert emit.call_count == 1


@pytest.mark.asyncio
async def test_merge_webhook_during_refresh_emits_only_once(
    db_session: Session,
    test_user: Any,
    forge: FakeForge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.services import fleet_code_activity, issue_cost_rollup
    from preloop.services.readiness import scheduler

    row = make_pr(db_session, test_user.account_id, github_key(43))
    emit = MagicMock()
    schedule = MagicMock()
    monkeypatch.setattr(fleet_code_activity, "emit_merge_event", emit)
    monkeypatch.setattr(scheduler, "schedule_pull_request", schedule)

    async def merge_during_read(path: str, etag: str | None) -> ConditionalResponse:
        payload = {
            "number": 43,
            "html_url": github_key(43),
            "state": "closed",
            "merged": True,
            "merged_at": NOW.isoformat(),
            "head": {"sha": HEAD},
        }
        recorded = issue_cost_rollup.record_pull_request_event(
            db_session,
            {
                "source": "github",
                "type": "pull_request_merged",
                "account_id": str(test_user.account_id),
                "payload": {
                    "pull_request": payload,
                    "repository": {"full_name": "acme/widgets"},
                },
            },
            now=NOW,
        )
        assert recorded is not None
        db_session.commit()
        return ConditionalResponse(200, payload, None)

    monkeypatch.setattr(forge, "conditional_get", merge_during_read)
    await refresh.refresh_pull_requests(db_session, now=NOW)
    db_session.refresh(row)
    assert row.merged_at == NOW
    assert row.state == "merged"
    emit.assert_called_once()
    schedule.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 304])
async def test_existing_webhook_merge_survives_a_stale_refresh(
    db_session: Session,
    test_user: Any,
    forge: FakeForge,
    status: int,
) -> None:
    # Older webhook writers persisted only merged_at. Normalize these rows
    # under the refresh lock even if the forge answers 304 or stale closed.
    row = make_pr(db_session, test_user.account_id, github_key(44), merged_at=NOW)
    forge.answers["/repos/acme/widgets/pulls/44"] = (
        304
        if status == 304
        else {"state": "closed", "merged": False, "head": {"sha": HEAD}}
    )
    await refresh.refresh_pull_requests(db_session, now=NOW)
    db_session.refresh(row)
    assert row.state == "merged"
    assert row.merged_at == NOW
    assert row.refresh_due_at is None
