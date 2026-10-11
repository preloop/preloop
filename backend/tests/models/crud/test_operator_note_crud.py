"""Tests for the operator-note half of the agent-control store.

Notes share a table with Agent Control commands, so two things are load
bearing here: a note is never mistaken for a command (the WebSocket queries
filter on ``kind``), and a pending note can be claimed exactly once.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from preloop.models.crud import crud_agent_control_command, crud_managed_agent
from preloop.models.models.runtime_session import RuntimeSession


def _agent(db_session, account_id, name="Note CRUD Agent"):
    return crud_managed_agent.create_custom_agent(
        db_session,
        account_id=account_id,
        display_name=name,
        commit=True,
    )


def _session(db_session, account_id) -> RuntimeSession:
    session = RuntimeSession(
        id=uuid4(),
        account_id=account_id,
        session_source_type="claude_code",
        session_source_id=f"workspace-{uuid4().hex[:6]}",
        session_reference="/repo",
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    db_session.flush()
    return session


def _note(
    db_session,
    account,
    agent,
    *,
    runtime_session_id=None,
    body="Stop and open a PR.",
    expires_at=None,
    created_by_user_id=None,
    created_by_managed_agent_id=None,
    author_display="Ada Lovelace",
    author_auth_method="jwt",
):
    note_id = uuid4().hex[:16]
    return crud_agent_control_command.create_note(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id if agent is not None else None,
        runtime_session_id=runtime_session_id,
        note_id=note_id,
        body=body,
        envelope={"kind": "message", "messageId": note_id},
        author_display=author_display,
        author_auth_method=author_auth_method,
        created_by_user_id=created_by_user_id,
        created_by_managed_agent_id=created_by_managed_agent_id,
        expires_at=expires_at,
    )


def test_create_note_persists_pending_with_author(db_session, create_account) -> None:
    """A note starts pending, with the author denormalised onto the row."""
    account = create_account()
    agent = _agent(db_session, account.id)

    note = _note(db_session, account, agent)

    assert note.kind == "note"
    assert note.status == "pending"
    assert note.body == "Stop and open a PR."
    assert note.author_display == "Ada Lovelace"
    assert note.author_auth_method == "jwt"
    assert note.delivered_at is None
    assert note.delivery_channel is None


def test_notes_never_appear_in_agent_control_command_queries(
    db_session, create_account
) -> None:
    """Notes must not travel down the control WebSocket.

    ``get_undelivered_for_agent`` is what a reconnecting runtime plugin drains,
    so a note leaking into it would be delivered as a command envelope.
    """
    account = create_account()
    agent = _agent(db_session, account.id)
    command_id = str(uuid4())
    crud_agent_control_command.create_command(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        runtime_session_id=None,
        command_id=command_id,
        envelope={"type": "command", "message_id": command_id},
    )
    _note(db_session, account, agent)

    pending = crud_agent_control_command.get_undelivered_for_agent(
        db_session, managed_agent_id=agent.id, now=datetime.now(UTC)
    )
    assert [row.command_id for row in pending] == [command_id]

    recent = crud_agent_control_command.list_recent_for_agent(
        db_session, account_id=account.id, managed_agent_id=agent.id
    )
    assert [row.command_id for row in recent] == [command_id]

    marked = crud_agent_control_command.mark_delivered_many(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        command_ids=[command_id],
        delivered_at=datetime.now(UTC),
    )
    assert marked == 1


def test_list_deliverable_notes_matches_session_and_waiting_notes(
    db_session, create_account
) -> None:
    """A session receives its own notes plus the agent's unaddressed ones."""
    account = create_account()
    agent = _agent(db_session, account.id)
    session = _session(db_session, account.id)
    other_session = _session(db_session, account.id)

    mine = _note(db_session, account, agent, runtime_session_id=session.id)
    waiting = _note(db_session, account, agent, body="Before you start: rebase.")
    _note(db_session, account, agent, runtime_session_id=other_session.id)

    deliverable = crud_agent_control_command.list_deliverable_notes(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        runtime_session_id=session.id,
        now=datetime.now(UTC),
    )

    assert {row.command_id for row in deliverable} == {
        mine.command_id,
        waiting.command_id,
    }


