"""Session discovery preserves identity, hierarchy and account boundaries."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import session_discovery
from preloop.models.crud.runtime_session import crud_runtime_session
from preloop.services.runtime_session_explorer import RuntimeSessionExplorerService


@pytest.fixture
def family(db_session: Session, create_account: Any) -> dict[str, Any]:
    """Seed a parent, running child, finished child and foreign session."""
    account, other = create_account(), create_account()
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = []
    for index in range(4):
        row = models.RuntimeSession(
            account_id=other.id if index == 3 else account.id,
            session_source_type="managed_agent",
            session_source_id=f"example:harness-{index}",
            session_reference=f"harness-{index}",
            runtime_principal_name="Example agent",
            title="Opening purpose" if index == 0 else None,
            cwd="/workspace/catalog",
            started_at=now,
            last_activity_at=now,
            turn_state="your_turn" if index == 2 else "working",
            parent_session_id=rows[0].id if index in (1, 2, 3) else None,
        )
        db_session.add(row)
        db_session.flush()
        rows.append(row)
        for offset, prompt in enumerate(
            ("First orchard request", "Later quasar topic")
        ):
            db_session.add(
                models.RuntimeSessionActivity(
                    account_id=row.account_id,
                    runtime_session_id=row.id,
                    activity_type="transcript_message",
                    summary=prompt,
                    metadata_={"role": "user"},
                    timestamp=now + timedelta(seconds=offset),
                )
            )
    db_session.flush()
    return {"account": account, "rows": rows, "now": now}


@pytest.mark.parametrize(
    "query", ["Opening", "orchard", "/workspace/catalog", "Example agent", "harness-0"]
)
def test_identity_search(
    db_session: Session, family: dict[str, Any], query: str
) -> None:
    """Each identity surface matches independently of content indexing."""
    result = crud_runtime_session.list_account_sessions(
        db_session, account_id=str(family["account"].id), query=query
    )
    assert str(family["rows"][0].id) in {row["id"] for row in result["items"]}
    assert str(family["rows"][3].id) not in {row["id"] for row in result["items"]}


def test_id_prefix_and_literal_wildcards(
    db_session: Session, family: dict[str, Any]
) -> None:
    """Preloop ids match prefixes; SQL wildcard characters stay literal."""
    account_id = str(family["account"].id)
    result = crud_runtime_session.list_account_sessions(
        db_session, account_id=account_id, query=str(family["rows"][0].id)[:8]
    )
    assert len(result["items"]) == 1
    assert not crud_runtime_session.list_account_sessions(
        db_session, account_id=account_id, query="%"
    )["items"]


def test_family_context_is_batched_and_tenant_scoped(
    db_session: Session, family: dict[str, Any]
) -> None:
    """A child match returns its parent and sibling with one SQL statement."""
    statements: list[str] = []

    def capture(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        many: bool,
    ) -> None:
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        ids, truncated = session_discovery.family_ids(
            db_session,
            account_id=str(family["account"].id),
            session_ids=[str(family["rows"][1].id)],
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert set(ids) == {str(row.id) for row in family["rows"][:3]}
    assert not truncated
    assert len(statements) == 1


def test_first_prompt_survives_topic_change(
    db_session: Session, family: dict[str, Any]
) -> None:
    """The second request never replaces the recognizable opening request."""
    ids = [str(row.id) for row in family["rows"]]
    prompts = session_discovery.opening_prompts(
        db_session, account_id=str(family["account"].id), session_ids=ids
    )
    assert set(prompts.values()) == {"First orchard request"}
    assert ids[3] not in prompts


def test_title_admission_is_once_and_preserves_harness_title(
    db_session: Session, family: dict[str, Any]
) -> None:
    """Repeated list reads cannot spend or retitle an unchanged session."""
    ids = [str(row.id) for row in family["rows"]]
    account_id = str(family["account"].id)
    assert set(
        session_discovery.reserve_initial_titles(
            db_session, account_id=account_id, session_ids=ids
        )
    ) == set(ids[1:3])
    assert (
        session_discovery.reserve_initial_titles(
            db_session, account_id=account_id, session_ids=ids
        )
        == []
    )
    assert family["rows"][0].title == "Opening purpose"
    assert not RuntimeSessionExplorerService._session_needs_title({"title": None})


def test_finished_parent_and_old_session_are_not_live(
    db_session: Session, family: dict[str, Any]
) -> None:
    """Turn completion applies to top-level sessions as well as subagents."""
    parent = family["rows"][0]
    parent.turn_state = "your_turn"
    child = family["rows"][1]
    child.started_at = family["now"] - timedelta(days=95)
    child.last_activity_at = family["now"] - timedelta(days=94)
    db_session.flush()
    result = crud_runtime_session.list_account_sessions(
        db_session, account_id=str(family["account"].id)
    )
    assert not any(row["is_active_now"] for row in result["items"])


def test_history_cutoff_does_not_relabel_later_prompt_as_first(
    db_session: Session,
    family: dict[str, Any],
) -> None:
    """An unavailable opening request stays unavailable instead of changing."""
    prompts = session_discovery.opening_prompts(
        db_session,
        account_id=str(family["account"].id),
        session_ids=[str(family["rows"][0].id)],
        since=family["now"] + timedelta(microseconds=1),
    )
    assert prompts == {}


def test_family_truncation_and_cycle_are_explicit(
    db_session: Session,
    family: dict[str, Any],
) -> None:
    """Malformed cycles terminate; a bounded family reports omitted context."""
    family["rows"][0].parent_session_id = family["rows"][1].id
    db_session.flush()
    ids, truncated = session_discovery.family_ids(
        db_session,
        account_id=str(family["account"].id),
        session_ids=[str(family["rows"][0].id)],
        limit=2,
    )
    assert len(ids) == 2
    assert truncated


def test_new_hook_activity_wins_over_older_model_usage(
    db_session: Session,
    family: dict[str, Any],
) -> None:
    """A recent real tool or prompt observation can follow an older model call."""
    row = family["rows"][0]
    db_session.add(
        models.ApiUsage(
            account_id=row.account_id,
            runtime_session_id=row.id,
            endpoint="/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=0.1,
            action_type="model_gateway",
            timestamp=family["now"] - timedelta(hours=1),
        )
    )
    db_session.flush()
    result = crud_runtime_session.list_account_sessions(
        db_session,
        account_id=str(family["account"].id),
        session_ids=[str(row.id)],
    )
    assert result["items"][0]["is_active_now"]
