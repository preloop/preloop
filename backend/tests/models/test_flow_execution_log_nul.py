"""Runner log lines with NUL bytes must not lose the batch (#1196).

PostgreSQL rejects U+0000 in text and JSONB. An agent stream that disconnects
mid-frame can emit such a line; before the fix the whole batch failed with
``psycopg.DataError`` and every line in it was lost.
"""

from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_flow_execution_log
from preloop.models.crud.flow_execution_log import MAX_LOG_MESSAGE_CHARS
from tests.models.test_flow_runner_ephemeral import _execution


def _line(text: str, **payload: object) -> dict:
    return {
        "_persistence_id": str(uuid4()),
        "type": "agent_log_line",
        "payload": {"line": text, **payload},
    }


def test_nul_bearing_line_does_not_fail_the_batch(db_session: Session) -> None:
    account = crud_account.create(db_session, obj_in={"organization_name": "Logs"})
    execution_id = str(_execution(db_session, account.id).id)
    batch = [
        (execution_id, _line("starting agent")),
        (execution_id, _line("stream \x00\x01garbage\x00", raw={"k\x00": ["\x00"]})),
        (execution_id, _line("x" * (MAX_LOG_MESSAGE_CHARS + 10))),
        (execution_id, _line("still running")),
    ]

    crud_flow_execution_log.append_logs(db_session, batch)

    logs = crud_flow_execution_log.get_by_execution_id(db_session, execution_id)
    messages = [log.message for log in logs]
    assert len(messages) == 4
    assert "starting agent" in messages and "still running" in messages
    garbled = next(m for m in messages if m.startswith("stream"))
    assert "\x00" not in garbled and "�" in garbled
    long_line = next(m for m in messages if m.startswith("xxx"))
    assert len(long_line) == MAX_LOG_MESSAGE_CHARS
    stored = next(log for log in logs if log.message.startswith("stream"))
    assert "\x00" not in repr(stored.metadata_)


def test_single_append_strips_nul(db_session: Session) -> None:
    account = crud_account.create(db_session, obj_in={"organization_name": "Logs"})
    execution_id = str(_execution(db_session, account.id).id)
    entry = crud_flow_execution_log.append_log(
        db_session, execution_id, {"type": "log", "message": "a\x00b"}
    )
    assert entry.message == "a�b"