def test_list_deliverable_notes_skips_expired_and_cancelled(
    db_session, create_account
) -> None:
    """Neither an expired nor a withdrawn note is ever a candidate."""
    account = create_account()
    agent = _agent(db_session, account.id)
    session = _session(db_session, account.id)

    _note(
        db_session,
        account,
        agent,
        runtime_session_id=session.id,
        body="Stale advice.",
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    withdrawn = _note(
        db_session, account, agent, runtime_session_id=session.id, body="Never mind."
    )
    crud_agent_control_command.cancel_note(
        db_session,
        account_id=account.id,
        note_id=withdrawn.command_id,
        cancelled_at=datetime.now(UTC),
    )
    live = _note(
        db_session, account, agent, runtime_session_id=session.id, body="Do this."
    )

    deliverable = crud_agent_control_command.list_deliverable_notes(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        runtime_session_id=session.id,
        now=datetime.now(UTC),
    )
    assert [row.command_id for row in deliverable] == [live.command_id]


def test_list_deliverable_notes_is_account_scoped(db_session, create_account) -> None:
    """A session id from another account matches nothing, by construction."""
    account = create_account()
    other = create_account()
    agent = _agent(db_session, account.id)
    session = _session(db_session, account.id)
    _note(db_session, account, agent, runtime_session_id=session.id)

    deliverable = crud_agent_control_command.list_deliverable_notes(
        db_session,
        account_id=other.id,
        managed_agent_id=agent.id,
        runtime_session_id=session.id,
        now=datetime.now(UTC),
    )
    assert deliverable == []


def test_claim_note_succeeds_once(db_session, create_account) -> None:
    """The second claim of one note loses: the guard is in the UPDATE."""
    account = create_account()
    agent = _agent(db_session, account.id)
    session = _session(db_session, account.id)
    note = _note(db_session, account, agent, runtime_session_id=session.id)
    now = datetime.now(UTC)

    assert (
        crud_agent_control_command.claim_note(
            db_session,
            note_id=note.id,
            delivered_at=now,
            delivery_channel="gateway",
            runtime_session_id=session.id,
            turn_index=7,
            commit=True,
        )
        is True
    )
    assert (
        crud_agent_control_command.claim_note(
            db_session,
            note_id=note.id,
            delivered_at=now,
            delivery_channel="gateway",
            commit=True,
        )
        is False
    )

    db_session.refresh(note)
    assert note.status == "delivered"
    assert note.delivered_at is not None
    assert note.delivery_channel == "gateway"
    assert note.delivered_turn_index == 7


def test_cancel_note_leaves_delivered_notes_alone(db_session, create_account) -> None:
    """A delivered note cannot be unsent, and is never deleted."""
    account = create_account()
    agent = _agent(db_session, account.id)
    note = _note(db_session, account, agent)
    crud_agent_control_command.claim_note(
        db_session,
        note_id=note.id,
        delivered_at=datetime.now(UTC),
        delivery_channel="hook",
        commit=True,
    )

    cancelled = crud_agent_control_command.cancel_note(
        db_session,
        account_id=account.id,
        note_id=note.command_id,
        cancelled_at=datetime.now(UTC),
    )
    assert cancelled is not None
    assert cancelled.status == "delivered"
    assert cancelled.cancelled_at is None


def test_count_recent_notes_by_author_scopes_to_author_and_window(
    db_session, create_account, create_user
) -> None:
    """Rate limiting counts one author's notes to one agent in the window."""
    account = create_account()
    author = create_user(account=account)
    other_author = create_user(account=account)
    agent = _agent(db_session, account.id)

    _note(db_session, account, agent, created_by_user_id=author.id)
    _note(db_session, account, agent, created_by_user_id=author.id)
    _note(db_session, account, agent, created_by_user_id=other_author.id)

    since = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None)
    assert (
        crud_agent_control_command.count_recent_notes_by_author(
            db_session,
            account_id=account.id,
            managed_agent_id=agent.id,
            created_by_user_id=author.id,
            since=since,
        )
        == 2
    )
    future = (datetime.now(UTC) + timedelta(minutes=1)).replace(tzinfo=None)
    assert (
        crud_agent_control_command.count_recent_notes_by_author(
            db_session,
            account_id=account.id,
            managed_agent_id=agent.id,
            created_by_user_id=author.id,
            since=future,
        )
        == 0
    )


