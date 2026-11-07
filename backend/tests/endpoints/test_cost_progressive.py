"""Selective Cost reads must preserve totals without running unused queries."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock

import pytest
from preloop.models.crud import crud_api_usage
from preloop.services.tool_usage_stats import ToolUsageStatsService

BREAKDOWNS = {
    "models": "get_gateway_usage_by_model",
    "flows": "get_gateway_usage_by_flow",
    "sessions": "get_gateway_usage_by_session",
    "days": "get_gateway_usage_timeseries",
}


@pytest.mark.parametrize(
    "selected", [None, "models", "flows", "sessions", "days", "tools"]
)
def test_cost_queries_only_requested_breakdowns(
    client: Any, db_session: Any, test_user: Any, monkeypatch: Any, selected: str | None
) -> None:
    """Unselected aggregations must not execute, even for nonempty usage."""
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        account_id=str(test_user.account_id),
        user_id=str(test_user.id),
        model_alias="example-model",
        prompt_tokens=12,
        completion_tokens=8,
        total_tokens=20,
        estimated_cost=0.05,
    )
    spies = {}
    for name, method in BREAKDOWNS.items():
        spy = Mock(wraps=getattr(crud_api_usage, method))
        monkeypatch.setattr(crud_api_usage, method, spy)
        spies[name] = spy
    tool_spy = Mock(return_value=[])
    monkeypatch.setattr(ToolUsageStatsService, "get_account_usage_by_tool", tool_spy)
    params = {"breakdown": selected} if selected else {"include_breakdown": "false"}
    response = client.get("/api/v1/cost/summary", params=params)
    assert response.status_code == 200
    body = response.json()
    assert body["total_requests"] == 1
    assert body["token_usage"]["total_tokens"] == 20
    assert body["estimated_cost"] == 0.05
    for name, spy in {**spies, "tools": tool_spy}.items():
        assert spy.call_count == int(name == selected), name


def test_cost_rejects_unknown_breakdown(client: Any) -> None:
    """A typo must not accidentally run every expensive aggregation."""
    assert client.get("/api/v1/cost/summary?breakdown=unknown").status_code == 422


@pytest.mark.parametrize("mode", ["light", "sessions", "imported", "default"])
def test_imported_totals_remain_separate_while_details_are_deferred(
    client: Any, db_session: Any, test_user: Any, monkeypatch: Any, mode: str
) -> None:
    """First paint keeps imported spend visible without loading its detail tables."""
    crud_api_usage.log_imported_usage_event(
        db_session,
        account_id=str(test_user.account_id),
        timestamp=datetime.now(UTC),
        model_alias="example-model",
        source="example",
        total_tokens=100,
        cost_usd=2.5,
        conversation_id="example-conversation",
    )
    spies = []
    for method in ("get_imported_usage_by_model", "get_imported_usage_by_conversation"):
        spy = Mock(wraps=getattr(crud_api_usage, method))
        monkeypatch.setattr(crud_api_usage, method, spy)
        spies.append(spy)
    params = {
        "light": {"include_breakdown": "false", "breakdown": "imported"},
        "sessions": {"breakdown": "sessions"},
        "imported": {"breakdown": "imported"},
        "default": {},
    }[mode]
    response = client.get("/api/v1/cost/summary", params=params)
    assert response.status_code == 200
    body = response.json()
    assert body["estimated_cost"] == 0
    assert body["total_requests"] == 0
    assert body["imported_usage"]["imported_cost"] == 2.5
    assert body["imported_usage"]["total_tokens"] == 100
    included = mode in ("imported", "default")
    for spy in spies:
        assert spy.call_count == int(included)
    assert bool(body["imported_usage"]["usage_by_model"]) == included
    assert bool(body["imported_usage"]["usage_by_conversation"]) == included


def test_repeated_selection_preserves_requested_groups(
    client: Any, monkeypatch: Any
) -> None:
    """Repeatable query parameters request a union, with duplicates executed once."""
    spies = {}
    for name, method in BREAKDOWNS.items():
        spy = Mock(return_value=[])
        monkeypatch.setattr(crud_api_usage, method, spy)
        spies[name] = spy
    response = client.get(
        "/api/v1/cost/summary?breakdown=sessions&breakdown=flows&breakdown=sessions"
    )
    assert response.status_code == 200
    assert {name: spy.call_count for name, spy in spies.items()} == {
        "models": 0,
        "flows": 1,
        "sessions": 1,
        "days": 0,
    }


def test_compare_window_rides_along_as_previous(
    client: Any, db_session: Any, test_user: Any
) -> None:
    """One request answers both the range and the comparison range (#1583)."""
    from datetime import timedelta

    now = datetime.now(UTC)
    for days_ago, cost in ((1, 0.25), (10, 1.5), (11, 0.5)):
        row = crud_api_usage.log_gateway_request(
            db_session,
            endpoint="/openai/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=0.1,
            account_id=str(test_user.account_id),
            model_alias="openai/gpt-5",
            provider_name="openai",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            estimated_cost=cost,
        )
        row.timestamp = (now - timedelta(days=days_ago)).replace(tzinfo=None)
    db_session.commit()

    params = {
        "start_date": (now - timedelta(days=7)).isoformat(),
        "end_date": now.isoformat(),
        "compare_start_date": (now - timedelta(days=14)).isoformat(),
        "compare_end_date": (now - timedelta(days=7)).isoformat(),
        "include_breakdown": "false",
    }
    body = client.get("/api/v1/cost/summary", params=params).json()
    assert body["estimated_cost"] == pytest.approx(0.25)
    assert body["previous"]["estimated_cost"] == pytest.approx(2.0)
    assert body["previous"]["total_requests"] == 2
    assert body["previous"]["usage_by_model"] == []
    assert body["previous"]["previous"] is None

    plain = client.get(
        "/api/v1/cost/summary",
        params={k: v for k, v in params.items() if not k.startswith("compare")},
    ).json()
    assert plain["previous"] is None

    half = dict(params)
    del half["compare_end_date"]
    assert client.get("/api/v1/cost/summary", params=half).status_code == 422


def test_summary_cache_reuses_queries_but_not_mutable_response(
    client: Any, monkeypatch: Any
) -> None:
    """An exact repeat reads no aggregates and cannot mutate the cached answer."""
    spy = Mock(wraps=crud_api_usage.get_gateway_usage_summary)
    monkeypatch.setattr(crud_api_usage, "get_gateway_usage_summary", spy)
    params = {
        "start_date": "2026-01-01T00:00:00Z",
        "end_date": "2026-01-31T00:00:00Z",
        "include_breakdown": "false",
    }
    first = client.get("/api/v1/cost/summary", params=params)
    assert first.status_code == 200
    assert spy.call_count == 1
    second = client.get("/api/v1/cost/summary", params=params)
    assert second.json() == first.json()
    assert spy.call_count == 1


@pytest.mark.parametrize(
    "change",
    [
        {"runtime_principal_id": "other-agent"},
        {"exclude_retries": "true"},
        {"breakdown": "sessions", "include_breakdown": "true"},
        {"end_date": "2026-02-01T00:00:00Z"},
        {"session_limit": 25},
        {"session_offset": 25},
    ],
)
def test_summary_cache_separates_filters(
    client: Any, monkeypatch: Any, change: dict[str, Any]
) -> None:
    """Filters and paging must not reuse a different aggregate answer."""
    spy = Mock(wraps=crud_api_usage.get_gateway_usage_summary)
    monkeypatch.setattr(crud_api_usage, "get_gateway_usage_summary", spy)
    params = {
        "start_date": "2026-01-01T00:00:00Z",
        "end_date": "2026-01-31T00:00:00Z",
        "include_breakdown": "false",
    }
    assert client.get("/api/v1/cost/summary", params=params).status_code == 200
    assert (
        client.get("/api/v1/cost/summary", params={**params, **change}).status_code
        == 200
    )
    assert spy.call_count == 2


def test_summary_cache_expires_and_invalidates_on_reprice(
    client: Any, test_user: Any, monkeypatch: Any
) -> None:
    """Expiry and repricing force fresh aggregates without wall-clock sleeps."""
    from preloop.api.endpoints import cost

    clock = Mock(return_value=1000.0)
    from types import SimpleNamespace

    monkeypatch.setattr(cost, "time", SimpleNamespace(monotonic=clock))
    spy = Mock(wraps=crud_api_usage.get_gateway_usage_summary)
    monkeypatch.setattr(crud_api_usage, "get_gateway_usage_summary", spy)
    params = {
        "start_date": "2026-01-01T00:00:00Z",
        "end_date": "2026-01-31T00:00:00Z",
        "include_breakdown": "false",
    }
    for _ in range(2):
        assert client.get("/api/v1/cost/summary", params=params).status_code == 200
    assert spy.call_count == 1
    # January 2026 is a closed window: the open-window minute does not expire it.
    clock.return_value += cost.SUMMARY_CACHE_TTL_SECONDS
    assert client.get("/api/v1/cost/summary", params=params).status_code == 200
    assert spy.call_count == 1
    clock.return_value += cost.CLOSED_SUMMARY_CACHE_TTL_SECONDS
    assert client.get("/api/v1/cost/summary", params=params).status_code == 200
    assert spy.call_count == 2
    cost._forget_cost_health(str(test_user.account_id))
    assert client.get("/api/v1/cost/summary", params=params).status_code == 200
    assert spy.call_count == 3


def test_open_window_summary_expires_after_a_minute(
    client: Any, monkeypatch: Any
) -> None:
    """A window still taking traffic keeps the short TTL."""
    from types import SimpleNamespace

    from preloop.api.endpoints import cost

    clock = Mock(return_value=5000.0)
    monkeypatch.setattr(cost, "time", SimpleNamespace(monotonic=clock))
    spy = Mock(wraps=crud_api_usage.get_gateway_usage_summary)
    monkeypatch.setattr(crud_api_usage, "get_gateway_usage_summary", spy)
    now = datetime.now(UTC)
    params = {
        "start_date": (now - timedelta(days=3)).isoformat(),
        "end_date": (now + timedelta(days=1)).isoformat(),
        "include_breakdown": "false",
    }
    for _ in range(2):
        assert client.get("/api/v1/cost/summary", params=params).status_code == 200
    assert spy.call_count == 1
    clock.return_value += cost.SUMMARY_CACHE_TTL_SECONDS
    assert client.get("/api/v1/cost/summary", params=params).status_code == 200
    assert spy.call_count == 2


def test_summary_cache_ttl_by_window() -> None:
    from preloop.api.endpoints import cost

    now = datetime(2026, 11, 7, 12, tzinfo=UTC)
    last_month_end = datetime(2026, 11, 1, tzinfo=UTC)
    assert (
        cost.summary_cache_ttl(last_month_end, now)
        == cost.CLOSED_SUMMARY_CACHE_TTL_SECONDS
    )
    assert cost.summary_cache_ttl(now, now) == cost.SUMMARY_CACHE_TTL_SECONDS
    # Inside the grace period a late row may still land: keep the short TTL.
    just_ended = now - cost.CLOSED_WINDOW_GRACE + timedelta(minutes=1)
    assert cost.summary_cache_ttl(just_ended, now) == cost.SUMMARY_CACHE_TTL_SECONDS


def test_cost_revision_bump_expires_closed_windows_on_every_worker(
    client: Any, db_session: Any, test_user: Any, monkeypatch: Any
) -> None:
    """Another worker's import bumps the revision; this worker's cache misses."""
    from preloop.models.crud import crud_account

    spy = Mock(wraps=crud_api_usage.get_gateway_usage_summary)
    monkeypatch.setattr(crud_api_usage, "get_gateway_usage_summary", spy)
    params = {
        "start_date": "2025-03-01T00:00:00Z",
        "end_date": "2025-04-01T00:00:00Z",
        "include_breakdown": "false",
    }
    for _ in range(2):
        assert client.get("/api/v1/cost/summary", params=params).status_code == 200
    assert spy.call_count == 1
    # No in-process forget: only the shared revision changes.
    crud_account.bump_cost_revision(db_session, account_id=test_user.account_id)
    db_session.flush()
    db_session.expire_all()
    assert client.get("/api/v1/cost/summary", params=params).status_code == 200
    assert spy.call_count == 2
    account = crud_account.get(db_session, id=test_user.account_id)
    assert account.meta_data[crud_account.COST_REVISION_KEY] == 1


def test_summary_cache_copies_response_and_never_caches_errors(
    db_session: Any, test_user: Any, monkeypatch: Any
) -> None:
    """An attached comparison cannot poison cache entries; failures can retry."""
    from preloop.api.endpoints import cost
    from preloop.models.crud import crud_account

    kwargs: dict[str, Any] = dict(
        account=crud_account.get(db_session, id=test_user.account_id),
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 1, 31, tzinfo=UTC),
        runtime_principal_id=None,
        exclude_retries=False,
        include_breakdown=False,
        breakdown=None,
    )
    build = Mock(wraps=cost._build_cost_summary)
    monkeypatch.setattr(cost, "_build_cost_summary", build)
    build.side_effect = RuntimeError("temporary failure")
    with pytest.raises(RuntimeError, match="temporary failure"):
        cost._cached_cost_summary(db_session, **kwargs)
    build.side_effect = None
    first = cost._cached_cost_summary(db_session, **kwargs)
    first.estimated_cost = 999
    first.previous = first.model_copy()
    second = cost._cached_cost_summary(db_session, **kwargs)
    assert second.estimated_cost == 0
    assert second.previous is None
    assert build.call_count == 2
    cost._forget_cost_health(str(test_user.account_id))


