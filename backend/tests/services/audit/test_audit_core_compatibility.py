"""The audit timeline must open on a core that predates consent filtering."""

import asyncio
import inspect
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.api.endpoints import audit as endpoints


def _call(handler, *args, **kwargs):
    """Call a handler that may run on the heavy-route pool (an async wrapper)."""
    result = handler(*args, **kwargs)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def _grouped_without_consent(db, *, account_id, skip=0, limit=50, **kwargs):
    if "consent_ref" in kwargs:
        raise TypeError("unexpected consent_ref")
    return [], 0


def test_grouped_timeline_opens_when_core_lacks_consent_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISABLE_RBAC", "true")
    monkeypatch.setattr(
        endpoints.crud_audit_log,
        "get_grouped_by_correlation",
        _grouped_without_consent,
    )
    user = type("User", (), {"account_id": uuid4()})()
    result = _call(
        endpoints.list_grouped_audit_logs,
        event_type=None,
        outcome=None,
        tool_name=None,
        start_date=None,
        end_date=None,
        min_cost=None,
        max_cost=None,
        consent_ref=None,
        skip=0,
        limit=50,
        db=object(),
        current_user=user,
    )
    assert result.total == 0
    assert result.groups == []


def test_consent_filter_is_rejected_when_core_cannot_apply_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISABLE_RBAC", "true")
    monkeypatch.setattr(
        endpoints.crud_audit_log,
        "get_grouped_by_correlation",
        _grouped_without_consent,
    )
    user = type("User", (), {"account_id": uuid4()})()
    with pytest.raises(HTTPException) as error:
        _call(
            endpoints.list_grouped_audit_logs,
            event_type=None,
            outcome=None,
            tool_name=None,
            start_date=None,
            end_date=None,
            min_cost=None,
            max_cost=None,
            consent_ref="consent-example",
            skip=0,
            limit=50,
            db=object(),
            current_user=user,
        )
    assert error.value.status_code == 400


def _grouped_without_outcome_match(db, *, account_id, skip=0, limit=50, **kwargs):
    if "outcome_match" in kwargs:
        raise TypeError("unexpected outcome_match")
    _grouped_without_outcome_match.calls.append(kwargs)
    return [], 0


def test_any_of_several_outcomes_is_refused_when_core_cannot_apply_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISABLE_RBAC", "true")
    _grouped_without_outcome_match.calls = []
    monkeypatch.setattr(
        endpoints.crud_audit_log,
        "get_grouped_by_correlation",
        _grouped_without_outcome_match,
    )
    user = type("User", (), {"account_id": uuid4()})()
    kwargs = dict(
        event_type=None,
        tool_name=None,
        start_date=None,
        end_date=None,
        min_cost=None,
        max_cost=None,
        consent_ref=None,
        skip=0,
        limit=50,
        db=object(),
        current_user=user,
    )
    # The default ("all") still works on such a core.
    _call(endpoints.list_grouped_audit_logs, outcome=["failed", "deny"], **kwargs)
    with pytest.raises(HTTPException) as error:
        _call(
            endpoints.list_grouped_audit_logs,
            outcome=["failed", "deny"],
            outcome_match="any",
            **kwargs,
        )
    assert error.value.status_code == 400


def test_overview_falls_back_to_one_read_per_outcome_on_an_older_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISABLE_RBAC", "true")
    _grouped_without_outcome_match.calls = []
    monkeypatch.setattr(
        endpoints.crud_audit_log,
        "get_grouped_by_correlation",
        _grouped_without_outcome_match,
    )
    monkeypatch.setattr(
        endpoints.crud_audit_log, "count_by_account", lambda db, **kwargs: 0
    )
    user = type("User", (), {"account_id": uuid4()})()
    result = _call(
        endpoints.get_audit_overview,
        start_date=None,
        exceptions_limit=12,
        db=object(),
        current_user=user,
    )
    assert result.exceptions.total == 0
    asked = [c.get("outcome_filter") for c in _grouped_without_outcome_match.calls]
    assert ["failed"] in asked
    assert ["deny"] in asked
    assert ["budget_denied"] in asked
    assert ["upstream_error"] not in asked