def test_count_recent_notes_by_author_scopes_to_a_session_without_an_agent(
    db_session, create_account, create_user
) -> None:
    """A credential-backed session still has a rate-limit key."""
    account = create_account()
    author = create_user(account=account)
    session = _session(db_session, account.id)
    other = _session(db_session, account.id)
    _note(
        db_session,
        account,
        None,
        runtime_session_id=session.id,
        created_by_user_id=author.id,
    )
    _note(
        db_session,
        account,
        None,
        runtime_session_id=other.id,
        created_by_user_id=author.id,
    )

    since = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None)
    assert (
        crud_agent_control_command.count_recent_notes_by_author(
            db_session,
            account_id=account.id,
            runtime_session_id=session.id,
            created_by_user_id=author.id,
            since=since,
        )
        == 1
    )


def test_a_note_can_record_an_agent_author_instead_of_a_user(
    db_session, create_account
) -> None:
    """The ``send_note`` tool's author: an agent, with an agent credential."""
    account = create_account()
    agent = _agent(db_session, account.id)
    writer = _agent(db_session, account.id, name="Note Author Agent")

    note = _note(
        db_session,
        account,
        agent,
        created_by_managed_agent_id=writer.id,
        author_display="Note Author Agent (agent)",
        author_auth_method="agent",
    )

    assert note.created_by_user_id is None
    assert note.created_by_managed_agent_id == writer.id
    assert note.author_auth_method == "agent"


def test_count_recent_notes_keys_on_the_agent_author(
    db_session, create_account, create_user
) -> None:
    """An agent author's count is its own, not the account's or a user's."""
    account = create_account()
    agent = _agent(db_session, account.id)
    writer = _agent(db_session, account.id, name="Writer")
    other_writer = _agent(db_session, account.id, name="Other Writer")
    human = create_user(account=account)

    _note(db_session, account, agent, created_by_managed_agent_id=writer.id)
    _note(db_session, account, agent, created_by_managed_agent_id=writer.id)
    _note(db_session, account, agent, created_by_managed_agent_id=other_writer.id)
    _note(db_session, account, agent, created_by_user_id=human.id)

    since = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None)
    assert (
        crud_agent_control_command.count_recent_notes_by_author(
            db_session,
            account_id=account.id,
            managed_agent_id=agent.id,
            created_by_managed_agent_id=writer.id,
            since=since,
        )
        == 2
    )
    assert (
        crud_agent_control_command.count_recent_notes_by_author(
            db_session,
            account_id=account.id,
            managed_agent_id=agent.id,
            created_by_user_id=human.id,
            since=since,
        )
        == 1
    )


def test_count_recent_notes_refuses_to_count_without_an_author(
    db_session, create_account
) -> None:
    """Counting every author's notes is not a rate limit, so it is refused."""
    import pytest

    account = create_account()
    agent = _agent(db_session, account.id)
    since = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None)

    with pytest.raises(ValueError):
        crud_agent_control_command.count_recent_notes_by_author(
            db_session,
            account_id=account.id,
            managed_agent_id=agent.id,
            since=since,
        )


def test_note_summaries_count_notes_and_name_the_newest_author(
    db_session, create_account
) -> None:
    """One query answers "noted, how often, and by whom last" per session."""
    account = create_account()
    agent = _agent(db_session, account.id)
    noted = _session(db_session, account.id)
    quiet = _session(db_session, account.id)

    _note(
        db_session,
        account,
        agent,
        runtime_session_id=noted.id,
        author_display="Ada Lovelace",
        author_auth_method="jwt",
    )
    newest = _note(
        db_session,
        account,
        agent,
        runtime_session_id=noted.id,
        body="Rebase before you push.",
        author_display="Reviewer",
        author_auth_method="agent",
    )
    # Written second but stamped older, so recency is read off created_at and
    # not off insertion order.
    newest.created_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5)
    db_session.flush()

    summaries = crud_agent_control_command.note_summaries_for_sessions(
        db_session,
        account_id=account.id,
        runtime_session_ids=[str(noted.id), str(quiet.id)],
    )

    assert set(summaries) == {str(noted.id)}
    summary = summaries[str(noted.id)]
    assert summary.note_count == 2
    assert summary.latest_author_display == "Reviewer"
    assert summary.latest_author_auth_method == "agent"
    assert summary.latest_note_at is not None


