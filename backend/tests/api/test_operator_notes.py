"""The live "note was read" event names the session and the hook that read it."""

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

from preloop.models.crud import (
    crud_account,
    crud_agent_control_command,
    crud_managed_agent,
)
from preloop.models.models.runtime_session import RuntimeSession
from preloop.services import operator_notes


def test_note_delivered_event_scoped_to_account_and_session(db_session):
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Note Event Org", "is_active": True}
    )
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    agent = crud_managed_agent.create_custom_agent(
        db_session, account_id=account.id, display_name="Terminal", commit=True
    )
    session = RuntimeSession(
        id=uuid4(),
        account_id=account.id,
        session_source_type="claude_code",
        session_source_id="tui-1",
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    db_session.flush()
    note_id = operator_notes.new_note_id()
    crud_agent_control_command.create_note(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        runtime_session_id=session.id,
        note_id=note_id,
        body="Use the staging bucket.",
        envelope=operator_notes.build_note_envelope(
            note_id=note_id,
            body="Use the staging bucket.",
            runtime_session_id=str(session.id),
            managed_agent_id=str(agent.id),
            author_user_id=None,
            author_display="Ada",
            author_auth_method="jwt",
            created_at=datetime.now(UTC),
            expires_at=None,
        ),
        author_display="Ada",
        author_auth_method="jwt",
        created_by_user_id=None,
        expires_at=None,
    )

    with patch("preloop.services.account_realtime.emit_account_event") as emit:
        # Another account asking for this session's notes gets nothing.
        assert (
            operator_notes.claim_pending_notes(
                db_session,
                account_id=str(other.id),
                managed_agent_id=str(agent.id),
                runtime_session_id=str(session.id),
                channel=operator_notes.CHANNEL_HOOK,
                event=operator_notes.EVENT_USER_PROMPT_SUBMIT,
            )
            == []
        )
        assert emit.call_count == 0
        claimed = operator_notes.claim_pending_notes(
            db_session,
            account_id=str(account.id),
            managed_agent_id=str(agent.id),
            runtime_session_id=str(session.id),
            channel=operator_notes.CHANNEL_HOOK,
            event=operator_notes.EVENT_USER_PROMPT_SUBMIT,
        )

    assert [n.command_id for n in claimed] == [note_id]
    assert emit.call_count == 1
    event = emit.call_args.args[0]
    assert str(event["account_id"]) == str(account.id)
    assert str(event["runtime_session_id"]) == str(session.id)
    payload = event["payload"]
    assert payload["runtime_session_id"] == str(session.id)
    meta = payload["metadata"]
    assert meta["kind"] == "operator_note"
    assert meta["note_id"] == note_id
    assert meta["hook_event"] == "user_prompt_submit"
    assert meta["delivered_at"]
