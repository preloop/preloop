"""Deterministic weekly signals: counts, percentiles, and tier-1 aggregates."""

from __future__ import annotations

import math
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator, Optional

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account, crud_spend_outlier_finding, crud_user
from preloop.models.models.user import User
from preloop.services.cache_accounting import (
    compute_cache_hit_ratio,
    uncached_input_tokens,
)
from preloop.services.weekly_signals import (
    CACHE_MISS_MAX_RATIO,
    CACHE_MISS_MIN_PREFIX_CHURN,
    CACHE_MISS_MIN_REQUESTS,
    LONG_TOOL_LOOP,
    OVERSIZED_CONTEXT_TOKENS,
    REPEATED_ERROR_SESSIONS,
    REPEATED_FAILURE_COUNT,
    UNUSED_TOOL_MIN_SESSIONS,
    ApprovalSignals,
    LatencyBucket,
    RepeatedErrorSignal,
    SessionEfficiency,
    Tier1Highlights,
    WeeklySignals,
    collect_weekly_signals,
    group_by_user,
    tier1_highlights,
)

START = datetime(2026, 10, 5, tzinfo=UTC)
END = datetime(2026, 10, 12, tzinfo=UTC)


def _naive(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(tzinfo=None)


def _percentile_cont(values: list[float], quantile: float) -> float:
    """PostgreSQL ``percentile_cont`` for a non-empty sample."""
    ordered = sorted(values)
    rank = 1 + quantile * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low - 1]
    weight = rank - low
    return ordered[low - 1] * (1 - weight) + ordered[high - 1] * weight