def test_note_summaries_stay_inside_the_account(db_session, create_account) -> None:
    """A session id from another account summarises to nothing."""
    account = create_account()
    other = create_account()
    agent = _agent(db_session, account.id)
    other_agent = _agent(db_session, other.id, name="Other Account Agent")
    mine = _session(db_session, account.id)
    theirs = _session(db_session, other.id)

    _note(db_session, account, agent, runtime_session_id=mine.id)
    _note(db_session, other, other_agent, runtime_session_id=theirs.id)

    summaries = crud_agent_control_command.note_summaries_for_sessions(
        db_session,
        account_id=account.id,
        runtime_session_ids=[str(mine.id), str(theirs.id)],
    )

    assert set(summaries) == {str(mine.id)}


def test_note_summaries_ignore_commands_and_unreadable_ids(
    db_session, create_account
) -> None:
    """Only note rows count, and a synthetic list row is skipped, not fatal."""
    account = create_account()
    agent = _agent(db_session, account.id)
    session = _session(db_session, account.id)
    command_id = str(uuid4())
    crud_agent_control_command.create_command(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        runtime_session_id=session.id,
        command_id=command_id,
        envelope={"type": "command", "message_id": command_id},
    )

    summaries = crud_agent_control_command.note_summaries_for_sessions(
        db_session,
        account_id=account.id,
        runtime_session_ids=[str(session.id), "standalone:claude_code:api"],
    )

    assert summaries == {}

    _note(db_session, account, agent, runtime_session_id=session.id)
    summaries = crud_agent_control_command.note_summaries_for_sessions(
        db_session,
        account_id=account.id,
        runtime_session_ids=[str(session.id), "standalone:claude_code:api"],
    )
    assert summaries[str(session.id)].note_count == 1


def test_note_summaries_ignore_notes_that_never_steered(
    db_session, create_account
) -> None:
    """A withdrawn or undelivered note does not count as steering the session."""
    account = create_account()
    agent = _agent(db_session, account.id)
    only_withdrawn = _session(db_session, account.id)
    mixed = _session(db_session, account.id)

    withdrawn = _note(
        db_session,
        account,
        agent,
        runtime_session_id=only_withdrawn.id,
        author_display="Jane Doe",
    )
    crud_agent_control_command.cancel_note(
        db_session,
        account_id=account.id,
        note_id=withdrawn.command_id,
        cancelled_at=datetime.now(UTC),
    )

    expired = _note(
        db_session,
        account,
        agent,
        runtime_session_id=mixed.id,
        body="Too late.",
        author_display="Jane Doe",
        author_auth_method="jwt",
    )
    expired.status = "expired"
    db_session.flush()
    _note(
        db_session,
        account,
        agent,
        runtime_session_id=mixed.id,
        body="Do this.",
        author_display="Reviewer",
        author_auth_method="agent",
    )

    summaries = crud_agent_control_command.note_summaries_for_sessions(
        db_session,
        account_id=account.id,
        runtime_session_ids=[str(only_withdrawn.id), str(mixed.id)],
    )

    assert str(only_withdrawn.id) not in summaries
    summary = summaries[str(mixed.id)]
    assert summary.note_count == 1
    assert summary.latest_author_display == "Reviewer"
    assert summary.latest_author_auth_method == "agent"


def test_note_summaries_with_no_sessions_reads_nothing(
    db_session, create_account
) -> None:
    """An empty page asks the database nothing."""
    account = create_account()

    assert (
        crud_agent_control_command.note_summaries_for_sessions(
            db_session, account_id=account.id, runtime_session_ids=[]
        )
        == {}
    )


def test_claimed_note_remains_visible_in_session_summary(db_session, create_account):
    account = create_account()
    agent = _agent(db_session, account.id)
    session = _session(db_session, account.id)
    note = _note(db_session, account, agent, runtime_session_id=session.id)
    assert crud_agent_control_command.claim_note(
        db_session,
        note_id=note.id,
        delivered_at=datetime.now(UTC),
        delivery_channel="hook",
        runtime_session_id=session.id,
        defer_delivery=True,
        commit=True,
    )
    summaries = crud_agent_control_command.note_summaries_for_sessions(
        db_session, account_id=account.id, runtime_session_ids=[str(session.id)]
    )
    assert str(session.id) in summaries
    assert summaries[str(session.id)].note_count == 1