def _grouped_with_outcome_match(
    db, *, account_id, skip=0, limit=50, outcome_match=None, **kwargs
):
    recorded = {"limit": limit, **kwargs}
    if outcome_match is not None:
        recorded["outcome_match"] = outcome_match
    _grouped_with_outcome_match.calls.append(recorded)
    return [], 0


def test_overview_reads_exceptions_in_one_any_match_on_a_new_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new-core branch is one grouped read, before the submodule bump."""
    monkeypatch.setenv("DISABLE_RBAC", "true")
    _grouped_with_outcome_match.calls = []
    monkeypatch.setattr(
        endpoints.crud_audit_log,
        "get_grouped_by_correlation",
        _grouped_with_outcome_match,
    )
    monkeypatch.setattr(
        endpoints.crud_audit_log, "count_by_account", lambda db, **kwargs: 0
    )
    user = type("User", (), {"account_id": uuid4()})()
    result = _call(
        endpoints.get_audit_overview,
        start_date=None,
        exceptions_limit=12,
        db=object(),
        current_user=user,
    )
    assert result.exceptions.total == 0
    any_match = [
        call
        for call in _grouped_with_outcome_match.calls
        if call.get("outcome_match") == "any"
    ]
    assert len(any_match) == 1
    assert any_match[0]["outcome_filter"] == [
        "failed",
        "upstream_error",
        "deny",
        "budget_denied",
    ]
    assert any_match[0]["limit"] == 12


def test_overview_caps_the_events_total_at_the_audit_page_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold overview stops counting at 10_000 groups (#1592).

    The exact grouped total over a long window is the slow read. Both
    overview reads pass that cap: the trail total, and the exceptions
    read, which asks only for ``exceptions_limit`` groups.
    """
    from datetime import datetime, timezone

    monkeypatch.setenv("DISABLE_RBAC", "true")
    asked: list[dict] = []

    def grouped(
        db,
        *,
        account_id,
        skip=0,
        limit=50,
        count_limit=None,
        outcome_match=None,
        **kwargs,
    ):
        recorded = {"limit": limit, "count_limit": count_limit, **kwargs}
        if outcome_match is not None:
            recorded["outcome_match"] = outcome_match
        asked.append(recorded)
        if kwargs.get("outcome_filter"):
            return [], 4
        # The core reports count_limit + 1 when more groups match.
        return [], 10001

    monkeypatch.setattr(endpoints.crud_audit_log, "get_grouped_by_correlation", grouped)
    monkeypatch.setattr(
        endpoints.crud_audit_log, "count_by_account", lambda db, **kwargs: 0
    )
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    user = type("User", (), {"account_id": uuid4()})()

    result = _call(
        endpoints.get_audit_overview,
        start_date=start,
        exceptions_limit=12,
        db=object(),
        current_user=user,
    )

    assert result.events_total == 10_000
    assert result.events_total_capped is True
    assert result.exceptions.total == 4
    assert result.exceptions.total_capped is False
    totals = [call for call in asked if not call.get("outcome_filter")]
    exceptions = [call for call in asked if call.get("outcome_filter")]
    assert totals == [{"limit": 1, "count_limit": 10_000, "start_date": start}]
    assert len(exceptions) == 1
    assert exceptions[0]["limit"] == 12
    assert exceptions[0]["count_limit"] == 10_000
    assert exceptions[0]["start_date"] == start


