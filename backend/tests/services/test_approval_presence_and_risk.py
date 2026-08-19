"""Unit tests for approval risk classification and local-presence push skip."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from preloop.models.schemas.approval_request import classify_approval_risk
from preloop.services.approval_service import _operator_present_at_machine


def test_classify_approval_risk_read_is_low() -> None:
    assert classify_approval_risk("read", {"path": "README.md"}) == "low"
    assert classify_approval_risk("glob") == "low"
    assert classify_approval_risk("grep") == "low"


def test_classify_approval_risk_unknown_fails_closed() -> None:
    assert classify_approval_risk("mystery_tool") == "danger"
    assert classify_approval_risk(None) == "danger"


def test_classify_approval_risk_bash_rm_is_danger() -> None:
    assert classify_approval_risk("bash", {"command": "rm -rf /tmp/x"}) == "danger"
    assert classify_approval_risk("Write", {"path": "src/app.py"}) == "danger"


def test_operator_present_skips_when_mode_is_remote() -> None:
    """Sidecar heartbeat is not local presence; remote operators still get push."""
    agent = SimpleNamespace(
        control_session_mode="remote",
        last_seen_at=datetime.now(UTC),
    )
    db = MagicMock()
    with patch(
        "preloop.models.crud.crud_managed_agent.get",
        return_value=agent,
    ):
        present = _operator_present_at_machine(
            db, SimpleNamespace(managed_agent_id=uuid4())
        )
    assert present is False


def test_operator_present_when_local_and_recent() -> None:
    agent = SimpleNamespace(
        control_session_mode="local",
        last_seen_at=datetime.now(UTC),
    )
    db = MagicMock()
    with patch(
        "preloop.models.crud.crud_managed_agent.get",
        return_value=agent,
    ):
        present = _operator_present_at_machine(
            db, SimpleNamespace(managed_agent_id=uuid4())
        )
    assert present is True


def test_operator_present_stale_local_heartbeat_is_absent() -> None:
    agent = SimpleNamespace(
        control_session_mode="local",
        last_seen_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    db = MagicMock()
    with patch(
        "preloop.models.crud.crud_managed_agent.get",
        return_value=agent,
    ):
        present = _operator_present_at_machine(
            db, SimpleNamespace(managed_agent_id=uuid4())
        )
    assert present is False
