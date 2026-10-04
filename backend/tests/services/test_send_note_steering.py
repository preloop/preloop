"""A conductor steers the runs it started from a shell (#1045).

The 2026-09-29 conductor spawned workers with ``claude -p ... &`` and then
could not note them: no lineage (``note_scope_no_lineage``), no way to find
the target id, and no lookup by the Claude session id it could read from
disk. These tests pin each of those gaps closed, end to end through the
same send, scope and delivery code the tool uses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from preloop.models.crud import crud_account, crud_managed_agent
from preloop.models.crud import crud_runtime_session
from preloop.models.models.runtime_session import RuntimeSession
from preloop.services import agent_note_scope, operator_notes
from preloop.services.agent_send_note import send_note_from_agent
from preloop.services.agent_session_lineage import (
    caller_session_ids,
    register_session_start,
    resolve_external_session,
    title_from_prompt,
)
from preloop.services.agent_session_list import list_for_agent

PRINCIPAL_TYPE = "claude_code"
PRINCIPAL_ID = "claude_machine_1045"


@pytest.fixture
def account(db_session):
    return crud_account.create(
        db_session, obj_in={"organization_name": "Conductor Org", "is_active": True}
    )


@pytest.fixture
def other_account(db_session):
    return crud_account.create(
        db_session, obj_in={"organization_name": "Elsewhere", "is_active": True}
    )


@pytest.fixture
def agent(db_session, account):
    return crud_managed_agent.create_custom_agent(
        db_session, account_id=account.id, display_name="Conductor", commit=True
    )


def _start(db, account, external, *, parent=None, prompt=None, cwd="/tmp/w"):
    result = register_session_start(
        db,
        account_id=account.id,
        principal_type=PRINCIPAL_TYPE,
        principal_id=PRINCIPAL_ID,
        principal_name="Claude Code",
        external_session_id=external,
        agent_kind=PRINCIPAL_TYPE,
        cwd=cwd,
        parent_session_id=parent,
        first_prompt=prompt,
    )
    assert result is not None
    return result


@pytest.fixture
def family(db_session, account):
    """A conductor session with two shell-spawned children."""
    parent = _start(db_session, account, "ext-parent", cwd="/work/conductor")
    child_a = _start(
        db_session, account, "ext-child-a", parent=parent.runtime_session_id
    )
    child_b = _start(
        db_session, account, "ext-child-b", parent=parent.runtime_session_id
    )
    return parent, child_a, child_b


def test_session_start_records_parent_on_gateway_key(db_session, account, family):
    parent, child_a, _ = family
    assert child_a.parent_session_id == parent.runtime_session_id
    row = crud_runtime_session.get_by_source(
        db_session,
        account_id=account.id,
        session_source_type=PRINCIPAL_TYPE,
        session_source_id=f"{PRINCIPAL_ID}:ext-child-a",
    )
    assert str(row.id) == child_a.runtime_session_id
    assert row.runtime_principal_name == "claude_code in w"

    again = _start(db_session, account, "ext-child-a", parent=None)
    assert again.runtime_session_id == child_a.runtime_session_id
    assert again.created is False
    assert again.parent_session_id == parent.runtime_session_id


def test_parent_from_another_account_is_ignored(db_session, account, other_account):
    foreign = RuntimeSession(
        account_id=other_account.id,
        session_source_type=PRINCIPAL_TYPE,
        session_source_id="foreign",
        started_at=datetime.now(UTC),
    )
    db_session.add(foreign)
    db_session.flush()
    result = _start(db_session, account, "ext-orphan", parent=str(foreign.id))
    assert result.parent_session_id is None


def test_parent_notes_child_with_default_scope_and_is_author(
    db_session, account, agent, family
):
    parent, child_a, _ = family
    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=agent.id,
        text="Stop after the migration and report.",
        runtime_session_id=child_a.runtime_session_id,
        author_session_ids=[parent.runtime_session_id],
    )
    assert result["ok"] is True, result
    assert result["note"]["author"]["agent_id"] == str(agent.id)
    assert result["note"]["runtime_session_id"] == child_a.runtime_session_id

    claimed = operator_notes.claim_pending_notes(
        db_session,
        account_id=str(account.id),
        managed_agent_id=str(uuid4()),
        runtime_session_id=child_a.runtime_session_id,
        channel=operator_notes.CHANNEL_HOOK,
    )
    assert [note.body for note in claimed] == ["Stop after the migration and report."]
    assert claimed[0].created_by_managed_agent_id == agent.id


def test_without_session_lineage_the_note_is_refused(
    db_session, account, agent, family
):
    _, child_a, child_b = family
    no_caller = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=agent.id,
        text="hello",
        runtime_session_id=child_a.runtime_session_id,
    )
    assert no_caller["error"]["code"] == agent_note_scope.REASON_NO_LINEAGE

    sibling = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=agent.id,
        text="hello",
        runtime_session_id=child_b.runtime_session_id,
        author_session_ids=[child_a.runtime_session_id],
    )
    assert sibling["ok"] is False
    assert sibling["error"]["code"] == agent_note_scope.REASON_OUT_OF_SCOPE


def test_external_session_id_resolves_to_the_same_session(
    db_session, account, agent, family
):
    parent, child_a, _ = family
    matches = resolve_external_session(
        db_session, account_id=account.id, external_session_id="ext-child-a"
    )
    assert [str(m.id) for m in matches] == [child_a.runtime_session_id]

    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=agent.id,
        text="via external id",
        external_session_id="ext-child-a",
        author_session_ids=[parent.runtime_session_id],
        author_principal=(PRINCIPAL_TYPE, PRINCIPAL_ID),
    )
    assert result["ok"] is True, result
    assert result["note"]["runtime_session_id"] == child_a.runtime_session_id


def test_external_session_id_unknown_or_ambiguous_is_refused(
    db_session, account, agent, family
):
    parent, _, _ = family
    unknown = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=agent.id,
        text="x",
        external_session_id="nobody",
        author_session_ids=[parent.runtime_session_id],
    )
    assert unknown["error"]["code"] == "target_not_found"

    db_session.add(
        RuntimeSession(
            account_id=account.id,
            session_source_type="codex",
            session_source_id="other_machine:ext-child-a",
            started_at=datetime.now(UTC),
        )
    )
    db_session.flush()
    ambiguous = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=agent.id,
        text="x",
        external_session_id="ext-child-a",
        author_session_ids=[parent.runtime_session_id],
    )
    assert ambiguous["error"]["code"] == "target_ambiguous"
    assert len(ambiguous["error"]["candidates"]) == 2


def test_exactly_one_target_still_holds(db_session, account, agent, family):
    parent, child_a, _ = family
    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=agent.id,
        text="x",
        runtime_session_id=child_a.runtime_session_id,
        children="all",
        author_session_ids=[parent.runtime_session_id],
    )
    assert result["error"]["code"] == "invalid_target"


def test_children_latest_and_all(db_session, account, agent, family):
    parent, child_a, child_b = family
    latest = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=agent.id,
        text="latest only",
        children="latest",
        author_session_ids=[parent.runtime_session_id],
    )
    assert latest["ok"] is True
    assert latest["note"]["runtime_session_id"] in {
        child_a.runtime_session_id,
        child_b.runtime_session_id,
    }

    every = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=agent.id,
        text="everyone",
        children="all",
        author_session_ids=[parent.runtime_session_id],
    )
    assert every["ok"] is True
    assert {n["runtime_session_id"] for n in every["notes"]} == {
        child_a.runtime_session_id,
        child_b.runtime_session_id,
    }

    none = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=agent.id,
        text="x",
        children="all",
        author_session_ids=[child_a.runtime_session_id],
    )
    assert none["error"]["code"] == "no_live_children"


def test_list_sessions_defaults_to_live_children(db_session, account, agent, family):
    parent, child_a, child_b = family
    ended = _start(db_session, account, "ext-ended", parent=parent.runtime_session_id)
    row = db_session.get(RuntimeSession, ended.runtime_session_id)
    row.ended_at = datetime.now(UTC)
    db_session.flush()

    result = list_for_agent(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        caller_session_ids=[parent.runtime_session_id],
        subject_context={"managed_agent_id": str(agent.id)},
    )
    assert result.get("refused") is None, result
    ids = {r["id"] for r in result["results"]}
    assert ids == {child_a.runtime_session_id, child_b.runtime_session_id}
    first = result["results"][0]
    assert set(first) >= {
        "id",
        "started_at",
        "agent_kind",
        "cwd",
        "parent_session_id",
        "title",
        "is_active_now",
    }
    assert first["parent_session_id"] == parent.runtime_session_id


def test_list_sessions_refusals_match_search_sessions(
    db_session, account, agent, family
):
    parent, child_a, _ = family
    no_identity = list_for_agent(
        db_session,
        account_id=account.id,
        managed_agent_id=None,
        caller_session_ids=[],
        subject_context={},
    )
    assert no_identity["refused"] is True
    assert no_identity["reason"] == "no_agent_identity"
    assert no_identity["results"] == []

    account_wide = list_for_agent(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        caller_session_ids=[child_a.runtime_session_id],
        subject_context={"managed_agent_id": str(agent.id)},
        parent_session_id="any",
    )
    assert account_wide["reason"] == "account_scope_not_granted"

    # The caller's own descendants need no grant.
    own = list_for_agent(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        caller_session_ids=[parent.runtime_session_id],
        subject_context={"managed_agent_id": str(agent.id)},
        parent_session_id=child_a.runtime_session_id,
    )
    assert own.get("refused") is None


def test_title_is_capped_and_kept_once_set(db_session, account):
    long_prompt = "word " * 100
    assert len(title_from_prompt(long_prompt)) == 120
    first = _start(db_session, account, "ext-keep", prompt="first prompt")
    _start(db_session, account, "ext-keep", prompt="second prompt")
    row = db_session.get(RuntimeSession, first.runtime_session_id)
    assert row.title == "first prompt"


def test_caller_session_comes_from_the_mcp_client_header(db_session, account, family):
    parent, _, _ = family
    found = caller_session_ids(
        db_session,
        account_id=account.id,
        principal_type=PRINCIPAL_TYPE,
        principal_id=PRINCIPAL_ID,
        headers={"X-Mcp-Client-Session-Id": "ext-parent"},
    )
    assert [str(s) for s in found] == [parent.runtime_session_id]

    other_principal = caller_session_ids(
        db_session,
        account_id=account.id,
        principal_type=PRINCIPAL_TYPE,
        principal_id="someone_else",
        headers={"X-Mcp-Client-Session-Id": "ext-parent"},
    )
    assert other_principal == []


def test_list_sessions_started_since_filter(db_session, account, agent, family):
    parent, _, _ = family
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    result = list_for_agent(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        caller_session_ids=[parent.runtime_session_id],
        subject_context={},
        started_since=future,
    )
    assert result["results"] == []
    bad = list_for_agent(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        caller_session_ids=[parent.runtime_session_id],
        subject_context={},
        started_since="yesterday",
    )
    assert bad["reason"] == "invalid_request"


def test_session_start_survives_the_gateway_creating_the_row_first(
    db_session, account, monkeypatch
):
    """The losing insert of a gateway/hook race retries onto the gateway's row."""
    from unittest.mock import MagicMock

    from sqlalchemy.exc import IntegrityError

    real_upsert = crud_runtime_session.upsert_by_source
    calls = {"n": 0}

    def racing_upsert(db, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # The gateway's row lands while the hook is between read and insert.
            real_upsert(db, **kwargs)
            raise IntegrityError("insert", {}, Exception("uq_runtime_session"))
        return real_upsert(db, **kwargs)

    rollback = MagicMock()
    monkeypatch.setattr(crud_runtime_session, "upsert_by_source", racing_upsert)
    monkeypatch.setattr(db_session, "rollback", rollback)

    result = _start(db_session, account, "ext-race")

    assert calls["n"] == 2
    rollback.assert_called_once()
    assert result.created is False
    rows = (
        db_session.query(RuntimeSession)
        .filter(RuntimeSession.account_id == account.id)
        .filter(RuntimeSession.session_source_id == f"{PRINCIPAL_ID}:ext-race")
        .all()
    )
    assert [str(r.id) for r in rows] == [result.runtime_session_id]


@pytest.mark.parametrize(
    "text, code",
    [("   ", "empty_body"), ("x" * 5000, "body_too_long")],
    ids=["empty", "too_long"],
)
def test_children_all_refuses_a_bad_body_once_before_fanning_out(
    db_session, account, agent, family, monkeypatch, text, code
):
    from preloop.services import agent_session_lineage

    parent, _, _ = family
    fanned = {"n": 0}
    real_children = agent_session_lineage.live_children

    def counting_children(*args, **kwargs):
        fanned["n"] += 1
        return real_children(*args, **kwargs)

    monkeypatch.setattr(agent_session_lineage, "live_children", counting_children)
    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=agent.id,
        text=text,
        children="all",
        author_session_ids=[parent.runtime_session_id],
    )
    assert result["ok"] is False
    assert result["error"]["code"] == code
    assert "refused" not in result
    assert fanned["n"] == 0