def test_overview_keeps_an_older_cores_exact_events_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A core that cannot stop early still reports the exact total."""
    monkeypatch.setenv("DISABLE_RBAC", "true")

    def grouped(db, *, account_id, skip=0, limit=50, **kwargs):
        assert "count_limit" not in kwargs
        return [], 250_000

    monkeypatch.setattr(endpoints.crud_audit_log, "get_grouped_by_correlation", grouped)
    monkeypatch.setattr(
        endpoints.crud_audit_log, "count_by_account", lambda db, **kwargs: 0
    )
    user = type("User", (), {"account_id": uuid4()})()

    result = _call(
        endpoints.get_audit_overview,
        start_date=None,
        exceptions_limit=12,
        db=object(),
        current_user=user,
    )

    assert result.events_total == 250_000
    assert result.events_total_capped is False


def test_legacy_exceptions_cap_the_sum_when_the_core_counts_to_a_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A core with ``count_limit`` but no ``outcome_match`` caps the sum.

    Each outcome is under the cap. Added together they pass it, and the
    merged total is the cap, not the sum (#1592).
    """
    monkeypatch.setenv("DISABLE_RBAC", "true")
    asked: list[dict] = []

    def grouped(db, *, account_id, skip=0, limit=50, count_limit=None, **kwargs):
        asked.append({"count_limit": count_limit, **kwargs})
        return [], 4_000

    monkeypatch.setattr(endpoints.crud_audit_log, "get_grouped_by_correlation", grouped)
    monkeypatch.setattr(
        endpoints.crud_audit_log, "count_by_account", lambda db, **kwargs: 0
    )
    user = type("User", (), {"account_id": uuid4()})()

    result = _call(
        endpoints.get_audit_overview,
        start_date=None,
        exceptions_limit=12,
        db=object(),
        current_user=user,
    )

    assert result.events_total == 4_000
    assert result.events_total_capped is False
    assert result.exceptions.total == 10_000
    assert result.exceptions.total_capped is True
    assert [call["count_limit"] for call in asked] == [10_000] * 4
    assert [call.get("outcome_filter") for call in asked] == [
        None,
        ["failed"],
        ["deny"],
        ["budget_denied"],
    ]
    assert all("outcome_match" not in call for call in asked)


def test_audit_aggregates_use_the_heavy_pool_when_core_has_it() -> None:
    """Timeline, Overview and stats queue on the bounded pool (#1593)."""
    pytest.importorskip("preloop.api.heavy_routes")
    for handler in (
        endpoints.list_grouped_audit_logs,
        endpoints.get_audit_overview,
        endpoints.get_audit_stats,
    ):
        assert getattr(handler, "__heavy_route__", False), handler.__name__


def test_heavy_route_fallback_only_covers_a_missing_module(monkeypatch) -> None:
    """A core without heavy_routes falls back; a broken heavy_routes raises."""
    import builtins
    import importlib

    real_import = builtins.__import__

    def _import(name, *args, **kwargs):
        if name == "preloop.api.heavy_routes":
            raise _error
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _import)
    try:
        _error = ModuleNotFoundError(
            "No module named 'preloop.api.heavy_routes'",
            name="preloop.api.heavy_routes",
        )
        fallback = importlib.reload(endpoints)
        assert not getattr(fallback.get_audit_overview, "__heavy_route__", False)

        _error = ImportError("cannot import name 'release_transaction'")
        with pytest.raises(ImportError):
            importlib.reload(endpoints)

        # A dependency missing inside heavy_routes is re-raised, not hidden.
        _error = ModuleNotFoundError(
            "No module named 'preloop.api.missing_dep'",
            name="preloop.api.missing_dep",
        )
        with pytest.raises(ModuleNotFoundError):
            importlib.reload(endpoints)
    finally:
        monkeypatch.setattr(builtins, "__import__", real_import)
        importlib.reload(endpoints)


