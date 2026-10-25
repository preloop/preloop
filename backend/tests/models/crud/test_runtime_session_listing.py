"""Runtime session listing preserves attribution while bounding DB results."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.runtime_session import (
    _latest_gateway_usage_for_sessions,
    crud_runtime_session,
)


@pytest.fixture
def session_listing_data(db_session: Session, create_account: Any) -> dict[str, Any]:
    account = create_account()
    other_account = create_account()
    now = datetime.now(UTC).replace(tzinfo=None)
    flow = models.Flow(
        account_id=account.id,
        name="Example flow",
        prompt_template="Example",
        agent_config={},
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(flow_id=flow.id)
    db_session.add(execution)
    db_session.flush()
    sessions = {}
    for name in ("direct", "legacy", "empty", "other"):
        session = models.RuntimeSession(
            account_id=other_account.id if name == "other" else account.id,
            session_source_type="flow_execution" if name == "legacy" else "custom",
            session_source_id=str(execution.id) if name == "legacy" else name,
            started_at=now - timedelta(hours=2),
            last_activity_at=now - timedelta(hours=1),
        )
        db_session.add(session)
        sessions[name] = session
    db_session.flush()

    def usage(name: str | None, **kwargs: Any) -> models.ApiUsage:
        row = models.ApiUsage(
            account_id=kwargs.pop("account_id", account.id),
            runtime_session_id=sessions[name].id if name else None,
            endpoint="/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=0.1,
            action_type="model_gateway",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            estimated_cost=0.01,
            timestamp=kwargs.pop("timestamp", now - timedelta(minutes=5)),
            **kwargs,
        )
        db_session.add(row)
        return row

    usage("direct", model_alias="z-old", timestamp=now - timedelta(minutes=10))
    usage("direct", model_alias="a-latest", cache_read_tokens=4)
    usage(None, flow_execution_id=execution.id, flow_id=flow.id)
    # Both attribution paths match: this row must only count once.
    usage("legacy", flow_execution_id=execution.id, flow_id=flow.id)
    # Distinct session attribution retains the historical OR-join semantics.
    usage(
        "direct",
        flow_execution_id=execution.id,
        flow_id=flow.id,
        timestamp=now - timedelta(minutes=7),
    )
    usage("direct", meta_data={"purpose": "session_title"})
    usage("direct", meta_data={"purpose": "replay_validation"})
    usage("direct", timestamp=now - timedelta(days=5))
    usage("other", account_id=other_account.id)
    db_session.flush()
    return {
        "account": account,
        "other_account": other_account,
        "now": now,
        "sessions": sessions,
        "usage": usage,
    }


def test_listing_preserves_legacy_attribution_and_pagination(
    db_session: Session, session_listing_data: dict[str, Any]
) -> None:
    data = session_listing_data
    kwargs = {
        "account_id": str(data["account"].id),
        "start_date": data["now"] - timedelta(days=1),
        "end_date": data["now"],
    }
    result = crud_runtime_session.list_account_sessions(db_session, **kwargs)
    items = {item["id"]: item for item in result["items"]}
    assert result["total"] == 3
    direct = items[str(data["sessions"]["direct"].id)]
    legacy = items[str(data["sessions"]["legacy"].id)]
    empty = items[str(data["sessions"]["empty"].id)]
    assert direct["total_requests"] == legacy["total_requests"] == 3
    assert direct["total_tokens"] == legacy["total_tokens"] == 45
    assert direct["cache_read_tokens"] == 4
    assert direct["latest_model_alias"] == "a-latest"
    assert empty["total_requests"] == 0
    page = crud_runtime_session.list_account_sessions(
        db_session, **kwargs, limit=1, offset=2
    )
    assert page["total"] == 3
    assert page["items"][0]["id"] == empty["id"]


def test_listing_does_not_aggregate_usage_owned_by_another_account(
    db_session: Session, session_listing_data: dict[str, Any]
) -> None:
    data = session_listing_data
    data["usage"]("direct", account_id=data["other_account"].id)
    db_session.flush()
    result = crud_runtime_session.list_account_sessions(
        db_session,
        account_id=str(data["account"].id),
        start_date=data["now"] - timedelta(days=1),
        end_date=data["now"],
    )
    direct = next(
        item for item in result["items"] if item["session_source_id"] == "direct"
    )
    assert direct["total_requests"] == 3


def test_latest_lookup_transfers_only_one_row_per_session(
    db_session: Session, session_listing_data: dict[str, Any]
) -> None:
    data = session_listing_data
    timestamp = data["now"] - timedelta(seconds=1)
    for _ in range(50):
        data["usage"]("direct", timestamp=timestamp, model_alias="tied")
    data["usage"](
        "direct",
        id=UUID(int=(1 << 128) - 1),
        timestamp=timestamp,
        model_alias="tie-winner",
    )
    data["usage"](
        "direct",
        timestamp=data["now"],
        model_alias="internal",
        meta_data={"purpose": "session_title"},
    )
    db_session.flush()
    fetched_row_counts = []

    def record_rows(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        fetched_row_counts.append(cursor.rowcount)

    event.listen(db_session.connection(), "after_cursor_execute", record_rows)
    try:
        latest = _latest_gateway_usage_for_sessions(
            db_session,
            account_id=str(data["account"].id),
            runtime_session_ids=[
                str(data["sessions"][name].id) for name in ("direct", "legacy")
            ],
        )
    finally:
        event.remove(db_session.connection(), "after_cursor_execute", record_rows)
    assert len(latest) == 2
    assert fetched_row_counts == [2]

    assert latest[str(data["sessions"]["direct"].id)].model_alias == "tie-winner"


def test_minimum_requests_is_account_scoped(
    db_session: Session, session_listing_data: dict[str, Any]
) -> None:
    data = session_listing_data
    data["usage"]("empty", account_id=data["other_account"].id)
    db_session.flush()
    result = crud_runtime_session.list_account_sessions(
        db_session,
        account_id=str(data["account"].id),
        min_requests=1,
    )
    assert {item["session_source_id"] for item in result["items"]} == {
        data["sessions"]["direct"].session_source_id,
        data["sessions"]["legacy"].session_source_id,
    }


def test_listing_ranks_pages_before_aggregating_usage(
    db_session: Session, session_listing_data: dict[str, Any]
) -> None:
    """Each page is picked on last request, then aggregated in full (#1584).

    Only the sessions on the requested page have their usage summed, so the
    order must come from a separate ranking that still sees every attribution
    path (direct and legacy) and the fallback to session activity.
    """
    data = session_listing_data
    kwargs = {
        "account_id": str(data["account"].id),
        "start_date": data["now"] - timedelta(days=1),
        "end_date": data["now"],
    }
    # Make the legacy session the most recent one through its legacy path.
    data["usage"](
        None,
        flow_execution_id=UUID(data["sessions"]["legacy"].session_source_id),
        timestamp=data["now"] - timedelta(minutes=1),
    )
    # Newer than every agent row, and still inside the window. If the narrow
    # ranking forgot to exclude internal usage, direct would take page one.
    data["usage"](
        "direct",
        meta_data={"purpose": "session_title"},
        timestamp=data["now"] - timedelta(seconds=30),
    )
    db_session.flush()

    pages = [
        crud_runtime_session.list_account_sessions(
            db_session, **kwargs, limit=1, offset=offset
        )
        for offset in range(4)
    ]

    assert [page["total"] for page in pages] == [3, 3, 3, 3]
    assert [
        page["items"][0]["session_source_id"] if page["items"] else None
        for page in pages
    ] == [
        data["sessions"]["legacy"].session_source_id,
        "direct",
        "empty",
        None,
    ]
    assert pages[0]["items"][0]["total_requests"] == 4
    assert pages[1]["items"][0]["total_requests"] == 3
    assert pages[2]["items"][0]["total_requests"] == 0


def test_listing_pages_one_model(
    db_session: Session, session_listing_data: dict[str, Any]
) -> None:
    """The model detail page ranks only that model's usage (#1584)."""
    data = session_listing_data
    listed = models.AIModel(
        account_id=data["account"].id,
        name="Listed",
        provider_name="openai",
        model_identifier="listed-model",
    )
    other = models.AIModel(
        account_id=data["account"].id,
        name="Other",
        provider_name="openai",
        model_identifier="other-model",
    )
    db_session.add_all([listed, other])
    db_session.flush()
    data["usage"](
        "direct",
        ai_model_id=listed.id,
        timestamp=data["now"] - timedelta(minutes=2),
    )
    data["usage"](
        "legacy",
        ai_model_id=other.id,
        timestamp=data["now"] - timedelta(seconds=20),
    )
    db_session.flush()

    page = crud_runtime_session.list_account_sessions(
        db_session,
        account_id=str(data["account"].id),
        start_date=data["now"] - timedelta(days=1),
        end_date=data["now"],
        ai_model_id=str(listed.id),
        limit=1,
    )
    assert page["total"] == 1
    assert page["items"][0]["session_source_id"] == "direct"


def test_model_page_past_the_end_does_not_invent_a_total(
    db_session: Session, session_listing_data: dict[str, Any]
) -> None:
    """An offset past the last session is an empty page, not ``total == offset``."""
    data = session_listing_data
    listed = models.AIModel(
        account_id=data["account"].id,
        name="Listed",
        provider_name="openai",
        model_identifier="listed-model-past-end",
    )
    db_session.add(listed)
    db_session.flush()
    data["usage"](
        "direct",
        ai_model_id=listed.id,
        timestamp=data["now"] - timedelta(minutes=2),
    )
    db_session.flush()

    page = crud_runtime_session.list_account_sessions(
        db_session,
        account_id=str(data["account"].id),
        start_date=data["now"] - timedelta(days=1),
        end_date=data["now"],
        ai_model_id=str(listed.id),
        limit=10,
        offset=20,
    )
    assert page["items"] == []
    assert page["has_more"] is False
    assert page["total"] is None


def test_recent_window_ranking_matches_the_full_history(
    db_session: Session,
    session_listing_data: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ranking from recent usage first must return the full-history page (#1584).

    The shapes that can fool a recent window: a session active just now whose
    last request is days old (its key is that old request, not the activity),
    a request-less session that started recently, and pages that reach past
    the window so the ranking has to fall back.
    """
    from preloop.models.crud import runtime_session as runtime_session_crud

    data = session_listing_data
    now = data["now"]
    account = data["account"]

    def session(name: str, *, started: timedelta, active: timedelta | None):
        row = models.RuntimeSession(
            account_id=account.id,
            session_source_type="custom",
            session_source_id=name,
            started_at=now - started,
            last_activity_at=None if active is None else now - active,
        )
        db_session.add(row)
        db_session.flush()
        data["sessions"][name] = row
        return row

    session("active-old-request", started=timedelta(days=9), active=timedelta(0))
    data["usage"]("active-old-request", timestamp=now - timedelta(days=3))
    session("fresh-no-usage", started=timedelta(minutes=1), active=None)
    session("old-no-usage", started=timedelta(days=20), active=None)
    session("two-days", started=timedelta(days=4), active=timedelta(days=2))
    data["usage"]("two-days", timestamp=now - timedelta(days=2))
    session("ten-days", started=timedelta(days=12), active=timedelta(days=10))
    data["usage"]("ten-days", timestamp=now - timedelta(days=10))
    db_session.flush()

    # The legacy (flow execution) and direct sessions of the fixture are in
    # the account too; a model filter and a date range are separate cases.
    model = models.AIModel(
        account_id=account.id,
        name="Ranked",
        provider_name="openai",
        model_identifier="ranked-model",
    )
    db_session.add(model)
    db_session.flush()
    data["usage"]("two-days", ai_model_id=model.id, timestamp=now - timedelta(days=2))
    data["usage"]("legacy", ai_model_id=model.id, timestamp=now - timedelta(hours=3))
    data["usage"]("ten-days", ai_model_id=model.id, timestamp=now - timedelta(days=10))
    db_session.flush()
    filters = {
        "account": {},
        "model": {"ai_model_id": str(model.id)},
        "range": {
            "start_date": now - timedelta(days=20),
            "end_date": now + timedelta(minutes=1),
        },
    }

    full_ranking = runtime_session_crud.CRUDRuntimeSession._rank_page_from_all_usage
    full_calls: list[tuple[int, int]] = []

    def counted_full(self, db, *, session_query, link_filters, limit, offset):
        full_calls.append((limit, offset))
        return full_ranking(
            self,
            db,
            session_query=session_query,
            link_filters=link_filters,
            limit=limit,
            offset=offset,
        )

    monkeypatch.setattr(
        runtime_session_crud.CRUDRuntimeSession,
        "_rank_page_from_all_usage",
        counted_full,
    )

    def pages_for(**kwargs: Any) -> dict[tuple[int, int], list[str]]:
        return {
            (limit, offset): [
                item["session_source_id"]
                for item in crud_runtime_session.list_account_sessions(
                    db_session,
                    account_id=str(account.id),
                    limit=limit,
                    offset=offset,
                    **kwargs,
                )["items"]
            ]
            for limit in (1, 2, 3, 20)
            for offset in (0, 1, 2, 4, 6)
        }

    windows = runtime_session_crud._RANKING_WINDOWS
    for name, kwargs in filters.items():
        monkeypatch.setattr(runtime_session_crud, "_RANKING_WINDOWS", windows)
        full_calls.clear()
        windowed = pages_for(**kwargs)
        fell_back = list(full_calls)
        monkeypatch.setattr(runtime_session_crud, "_RANKING_WINDOWS", ())
        assert pages_for(**kwargs) == windowed, name
        if name == "account":
            # The recent windows must actually answer the shallow pages.
            assert len(fell_back) < 20
            assert (1, 0) not in fell_back
    monkeypatch.setattr(runtime_session_crud, "_RANKING_WINDOWS", windows)
    windowed = pages_for()
    # The recently active session ranks by its 3-day-old request.
    assert windowed[(20, 0)].index("two-days") < windowed[(20, 0)].index(
        "active-old-request"
    )
    assert windowed[(1, 0)] == ["fresh-no-usage"]


def test_min_requests_counts_each_sessions_own_usage(
    db_session: Session, session_listing_data: dict[str, Any]
) -> None:
    """The filter is correlated per session and keeps its old semantics."""
    data = session_listing_data
    kwargs = {"account_id": str(data["account"].id)}
    ids = {name: str(row.id) for name, row in data["sessions"].items()}

    at_least_one = crud_runtime_session.list_account_sessions(
        db_session, **kwargs, min_requests=1
    )
    assert {item["id"] for item in at_least_one["items"]} == {
        ids["direct"],
        ids["legacy"],
    }
    assert at_least_one["total"] == 2

    # "direct" carries six gateway rows of its own, "legacy" one.
    at_least_three = crud_runtime_session.list_account_sessions(
        db_session, **kwargs, min_requests=3
    )
    assert [item["id"] for item in at_least_three["items"]] == [ids["direct"]]
    assert at_least_three["total"] == 1


def test_page_without_flow_sessions_keeps_direct_totals(
    db_session: Session, session_listing_data: dict[str, Any]
) -> None:
    """A page with no legacy flow session skips that branch, same numbers."""
    data = session_listing_data
    result = crud_runtime_session.list_account_sessions(
        db_session,
        account_id=str(data["account"].id),
        session_source_type="custom",
        start_date=data["now"] - timedelta(days=1),
        end_date=data["now"],
    )
    direct = next(
        item for item in result["items"] if item["session_source_id"] == "direct"
    )
    assert direct["total_requests"] == 3
    assert direct["total_tokens"] == 45


def test_legacy_session_with_a_non_uuid_source_is_ignored_safely(
    db_session: Session, session_listing_data: dict[str, Any]
) -> None:
    data = session_listing_data
    broken = models.RuntimeSession(
        account_id=data["account"].id,
        session_source_type="flow_execution",
        session_source_id="not-a-uuid",
        started_at=data["now"] - timedelta(hours=1),
        last_activity_at=data["now"] - timedelta(minutes=30),
    )
    db_session.add(broken)
    db_session.flush()
    result = crud_runtime_session.list_account_sessions(
        db_session,
        account_id=str(data["account"].id),
        start_date=data["now"] - timedelta(days=1),
        end_date=data["now"],
    )
    items = {item["id"]: item for item in result["items"]}
    assert items[str(broken.id)]["total_requests"] == 0
    assert items[str(data["sessions"]["legacy"].id)]["total_requests"] == 3


def test_listing_carries_the_hook_reported_cwd(
    db_session: Session, session_listing_data: dict[str, Any]
) -> None:
    """The Talk status line reads the session's cwd from the agent detail list."""
    from preloop.services.runtime_session_explorer import (
        RuntimeSessionExplorerService,
    )

    data = session_listing_data
    direct = data["sessions"]["direct"]
    direct.cwd = "/work/example"
    direct.runtime_principal_type = "managed_agent"
    direct.runtime_principal_id = "agent-1"
    db_session.flush()
    result = crud_runtime_session.list_account_sessions(
        db_session,
        account_id=str(data["account"].id),
        runtime_principal_type="managed_agent",
        runtime_principal_id="agent-1",
        min_requests=1,
        status="all",
    )
    assert [item["cwd"] for item in result["items"]] == ["/work/example"]
    schema = RuntimeSessionExplorerService._summary_row_to_schema(result["items"][0])
    assert schema.cwd == "/work/example"
    unscoped = crud_runtime_session.list_account_sessions(
        db_session,
        account_id=str(data["account"].id),
        start_date=data["now"] - timedelta(days=1),
        end_date=data["now"],
    )
    by_id = {item["id"]: item for item in unscoped["items"]}
    assert by_id[str(data["sessions"]["direct"].id)]["cwd"] == "/work/example"
    assert by_id[str(data["sessions"]["empty"].id)]["cwd"] is None