def _list(db, account, agent, parent, **kwargs):
    return list_for_agent(
        db,
        account_id=account.id,
        managed_agent_id=agent.id,
        caller_session_ids=[parent.runtime_session_id],
        subject_context={"managed_agent_id": str(agent.id)},
        **kwargs,
    )


def test_list_sessions_filters_cwd_kind_external_active_and_limit(
    db_session, account, agent, family
):
    parent, child_a, child_b = family
    in_repo = _start(
        db_session,
        account,
        "ext-in-repo",
        parent=parent.runtime_session_id,
        cwd="/work/repo_x/sub",
    )
    ended = _start(db_session, account, "ext-done", parent=parent.runtime_session_id)
    db_session.get(RuntimeSession, ended.runtime_session_id).ended_at = datetime.now(
        UTC
    )
    db_session.flush()

    def ids(result):
        assert result.get("refused") is None, result
        return {r["id"] for r in result["results"]}

    # cwd is a prefix match, and "_" is literal, not a LIKE wildcard.
    assert ids(_list(db_session, account, agent, parent, cwd="/work/repo_x")) == {
        in_repo.runtime_session_id
    }
    assert ids(_list(db_session, account, agent, parent, cwd="/work/repoXx")) == set()

    assert ids(_list(db_session, account, agent, parent, agent_kind="codex")) == set()
    assert (
        len(ids(_list(db_session, account, agent, parent, agent_kind=PRINCIPAL_TYPE)))
        == 3
    )

    assert ids(
        _list(db_session, account, agent, parent, external_session_id="ext-child-b")
    ) == {child_b.runtime_session_id}
    assert (
        ids(_list(db_session, account, agent, parent, external_session_id="ext-nope"))
        == set()
    )

    with_ended = ids(_list(db_session, account, agent, parent, active_only=False))
    assert ended.runtime_session_id in with_ended
    assert ended.runtime_session_id not in ids(
        _list(db_session, account, agent, parent)
    )

    one = _list(db_session, account, agent, parent, limit=1)
    assert len(one["results"]) == 1
    assert one["total"] == 3 and one["truncated"] is True


def test_list_sessions_limit_is_clamped_to_the_schema_maximum(
    db_session, account, agent
):
    from preloop.tools.builtin_defs import LIST_SESSIONS_MAX_LIMIT

    parent = _start(db_session, account, "ext-big-parent")
    for i in range(LIST_SESSIONS_MAX_LIMIT + 2):
        _start(db_session, account, f"ext-big-{i}", parent=parent.runtime_session_id)
    result = _list(db_session, account, agent, parent, limit=10_000)
    assert len(result["results"]) == LIST_SESSIONS_MAX_LIMIT
    assert result["truncated"] is True