def test_overview_counts_tool_calls_in_the_window_and_caches_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool call totals are plain counts over the same window (#1592).

    They used to be two all-time grouped reads, the second one evaluating
    the outcome of the newest 5000 calls. A second load inside the TTL is
    served from the cache without touching the database.
    """
    from datetime import datetime, timezone

    monkeypatch.setenv("DISABLE_RBAC", "true")
    _grouped_with_outcome_match.calls = []
    monkeypatch.setattr(
        endpoints.crud_audit_log,
        "get_grouped_by_correlation",
        _grouped_with_outcome_match,
    )
    counts: list[dict] = []

    def count_by_account(db, **kwargs):
        counts.append(kwargs)
        return 7 if kwargs.get("status") == "failed" else 40

    monkeypatch.setattr(endpoints.crud_audit_log, "count_by_account", count_by_account)
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    user = type("User", (), {"account_id": uuid4()})()
    kwargs = dict(start_date=start, exceptions_limit=12, db=object(), current_user=user)

    result = _call(endpoints.get_audit_overview, **kwargs)

    assert (result.tool_calls_total, result.tool_calls_failed) == (40, 7)
    assert all(c["start_date"] == start for c in counts)
    assert all(c["action"] == "tool_call" for c in counts)
    assert all(
        call.get("start_date") == start for call in _grouped_with_outcome_match.calls
    )
    assert not any(
        call.get("event_type_filter") for call in _grouped_with_outcome_match.calls
    )

    reads = len(counts) + len(_grouped_with_outcome_match.calls)
    assert _call(endpoints.get_audit_overview, **kwargs) == result
    assert len(counts) + len(_grouped_with_outcome_match.calls) == reads

    other = type("User", (), {"account_id": uuid4()})()
    _call(endpoints.get_audit_overview, **{**kwargs, "current_user": other})
    assert len(counts) + len(_grouped_with_outcome_match.calls) > reads


def test_concurrent_overview_misses_share_one_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eight loads of the same Overview at once run the reads once."""
    import threading
    import time

    monkeypatch.setenv("DISABLE_RBAC", "true")
    reads: list[int] = []
    gate = threading.Event()

    def grouped(db, *, account_id, skip=0, limit=50, **kwargs):
        reads.append(1)
        gate.wait(2)
        return [], 0

    monkeypatch.setattr(endpoints.crud_audit_log, "get_grouped_by_correlation", grouped)
    monkeypatch.setattr(
        endpoints.crud_audit_log, "count_by_account", lambda db, **kwargs: 0
    )
    user = type("User", (), {"account_id": uuid4()})()
    barrier = threading.Barrier(8)
    results: list = []

    def load() -> None:
        barrier.wait()
        results.append(
            _call(
                endpoints.get_audit_overview,
                start_date=None,
                exceptions_limit=12,
                db=object(),
                current_user=user,
            )
        )

    threads = [threading.Thread(target=load) for _ in range(8)]
    for thread in threads:
        thread.start()
    time.sleep(0.3)
    gate.set()
    for thread in threads:
        thread.join(5)

    assert len(results) == 8
    concurrent_reads = len(reads)
    reads.clear()
    _call(
        endpoints.get_audit_overview,
        start_date=None,
        exceptions_limit=12,
        db=object(),
        current_user=type("User", (), {"account_id": uuid4()})(),
    )
    # Eight concurrent loads cost what one load costs.
    assert concurrent_reads == len(reads)
    assert not endpoints._overview_inflight


def test_a_failed_overview_is_not_cached_and_leaves_nothing_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISABLE_RBAC", "true")

    def broken(db, *, account_id, skip=0, limit=50, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(endpoints.crud_audit_log, "get_grouped_by_correlation", broken)
    user = type("User", (), {"account_id": uuid4()})()
    with pytest.raises(HTTPException):
        _call(
            endpoints.get_audit_overview,
            start_date=None,
            exceptions_limit=12,
            db=object(),
            current_user=user,
        )
    assert not endpoints._overview_inflight
    assert not endpoints._overview_cache


def test_overview_cache_expires_and_stays_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISABLE_RBAC", "true")
    reads: list[int] = []

    def grouped(db, *, account_id, skip=0, limit=50, **kwargs):
        reads.append(1)
        return [], 0

    monkeypatch.setattr(endpoints.crud_audit_log, "get_grouped_by_correlation", grouped)
    monkeypatch.setattr(
        endpoints.crud_audit_log, "count_by_account", lambda db, **kwargs: 0
    )
    clock = [1000.0]
    monkeypatch.setattr(endpoints.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(endpoints, "_OVERVIEW_CACHE_MAX_ENTRIES", 2)
    users = [type("User", (), {"account_id": uuid4()})() for _ in range(3)]

    def load(user) -> None:
        _call(
            endpoints.get_audit_overview,
            start_date=None,
            exceptions_limit=12,
            db=object(),
            current_user=user,
        )

    load(users[0])
    first = len(reads)
    load(users[0])
    assert len(reads) == first  # cached
    clock[0] += endpoints.OVERVIEW_CACHE_TTL_SECONDS + 1
    load(users[0])
    assert len(reads) == 2 * first  # expired, read again

    load(users[1])
    clock[0] += 1
    load(users[2])  # evicts the oldest entry, users[0]
    assert len(endpoints._overview_cache) == 2
    reads.clear()
    load(users[2])
    assert not reads
    load(users[0])
    assert reads


def test_after_a_failed_leader_the_waiters_share_one_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed build hands the flight to one waiter, not to all of them."""
    import threading
    import time

    monkeypatch.setenv("DISABLE_RBAC", "true")
    builds: list[int] = []
    gate = threading.Event()

    def build() -> str:
        builds.append(1)
        if len(builds) == 1:
            gate.wait(2)
            raise RuntimeError("first read fails")
        time.sleep(0.2)
        return "figures"

    key = ("acc", None, 12)
    results: list = []
    errors: list = []

    def load() -> None:
        try:
            results.append(endpoints._single_flight_overview(key, build))
        except RuntimeError as error:
            errors.append(error)

    leader = threading.Thread(target=load)
    leader.start()
    time.sleep(0.1)
    waiters = [threading.Thread(target=load) for _ in range(6)]
    for thread in waiters:
        thread.start()
    time.sleep(0.2)
    gate.set()
    for thread in [leader, *waiters]:
        thread.join(5)

    assert len(errors) == 1
    assert results == ["figures"] * 6
    assert len(builds) == 2
    assert not endpoints._overview_inflight


def test_grouped_total_is_capped_when_asked_and_the_core_can(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``count_limit`` caps the count (#1592); a capped total says so."""
    monkeypatch.setenv("DISABLE_RBAC", "true")
    asked: list[dict] = []

    def grouped(db, *, account_id, skip=0, limit=50, count_limit=None, **kwargs):
        asked.append({"count_limit": count_limit})
        return [], 101 if count_limit == 100 else 7

    monkeypatch.setattr(endpoints.crud_audit_log, "get_grouped_by_correlation", grouped)
    user = type("User", (), {"account_id": uuid4()})()
    kwargs = dict(
        event_type=None,
        outcome=None,
        tool_name=None,
        start_date=None,
        end_date=None,
        min_cost=None,
        max_cost=None,
        consent_ref=None,
        skip=0,
        limit=50,
        db=object(),
        current_user=user,
    )

    capped = _call(endpoints.list_grouped_audit_logs, count_limit=100, **kwargs)
    exact = _call(endpoints.list_grouped_audit_logs, **kwargs)

    assert (capped.total, capped.total_capped) == (100, True)
    assert (exact.total, exact.total_capped) == (7, False)
    assert asked == [{"count_limit": 100}, {"count_limit": None}]


def test_count_limit_is_dropped_on_a_core_that_counts_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISABLE_RBAC", "true")
    monkeypatch.setattr(
        endpoints.crud_audit_log,
        "get_grouped_by_correlation",
        _grouped_without_outcome_match,
    )
    _grouped_without_outcome_match.calls = []
    user = type("User", (), {"account_id": uuid4()})()
    result = _call(
        endpoints.list_grouped_audit_logs,
        event_type=None,
        outcome=None,
        tool_name=None,
        start_date=None,
        end_date=None,
        min_cost=None,
        max_cost=None,
        consent_ref=None,
        skip=0,
        limit=50,
        count_limit=100,
        db=object(),
        current_user=user,
    )
    assert result.total_capped is False
    assert "count_limit" not in _grouped_without_outcome_match.calls[0]


def test_an_older_core_total_above_count_limit_stays_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISABLE_RBAC", "true")

    def exact(db, *, account_id, skip=0, limit=50, **kwargs):
        assert "count_limit" not in kwargs
        return [], 50

    monkeypatch.setattr(endpoints.crud_audit_log, "get_grouped_by_correlation", exact)
    user = type("User", (), {"account_id": uuid4()})()
    result = _call(
        endpoints.list_grouped_audit_logs,
        event_type=None,
        outcome=None,
        tool_name=None,
        start_date=None,
        end_date=None,
        min_cost=None,
        max_cost=None,
        consent_ref=None,
        skip=0,
        limit=50,
        count_limit=10,
        db=object(),
        current_user=user,
    )
    assert (result.total, result.total_capped) == (50, False)