@contextmanager
def _selects(db_engine: Any) -> Iterator[list[str]]:
    """Collect SELECT/WITH statements issued while the block runs."""
    statements: list[str] = []

    def capture(_connection: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        head = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        if head in {"SELECT", "WITH"}:
            statements.append(statement)

    event.listen(db_engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(db_engine, "before_cursor_execute", capture)


def _user(db: Session, account_id: Any, email: str) -> User:
    user = crud_user.create(
        db,
        obj_in={
            "account_id": account_id,
            "email": email,
            "username": email.split("@")[0],
            "full_name": "Jane Doe",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    db.flush()
    return user


def _usage(
    db: Session,
    user: User,
    *,
    at: datetime,
    duration: float,
    status_code: int = 200,
    model: str = "fast",
    error_class: Optional[str] = None,
    cost: float = 0.0,
    purpose: Optional[str] = None,
    action_type: str = "model_gateway",
    session_id: Optional[uuid.UUID] = None,
    prompt_tokens: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
    cache_creation_tokens: Optional[int] = None,
    tools: Optional[list[str]] = None,
    is_retry: Optional[bool] = None,
    tool_call_count: Optional[int] = None,
) -> None:
    meta: dict[str, Any] = {}
    if purpose is not None:
        meta["purpose"] = purpose
    if tools is not None:
        meta["tools_meta"] = [
            {"name": name, "source": "payload", "schema_tokens_estimate": 10}
            for name in tools
        ]
    db.add(
        models.ApiUsage(
            user_id=user.id,
            account_id=user.account_id,
            endpoint="/v1/chat/completions",
            method="POST",
            status_code=status_code,
            duration=duration,
            action_type=action_type,
            model_alias=model,
            error_class=error_class,
            estimated_cost=cost,
            runtime_session_id=session_id,
            prompt_tokens=prompt_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            is_retry=is_retry,
            tool_call_count=tool_call_count,
            meta_data=meta or None,
            timestamp=_naive(at),
        )
    )
    db.flush()


def _session(db: Session, account_id: Any) -> models.RuntimeSession:
    session = models.RuntimeSession(
        account_id=account_id,
        session_source_type="cli",
        session_source_id=str(uuid.uuid4()),
        title="Weekly signals session",
        started_at=_naive(START),
    )
    db.add(session)
    db.flush()
    return session


def _approval_request(db: Session, user: User, token: str) -> models.ApprovalRequest:
    workflow = models.ApprovalWorkflow(
        account_id=user.account_id,
        name=f"wf-{token}",
        approval_type="manual",
    )
    db.add(workflow)
    db.flush()
    tool = models.ToolConfiguration(
        tool_name=f"tool-{token}",
        tool_source="builtin",
        account_id=user.account_id,
        approval_workflow_id=workflow.id,
    )
    db.add(tool)
    db.flush()
    request = models.ApprovalRequest(
        account_id=user.account_id,
        tool_configuration_id=tool.id,
        approval_workflow_id=workflow.id,
        tool_name=tool.tool_name,
        tool_args={},
        status="pending",
        requested_at=_naive(START),
        approval_token=token,
    )
    db.add(request)
    db.flush()
    return request


def _event(
    db: Session,
    request: models.ApprovalRequest,
    event_type: str,
    detail: str,
    at: datetime,
) -> None:
    if event_type == "approval_requested":
        request.requested_at = _naive(at)
    elif event_type == "approval_complete":
        request.resolved_at = _naive(at)
        request.status = "declined" if "declined" in detail else "approved"
        if detail.startswith("Auto-approved"):
            request.auto_approved_reason = "bypass"
    elif event_type == "expired":
        request.status = "expired"
        request.resolved_at = _naive(at)
    db.add(
        models.ApprovalEvent(
            approval_request_id=request.id,
            account_id=request.account_id,
            event_type=event_type,
            detail=detail,
            timestamp=_naive(at),
        )
    )
    db.flush()


def test_empty_window_is_zeroed_and_bounded(
    db_session: Session, db_engine: Any, test_user: User
) -> None:
    """No rows yields zeros and empty lists, in at most six queries."""
    with _selects(db_engine) as statements:
        signals = collect_weekly_signals(
            db_session,
            account_id=test_user.account_id,
            start=START,
            end=END,
        )

    assert len(statements) <= 6
    assert signals.approvals == ApprovalSignals(0, 0, 0, 0, 0.0)
    assert signals.first_seen == ()
    assert signals.overall == LatencyBucket(None, 0, 0, 0.0, 0.0, 0.0)
    assert signals.by_model == ()
    assert signals.top_error_classes == ()
    assert signals.by_user == ()
    assert signals.sessions == ()
    assert signals.spend_outliers == ()
    assert signals.highlights == Tier1Highlights(0, 0, (), 0, 0, ())
    assert group_by_user(signals) == ()


def test_gateway_error_rate_and_percentiles(
    db_session: Session, db_engine: Any, test_user: User
) -> None:
    """A known duration sample has an exact error rate and p95."""
    other = _user(db_session, test_user.account_id, "jane@example.com")
    fast = [1.0, 2.0, 3.0, 4.0]
    slow = [10.0, 20.0, 30.0]
    fast_status = [200, 200, 500, 500]
    fast_errors = [None, None, "upstream_overloaded", "upstream_overloaded"]
    slow_status = [200, 400, 400]
    slow_errors = [None, "network", "upstream_auth"]
    for offset, (duration, status, error_class) in enumerate(
        zip(fast, fast_status, fast_errors, strict=True)
    ):
        _usage(
            db_session,
            test_user,
            at=START + timedelta(hours=1, minutes=offset),
            duration=duration,
            status_code=status,
            model="fast",
            error_class=error_class,
            cost=1.5,
        )
    for offset, (duration, status, error_class) in enumerate(
        zip(slow, slow_status, slow_errors, strict=True)
    ):
        _usage(
            db_session,
            other,
            at=START + timedelta(hours=2, minutes=offset),
            duration=duration,
            status_code=status,
            model="slow",
            error_class=error_class,
            cost=2.0,
        )

    with _selects(db_engine) as statements:
        signals = collect_weekly_signals(
            db_session,
            account_id=str(test_user.account_id),
            start=START,
            end=END,
        )

    assert len(statements) <= 6
    overall = fast + slow
    assert signals.overall.request_count == 7
    assert signals.overall.error_count == 4
    assert signals.overall.error_rate == 4 / 7
    assert signals.overall.p50_duration == pytest.approx(_percentile_cont(overall, 0.5))
    assert signals.overall.p95_duration == pytest.approx(
        _percentile_cont(overall, 0.95)
    )
    assert signals.overall.p95_duration == pytest.approx(27.0)

    by_model = {bucket.model_alias: bucket for bucket in signals.by_model}
    assert by_model["fast"].error_rate == 0.5
    assert by_model["fast"].p50_duration == pytest.approx(_percentile_cont(fast, 0.5))
    assert by_model["fast"].p95_duration == pytest.approx(_percentile_cont(fast, 0.95))
    assert by_model["slow"].error_rate == 2 / 3
    assert by_model["slow"].p95_duration == pytest.approx(_percentile_cont(slow, 0.95))
    assert [(item.error_class, item.count) for item in signals.top_error_classes] == [
        ("upstream_overloaded", 2),
        ("network", 1),
        ("upstream_auth", 1),
    ]

    grouped = {row.user_id: row for row in group_by_user(signals)}
    assert grouped[test_user.id].request_count == 4
    assert grouped[test_user.id].estimated_cost == pytest.approx(6.0)
    assert grouped[test_user.id].p95_duration == pytest.approx(
        _percentile_cont(fast, 0.95)
    )
    assert grouped[other.id].estimated_cost == pytest.approx(6.0)
    assert grouped[other.id].error_rate == 2 / 3


def test_window_excludes_edges_replay_and_other_accounts(
    db_session: Session, test_user: User
) -> None:
    """Only in-window gateway rows for this account affect the rate."""
    _usage(db_session, test_user, at=START, duration=1.0, status_code=200)
    _usage(
        db_session,
        test_user,
        at=END,
        duration=99.0,
        status_code=500,
        error_class="network",
    )
    _usage(
        db_session,
        test_user,
        at=START - timedelta(seconds=1),
        duration=99.0,
        status_code=500,
        error_class="network",
    )
    _usage(
        db_session,
        test_user,
        at=START + timedelta(hours=1),
        duration=99.0,
        status_code=500,
        error_class="network",
        purpose="replay_validation",
    )
    _usage(
        db_session,
        test_user,
        at=START + timedelta(hours=1),
        duration=99.0,
        status_code=500,
        action_type="create_issue",
        error_class="network",
    )
    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    outsider = _user(db_session, other_account.id, "outsider@example.com")
    _usage(
        db_session,
        outsider,
        at=START + timedelta(hours=1),
        duration=99.0,
        status_code=500,
    )

    signals = collect_weekly_signals(
        db_session, account_id=test_user.account_id, start=START, end=END
    )
    assert signals.overall.request_count == 1
    assert signals.overall.error_count == 0
    assert signals.overall.error_rate == 0.0
    assert signals.overall.p50_duration == pytest.approx(1.0)
    assert signals.overall.p95_duration == pytest.approx(1.0)
    assert signals.top_error_classes == ()


def test_approval_counts_and_median(db_session: Session, test_user: User) -> None:
    """Human grants and declines count; auto-approvals and expiry do not."""
    granted = _approval_request(db_session, test_user, "granted")
    _event(
        db_session,
        granted,
        "approval_requested",
        "Approval requested",
        START + timedelta(hours=1),
    )
    _event(
        db_session,
        granted,
        "approval_complete",
        "Approval request approved",
        START + timedelta(hours=1, seconds=10),
    )
    _event(
        db_session,
        granted,
        "notification_sent",
        "Notification via email (sent)",
        START + timedelta(hours=1, seconds=1),
    )

    denied = _approval_request(db_session, test_user, "denied")
    _event(
        db_session,
        denied,
        "approval_requested",
        "Approval requested",
        START + timedelta(hours=2),
    )
    _event(
        db_session,
        denied,
        "vote_received",
        "Decline vote received",
        START + timedelta(hours=2, seconds=5),
    )
    _event(
        db_session,
        denied,
        "approval_complete",
        "Approval request declined (quorum cannot be reached: 1 decline(s))",
        START + timedelta(hours=2, seconds=30),
    )

    expired = _approval_request(db_session, test_user, "expired")
    _event(
        db_session,
        expired,
        "approval_requested",
        "Approval requested",
        START + timedelta(hours=3),
    )
    _event(
        db_session,
        expired,
        "expired",
        "Expired: no response within the approval window",
        START + timedelta(hours=3, seconds=60),
    )

    automatic = _approval_request(db_session, test_user, "automatic")
    _event(
        db_session,
        automatic,
        "approval_requested",
        "Approval requested",
        START + timedelta(hours=4),
    )
    _event(
        db_session,
        automatic,
        "approval_complete",
        "Auto-approved without review: standing bypass",
        START + timedelta(hours=4, seconds=1),
    )

    early = _approval_request(db_session, test_user, "early")
    _event(
        db_session,
        early,
        "approval_requested",
        "Approval requested",
        START - timedelta(hours=1),
    )
    _event(
        db_session,
        early,
        "approval_complete",
        "Approval request approved (quorum 2/2 met)",
        START + timedelta(hours=5),
    )

    outside = _approval_request(db_session, test_user, "outside")
    _event(
        db_session,
        outside,
        "approval_requested",
        "Approval requested",
        START - timedelta(days=2),
    )
    _event(
        db_session,
        outside,
        "approval_complete",
        "Approval request approved",
        START - timedelta(days=2, seconds=-10),
    )

    at_end = _approval_request(db_session, test_user, "at-end")
    _event(db_session, at_end, "approval_requested", "Approval requested", END)

    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    outsider = _user(db_session, other_account.id, "outsider@example.com")
    foreign = _approval_request(db_session, outsider, "foreign")
    _event(
        db_session,
        foreign,
        "approval_requested",
        "Approval requested",
        START + timedelta(hours=1),
    )
    _event(
        db_session,
        foreign,
        "approval_complete",
        "Approval request approved",
        START + timedelta(hours=1, seconds=10),
    )

    signals = collect_weekly_signals(
        db_session, account_id=test_user.account_id, start=START, end=END
    )
    assert signals.approvals == ApprovalSignals(
        requested=3,
        granted=2,
        denied=1,
        expired=1,
        median_decision_seconds=30.0,
        auto_approved=1,
        auto_approved_reasons=({"reason": "bypass", "count": 1, "agent_count": 0},),
    )


def test_first_seen_is_limited_to_the_window(
    db_session: Session, test_user: User
) -> None:
    """Subjects, agents, clients, and tools are new only on their first hit."""
    key = models.ApiKey(
        account_id=test_user.account_id,
        user_id=test_user.id,
        name="gateway",
        key=f"pk_{uuid.uuid4().hex}",
        scopes=[],
        is_active=True,
    )
    db_session.add(key)
    db_session.flush()
    db_session.add(
        models.GatewaySubject(
            account_id=test_user.account_id,
            api_key_id=key.id,
            external_subject="sub-new",
            email="jane@example.com",
            linked_user_id=test_user.id,
            first_seen_at=START + timedelta(hours=1),
            last_seen_at=START + timedelta(hours=1),
        )
    )
    db_session.add(
        models.GatewaySubject(
            account_id=test_user.account_id,
            api_key_id=key.id,
            external_subject="sub-old",
            email="old@example.com",
            first_seen_at=START - timedelta(days=1),
            last_seen_at=START + timedelta(hours=1),
        )
    )
    fresh_agent = models.DiscoveredAgentCandidate(
        account_id=test_user.account_id,
        workstation_fingerprint="a" * 64,
        agent_kind="cursor",
        config_path_hash="b" * 64,
        mcp_server_count=1,
        reported_enrolled=False,
        status="new",
        first_seen_at=_naive(START + timedelta(hours=2)),
        last_seen_at=_naive(START + timedelta(hours=2)),
    )
    db_session.add(fresh_agent)
    db_session.add(
        models.DiscoveredAgentCandidate(
            account_id=test_user.account_id,
            workstation_fingerprint="c" * 64,
            agent_kind="codex",
            config_path_hash="d" * 64,
            mcp_server_count=0,
            reported_enrolled=False,
            status="new",
            first_seen_at=_naive(START - timedelta(days=3)),
            last_seen_at=_naive(START + timedelta(hours=2)),
        )
    )
    client = models.CliClient(
        client_id=uuid.uuid4(),
        cli_version="1.2.3",
        account_id=test_user.account_id,
        first_seen=START + timedelta(hours=3),
        last_seen=START + timedelta(hours=3),
        check_count=1,
    )
    db_session.add(client)
    db_session.add(
        models.CliClient(
            client_id=uuid.uuid4(),
            cli_version="0.9.0",
            account_id=test_user.account_id,
            first_seen=START - timedelta(days=1),
            last_seen=START + timedelta(hours=3),
            check_count=4,
        )
    )
    db_session.flush()
    _usage(
        db_session,
        test_user,
        at=START + timedelta(hours=4),
        duration=0.2,
        tools=["new_tool", "old_tool"],
    )
    _usage(
        db_session,
        test_user,
        at=START - timedelta(days=1),
        duration=0.2,
        tools=["old_tool"],
    )
    _usage(
        db_session,
        test_user,
        at=END + timedelta(hours=1),
        duration=0.2,
        tools=["future_tool", "new_tool"],
    )
    _usage(
        db_session,
        test_user,
        at=START + timedelta(hours=4),
        duration=0.2,
        tools=["replay_tool"],
        purpose="replay_validation",
    )

    signals = collect_weekly_signals(
        db_session, account_id=test_user.account_id, start=START, end=END
    )
    seen = [(item.kind, item.label) for item in signals.first_seen]
    assert seen == [
        ("gateway_subject", "jane@example.com"),
        ("discovered_agent", "cursor"),
        ("cli_client", "1.2.3"),
        ("mcp_tool", "new_tool"),
    ]
    assert signals.first_seen[0].user_id == test_user.id
    assert signals.first_seen[1].item_key == str(fresh_agent.id)
    assert signals.first_seen[2].item_key == str(client.client_id)
    developers = {row.user_id: row for row in group_by_user(signals)}
    assert [item.kind for item in developers[test_user.id].first_seen] == [
        "gateway_subject"
    ]
    assert [item.kind for item in developers[None].first_seen] == [
        "discovered_agent",
        "cli_client",
        "mcp_tool",
    ]


def test_session_tier1_aggregates(db_session: Session, test_user: User) -> None:
    """Cache ratio, prefix churn, retries, loops, and unused tools are exact."""
    session = _session(db_session, test_user.account_id)
    quiet = _session(db_session, test_user.account_id)
    plan = [
        ("small", ["alpha", "beta"], 100, 80, 200, None, False),
        ("small", ["alpha", "beta"], 100, 80, 200, None, False),
        ("small", ["alpha", "gamma"], 100, 0, 500, "upstream_overloaded", True),
        ("large", ["alpha", "gamma"], 100, 0, 500, "upstream_overloaded", True),
        ("large", ["alpha", "gamma"], 100, 0, 500, "upstream_overloaded", False),
        ("large", ["alpha", "gamma"], 100, 0, 400, "network", False),
    ]
    for offset, (
        model,
        tools,
        prompt,
        cache_read,
        status,
        error_class,
        retry,
    ) in enumerate(plan):
        _usage(
            db_session,
            test_user,
            at=START + timedelta(minutes=offset),
            duration=1.0,
            status_code=status,
            model=model,
            error_class=error_class,
            session_id=session.id,
            prompt_tokens=prompt,
            cache_read_tokens=cache_read,
            tools=tools,
            is_retry=retry,
        )
    _usage(
        db_session,
        test_user,
        at=START + timedelta(minutes=10),
        duration=1.0,
        status_code=200,
        model="large",
        session_id=session.id,
        prompt_tokens=999_999,
        cache_read_tokens=0,
        tools=["replay_only"],
        purpose="replay_validation",
    )
    _usage(
        db_session,
        test_user,
        at=START + timedelta(hours=1),
        duration=1.0,
        status_code=200,
        model="small",
        session_id=quiet.id,
        prompt_tokens=50,
        tools=["quiet_tool"],
    )
    moment = START + timedelta(hours=2)
    for offset, (tool_name, tool_status) in enumerate(
        [
            ("alpha", "success"),
            ("alpha", "failed"),
            ("alpha", "failed"),
            ("alpha", "failed"),
            ("beta", "success"),
        ]
    ):
        db_session.add(
            models.RuntimeSessionActivity(
                account_id=test_user.account_id,
                runtime_session_id=session.id,
                activity_type="tool_call",
                tool_name=tool_name,
                status=tool_status,
                timestamp=moment + timedelta(seconds=offset),
            )
        )
    db_session.add(
        models.RuntimeSessionActivity(
            account_id=test_user.account_id,
            runtime_session_id=session.id,
            activity_type="model_interaction",
            timestamp=moment + timedelta(seconds=10),
        )
    )
    db_session.add(
        models.RuntimeSessionActivity(
            account_id=test_user.account_id,
            runtime_session_id=session.id,
            activity_type="tool_call",
            tool_name="alpha",
            status="success",
            timestamp=moment + timedelta(seconds=11),
        )
    )
    db_session.flush()

    crud_spend_outlier_finding.record(
        db_session,
        account_id=test_user.account_id,
        rule="daily_spend",
        user_id=test_user.id,
        day=START.date(),
        item_id=f"spend:daily_spend:{test_user.id}",
        fingerprint=f"daily_spend|{test_user.id}|2026-10-05",
        details={"spend_usd": 40.0, "median_usd": 10.0},
        detected_at=START + timedelta(days=1),
        commit=False,
    )
    crud_spend_outlier_finding.record(
        db_session,
        account_id=test_user.account_id,
        rule="model_mix",
        user_id=test_user.id,
        day=START.date(),
        item_id=f"spend:model_mix:{test_user.id}",
        fingerprint=f"model_mix|{test_user.id}|2026-10-04",
        details={"share": 0.8},
        detected_at=START - timedelta(days=1),
        commit=False,
    )

    signals = collect_weekly_signals(
        db_session, account_id=test_user.account_id, start=START, end=END
    )
    by_session = {row.runtime_session_id: row for row in signals.sessions}
    busy = by_session[session.id]
    expected_ratio = compute_cache_hit_ratio(
        cached_tokens=160,
        uncached_tokens=uncached_input_tokens(
            prompt_tokens=600, cache_read_tokens=160, cache_write_tokens=0
        ),
    )
    assert busy.request_count == 6
    assert busy.user_id == test_user.id
    assert busy.cache_read_tokens == 160
    assert busy.cache_read_ratio == expected_ratio
    assert busy.prefix_churn == 2
    assert busy.retry_count == 2
    assert busy.error_class_counts == (
        ("upstream_overloaded", 3),
        ("network", 1),
    )
    assert busy.failed_tool_counts == (("alpha", 3),)
    assert busy.max_tool_loop == 5
    assert busy.max_context_tokens == 100
    assert busy.tools_attached == ("alpha", "beta", "gamma")
    assert busy.tools_used == ("alpha", "beta")
    assert busy.unused_tools == ("gamma",)

    assert by_session[quiet.id].cache_read_ratio is None
    assert by_session[quiet.id].request_count == 1
    assert by_session[quiet.id].prefix_churn == 0
    assert by_session[quiet.id].max_context_tokens == 50
    assert by_session[quiet.id].tools_attached == ("quiet_tool",)
    assert by_session[quiet.id].tools_used == ()
    assert by_session[quiet.id].unused_tools == ("quiet_tool",)

    assert signals.highlights.cache_miss_sessions == 0
    assert signals.highlights.repeated_failure_sessions == 0
    assert signals.highlights.long_tool_loop_sessions == 0
    assert signals.highlights.unused_tools == ()
    assert [item.rule for item in signals.spend_outliers] == ["daily_spend"]
    developer = group_by_user(signals)[0]
    assert developer.user_id == test_user.id
    assert len(developer.sessions) == 2
    assert [item.rule for item in developer.spend_outliers] == ["daily_spend"]


def test_mcp_advertised_name_matches_the_bare_invocation(
    db_session: Session, test_user: User
) -> None:
    """An ``mcp__server__tool`` advertisement is the same tool as a bare call."""
    session = _session(db_session, test_user.account_id)
    _usage(
        db_session,
        test_user,
        at=START + timedelta(minutes=1),
        duration=0.4,
        session_id=session.id,
        prompt_tokens=100,
        cache_read_tokens=None,
        cache_creation_tokens=40,
        tools=["mcp__docs__search"],
    )
    db_session.add(
        models.RuntimeSessionActivity(
            account_id=test_user.account_id,
            runtime_session_id=session.id,
            activity_type="tool_call",
            tool_name="search",
            status="success",
            timestamp=START + timedelta(minutes=2),
        )
    )
    db_session.add(
        models.RuntimeSessionActivity(
            account_id=test_user.account_id,
            runtime_session_id=session.id,
            activity_type="tool_call",
            tool_name="search",
            status="refused",
            timestamp=START + timedelta(minutes=3),
        )
    )
    db_session.flush()

    signals = collect_weekly_signals(
        db_session, account_id=test_user.account_id, start=START, end=END
    )
    row = signals.sessions[0]
    assert row.tools_attached == ("search",)
    assert row.tools_used == ("search",)
    assert row.unused_tools == ()
    assert row.failed_tool_counts == (("search", 1),)
    # A write with no cache read is still covered. uncached = 100 - 0 - 40.
    assert row.cache_read_ratio == 0.0
    assert row.cache_read_tokens == 0


def _efficiency(**overrides: Any) -> SessionEfficiency:
    base: dict[str, Any] = {
        "runtime_session_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "request_count": 1,
        "cache_read_tokens": 0,
        "cache_read_ratio": None,
        "prefix_churn": 0,
        "retry_count": 0,
        "error_class_counts": (),
        "failed_tool_counts": (),
        "max_tool_loop": 0,
        "max_context_tokens": 0,
        "tools_attached": (),
        "tools_used": (),
        "unused_tools": (),
    }
    base.update(overrides)
    return SessionEfficiency(**base)


def test_tier1_highlights_use_the_spec_thresholds() -> None:
    """Thresholds match the inefficiency taxonomy, and short sessions stay quiet."""
    user_id = uuid.uuid4()
    quiet = _efficiency(user_id=user_id, request_count=6, cache_read_ratio=0.0)
    cache_miss = _efficiency(
        user_id=user_id,
        request_count=CACHE_MISS_MIN_REQUESTS,
        cache_read_ratio=CACHE_MISS_MAX_RATIO - 0.01,
        prefix_churn=CACHE_MISS_MIN_PREFIX_CHURN,
    )
    almost_cache_miss = _efficiency(
        user_id=user_id,
        request_count=CACHE_MISS_MIN_REQUESTS - 1,
        cache_read_ratio=0.0,
        prefix_churn=CACHE_MISS_MIN_PREFIX_CHURN,
    )
    retries = _efficiency(user_id=user_id, retry_count=REPEATED_FAILURE_COUNT)
    loop = _efficiency(user_id=user_id, max_tool_loop=LONG_TOOL_LOOP)
    oversized = _efficiency(
        user_id=user_id, max_context_tokens=OVERSIZED_CONTEXT_TOKENS
    )
    error_sessions = [
        _efficiency(
            user_id=user_id,
            error_class_counts=(("upstream_overloaded", 1),),
        )
        for _ in range(REPEATED_ERROR_SESSIONS)
    ]
    attached = [
        _efficiency(
            user_id=user_id,
            tools_attached=("shell",),
            tools_used=(),
            unused_tools=("shell",),
        )
        for _ in range(UNUSED_TOOL_MIN_SESSIONS)
    ]
    editors = [
        _efficiency(
            user_id=user_id,
            tools_attached=("editor",),
            tools_used=(),
            unused_tools=("editor",),
        )
        for _ in range(UNUSED_TOOL_MIN_SESSIONS - 1)
    ]
    editors.append(
        _efficiency(
            user_id=user_id,
            tools_attached=("editor",),
            tools_used=("editor",),
            unused_tools=(),
        )
    )

    highlights = tier1_highlights(
        [
            quiet,
            cache_miss,
            almost_cache_miss,
            retries,
            loop,
            oversized,
            *error_sessions,
            *attached,
            *editors,
        ]
    )
    assert highlights.cache_miss_sessions == 1
    assert highlights.repeated_failure_sessions == 1
    assert highlights.long_tool_loop_sessions == 1
    assert highlights.oversized_context_sessions == 1
    assert highlights.repeated_error_classes == (
        RepeatedErrorSignal(
            error_class="upstream_overloaded",
            sessions=REPEATED_ERROR_SESSIONS,
            occurrences=REPEATED_ERROR_SESSIONS,
        ),
    )
    assert len(highlights.unused_tools) == 1
    assert highlights.unused_tools[0].tool_name == "shell"
    assert highlights.unused_tools[0].sessions_attached == UNUSED_TOOL_MIN_SESSIONS
    assert tier1_highlights([quiet]) == Tier1Highlights(0, 0, (), 0, 0, ())
    prefixed = [
        _efficiency(
            user_id=user_id,
            tools_attached=("mcp__docs__shell",),
            tools_used=("shell",),
        )
        for _ in range(UNUSED_TOOL_MIN_SESSIONS)
    ]
    assert tier1_highlights(prefixed).unused_tools == ()


def test_group_by_user_orders_developers_and_keeps_unattributed() -> None:
    """The digest helper does not drop rows that have no user."""
    first = uuid.uuid4()
    second = uuid.uuid4()
    low, high = sorted((first, second), key=str)
    signals = WeeklySignals(
        approvals=ApprovalSignals(1, 0, 0, 0, 0.0),
        first_seen=(),
        overall=LatencyBucket(None, 0, 0, 0.0, 0.0, 0.0),
        by_model=(),
        top_error_classes=(),
        by_user=(),
        sessions=(
            _efficiency(user_id=high, request_count=2),
            _efficiency(user_id=None, request_count=1),
            _efficiency(user_id=low, request_count=3),
        ),
        spend_outliers=(),
        highlights=Tier1Highlights(0, 0, (), 0, 0, ()),
    )
    grouped = group_by_user(signals)
    assert [row.user_id for row in grouped] == [low, high, None]
    assert [row.sessions[0].request_count for row in grouped] == [3, 2, 1]


def test_native_automatic_approvals_do_not_inflate_human_requests(
    db_session: Session, test_user: User
) -> None:
    """Repeated audit events cannot multiply real requests or human latency."""
    for index in range(3):
        request = _approval_request(db_session, test_user, f"native-{index}")
        request.status = "approved"
        request.auto_approved_reason = "native_tool_approvals_off"
        request.managed_agent_id = uuid.UUID(int=1 if index < 2 else 2)
        request.resolved_at = _naive(START + timedelta(seconds=2))
        for _ in range(2):
            _event(
                db_session, request, "approval_requested", "Approval requested", START
            )
    pending = _approval_request(db_session, test_user, "waiting-for-human")
    _event(db_session, pending, "approval_requested", "Approval requested", START)
    signals = collect_weekly_signals(
        db_session, account_id=test_user.account_id, start=START, end=END
    )
    assert signals.approvals.requested == 1
    assert signals.approvals.pending == 1
    assert signals.approvals.granted == 0
    assert signals.approvals.median_decision_seconds == 0
    assert signals.approvals.auto_approved == 3
    assert signals.approvals.auto_approved_reasons == (
        {"reason": "native_tool_approvals_off", "count": 3, "agent_count": 2},
    )


def test_first_seen_tool_usage_counts_calls_instead_of_schema_exposures(
    db_session: Session, test_user: User
) -> None:
    session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="test",
        session_source_id="new-tool-usage",
        started_at=_naive(START),
    )
    db_session.add(session)
    db_session.flush()
    _usage(
        db_session,
        test_user,
        at=START + timedelta(hours=1),
        duration=0.1,
        session_id=session.id,
        tools=["mcp__tracker__search"],
    )
    for moment in [START, START + timedelta(hours=1), END]:
        db_session.add(
            models.RuntimeSessionActivity(
                account_id=test_user.account_id,
                runtime_session_id=session.id,
                activity_type="tool_call",
                tool_name="search",
                server_name="tracker",
                timestamp=moment,
            )
        )
    db_session.flush()
    signals = collect_weekly_signals(
        db_session, account_id=test_user.account_id, start=START, end=END
    )
    tool = next(item for item in signals.first_seen if item.kind == "mcp_tool")
    assert tool.usage_count == 2


def test_exception_pending_backlog_keeps_old_requests_and_detects_replacements(
    db_session: Session, test_user: User
) -> None:
    from preloop.models.crud.weekly_signals import approval_signals

    old = _approval_request(db_session, test_user, "old-pending-exception")
    old.requested_at = _naive(START - timedelta(days=10))
    db_session.flush()
    args = dict(account_id=test_user.account_id, start=START, end=END)
    assert approval_signals(db_session, **args).pending == 0
    before = approval_signals(db_session, **args, pending_backlog=True)
    assert before.pending == 1
    old.status = "approved"
    old.resolved_at = _naive(START)
    _approval_request(db_session, test_user, "replacement-pending-exception")
    db_session.flush()
    after = approval_signals(db_session, **args, pending_backlog=True)
    assert after.pending == before.pending
    assert after.pending_fingerprint != before.pending_fingerprint
    # Unknown member cannot inherit account-wide requests.
    scoped = approval_signals(
        db_session, **args, user_id=uuid.uuid4(), pending_backlog=True
    )
    assert scoped.pending == 0
