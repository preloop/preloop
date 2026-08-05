"""Persistence-layer scrubbing tests for issue #173.

``append_log`` is the last gate before a log row is written, so it scrubs even
when the producer did not. Both implementations are covered because both are
still called: ``CRUDFlowExecution.append_log`` and the newer
``CRUDFlowExecutionLog.append_log``.
"""

from unittest.mock import MagicMock

import pytest

from preloop.models.crud.flow_execution import CRUDFlowExecution
from preloop.models.crud.flow_execution_log import CRUDFlowExecutionLog

PAT = "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
LEAK = f"origin\thttps://{PAT}@github.com/acme/private.git (fetch)"


@pytest.fixture(params=[CRUDFlowExecution, CRUDFlowExecutionLog])
def crud(request):
    return request.param()


@pytest.fixture
def db():
    session = MagicMock()
    session.added = []
    session.add.side_effect = session.added.append
    return session


def _append(crud, db, log_data):
    crud.append_log(db, "execution-1", log_data)
    assert len(db.added) == 1
    return db.added[0]


class TestAppendLogScrubbing:
    def test_message_is_scrubbed(self, crud, db):
        row = _append(crud, db, {"message": LEAK})
        assert PAT not in row.message
        assert "[REDACTED]" in row.message

    def test_nats_payload_line_is_scrubbed(self, crud, db):
        row = _append(crud, db, {"type": "agent_log_line", "payload": {"line": LEAK}})
        assert PAT not in row.message

    def test_metadata_is_scrubbed(self, crud, db):
        row = _append(crud, db, {"message": "ok", "payload": {"line": LEAK}})
        assert PAT not in str(row.metadata_)

    def test_nested_metadata_is_scrubbed(self, crud, db):
        row = _append(
            crud,
            db,
            {"message": "ok", "metadata": {"remotes": [LEAK], "count": 1}},
        )
        assert PAT not in str(row.metadata_)
        assert row.metadata_["count"] == 1

    def test_clean_message_is_unchanged(self, crud, db):
        row = _append(crud, db, {"message": "Cloning into 'repo'..."})
        assert row.message == "Cloning into 'repo'..."

    def test_log_type_is_preserved(self, crud, db):
        row = _append(crud, db, {"type": "stderr", "message": LEAK})
        assert row.log_type == "stderr"

    def test_missing_message_stays_none(self, crud, db):
        row = _append(crud, db, {"type": "heartbeat"})
        assert row.message is None