def test_summary_cache_checks_history_on_hits(client: Any, monkeypatch: Any) -> None:
    """A cached answer must not bypass a changed history entitlement."""
    from fastapi import HTTPException
    from preloop.api.endpoints import cost

    params = {
        "start_date": "2026-01-01T00:00:00Z",
        "end_date": "2026-01-31T00:00:00Z",
        "include_breakdown": "false",
    }
    assert client.get("/api/v1/cost/summary", params=params).status_code == 200
    monkeypatch.setattr(
        cost, "restrict_history_window", Mock(side_effect=HTTPException(403))
    )
    assert client.get("/api/v1/cost/summary", params=params).status_code == 403


def test_summary_cache_coalesces_concurrent_reads(
    db_session: Any, test_user: Any, monkeypatch: Any
) -> None:
    """Two identical misses run one builder, without sleeps or concurrent DB use."""
    from concurrent.futures import Future, ThreadPoolExecutor
    from threading import Event
    from preloop.api.endpoints import cost
    from preloop.models.crud import crud_account

    kwargs: dict[str, Any] = dict(
        account=crud_account.get(db_session, id=test_user.account_id),
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 1, 31, tzinfo=UTC),
        runtime_principal_id=None,
        exclude_retries=False,
        include_breakdown=False,
        breakdown=None,
    )
    expected = cost._build_cost_summary(db_session, **kwargs)
    entered, waiting, release = Event(), Event(), Event()

    class TrackedFuture(Future):
        def result(self, timeout: float | None = None) -> Any:
            waiting.set()
            return super().result(timeout=timeout)

    def build(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return expected

    monkeypatch.setattr(cost, "Future", TrackedFuture)
    monkeypatch.setattr(
        cost,
        "restrict_history_window",
        lambda db, **kw: (kw["start_date"], kw["end_date"]),
    )
    builder = Mock(side_effect=build)
    monkeypatch.setattr(cost, "_build_cost_summary", builder)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(cost._cached_cost_summary, Mock(), **kwargs)
        try:
            assert entered.wait(5)
            second = workers.submit(cost._cached_cost_summary, Mock(), **kwargs)
            assert waiting.wait(5)
        finally:
            release.set()
        assert first.result(5) == second.result(5) == expected
        assert first.result(5) is not second.result(5)
    assert builder.call_count == 1
    cost._forget_cost_health(str(test_user.account_id))


def test_summary_cache_is_bounded_and_account_scoped(
    db_session: Any, test_user: Any, monkeypatch: Any
) -> None:
    """A full cache evicts completed entries and cannot answer another account."""
    from preloop.api.endpoints import cost
    from preloop.models.crud import crud_account

    monkeypatch.setattr(cost, "_SUMMARY_CACHE", {})
    monkeypatch.setattr(cost, "_SUMMARY_CACHE_MAX_ENTRIES", 1)
    kwargs: dict[str, Any] = dict(
        account=crud_account.get(db_session, id=test_user.account_id),
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 1, 31, tzinfo=UTC),
        runtime_principal_id=None,
        exclude_retries=False,
        include_breakdown=False,
        breakdown=None,
    )
    builder = Mock(wraps=cost._build_cost_summary)
    monkeypatch.setattr(cost, "_build_cost_summary", builder)
    cost._cached_cost_summary(db_session, **kwargs)
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other example"}
    )
    cost._cached_cost_summary(db_session, **{**kwargs, "account": other})
    assert builder.call_count == 2
    assert len(cost._SUMMARY_CACHE) == 1
    assert next(iter(cost._SUMMARY_CACHE))[0] == str(other.id)


def test_summary_finishing_after_reprice_is_not_cached(
    db_session: Any, test_user: Any, monkeypatch: Any
) -> None:
    """Invalidating an in-flight entry must not let its old answer repopulate it."""
    from preloop.api.endpoints import cost
    from preloop.models.crud import crud_account

    kwargs: dict[str, Any] = dict(
        account=crud_account.get(db_session, id=test_user.account_id),
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 1, 31, tzinfo=UTC),
        runtime_principal_id=None,
        exclude_retries=False,
        include_breakdown=False,
        breakdown=None,
    )
    real = cost._build_cost_summary

    def reprice_during_build(*args: Any, **kw: Any) -> Any:
        result = real(*args, **kw)
        cost._forget_cost_health(str(test_user.account_id))
        return result

    builder = Mock(side_effect=reprice_during_build)
    monkeypatch.setattr(cost, "_build_cost_summary", builder)
    cost._cached_cost_summary(db_session, **kwargs)
    cost._cached_cost_summary(db_session, **kwargs)
    assert builder.call_count == 2
    assert not any(key[0] == str(test_user.account_id) for key in cost._SUMMARY_CACHE)
