"""The real async audit service preserves safe upstream failure details."""

from typing import Any, Callable
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from preloop.services.audit import service as audit


@pytest.mark.parametrize(
    ("code", "reason"),
    [("insufficient_scope", "scope not granted"), ("invalid_token", "token expired")],
)
def test_real_service_async_error_contract(monkeypatch, code: str, reason: str) -> None:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        audit.crud_audit_log, "log_action", lambda **row: rows.append(row)
    )
    monkeypatch.setattr(audit, "_broadcast_audit_event", lambda **kwargs: None)
    monkeypatch.setattr(
        audit,
        "schedule_audit_write",
        lambda factory, callback, **kwargs: callback(factory(), **kwargs),
    )
    service = audit.AuditService()
    service.log_tool_call_async(
        db_factory=MagicMock,
        account_id=uuid4(),
        tool_name="read_record",
        tool_args={"token": "synthetic-secret"},
        result="upstream_error",
        error_code=code,
        error_reason=reason,
    )
    assert rows[0]["status"] == "upstream_error"
    assert rows[0]["details"]["error_code"] == code
    assert rows[0]["details"]["error_reason"] == reason
    assert rows[0]["details"]["tool_args"]["token"] != "synthetic-secret"


def test_error_details_are_redacted_before_bounding(monkeypatch) -> None:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        audit.crud_audit_log, "log_action", lambda **row: rows.append(row)
    )
    monkeypatch.setattr(audit, "_broadcast_audit_event", lambda **kwargs: None)
    monkeypatch.setattr(
        audit,
        "schedule_audit_write",
        lambda factory, callback, **kwargs: callback(factory(), **kwargs),
    )
    audit.AuditService().log_tool_call_async(
        db_factory=MagicMock,
        account_id=uuid4(),
        tool_name="read_record",
        tool_args={},
        result="upstream_error",
        error_code="x" * 240,
        error_reason="Bearer synthetic-secret; token=other-secret " + "x" * 300,
    )
    details = rows[0]["details"]
    assert len(details["error_code"]) <= 200
    assert len(details["error_reason"]) <= 200
    assert "synthetic-secret" not in details["error_reason"]
    assert "other-secret" not in details["error_reason"]
    assert "REDACTED" in details["error_reason"]


def test_real_service_persists_error_details(
    monkeypatch, db_session, test_user
) -> None:
    """Exercise the shipped serializer and CRUD against the migrated local DB."""
    monkeypatch.setattr(audit, "_broadcast_audit_event", lambda **kwargs: None)
    monkeypatch.setattr(
        audit,
        "schedule_audit_write",
        lambda factory, callback, **kwargs: callback(factory(), **kwargs),
    )
    audit.AuditService().log_tool_call_async(
        db_factory=lambda: db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        tool_name="read_record",
        tool_args={},
        result="upstream_error",
        error_code="insufficient_scope",
        error_reason="scope not granted",
    )
    rows = audit.crud_audit_log.get_by_account(
        db_session,
        account_id=test_user.account_id,
    )
    assert len(rows) == 1
    assert rows[0].status == "upstream_error"
    assert rows[0].details["error_code"] == "insufficient_scope"
    assert rows[0].details["error_reason"] == "scope not granted"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "reason",
    [
        "X-Api-Key: synthetic-secret",
        "api-key=synthetic-secret",
        "https://upstream.example.com/mcp?api-key=synthetic-secret&mode=read",
        "x_api_key=synthetic-secret",
        "apikey=synthetic-secret",
        "API-KEY: synthetic-secret",
    ],
)
def test_hyphenated_credentials_redacted_before_queue_and_storage(
    monkeypatch, asynchronous: bool, reason: str
) -> None:
    """Upstream header and URL dumps must not put API keys in audit evidence."""
    rows: list[dict[str, Any]] = []
    queued: list[dict[str, Any]] = []
    monkeypatch.setattr(
        audit.crud_audit_log, "log_action", lambda **row: rows.append(row)
    )
    monkeypatch.setattr(audit, "_broadcast_audit_event", lambda **kwargs: None)

    def schedule(
        factory: Callable[[], Any], callback: Callable[..., Any], **kwargs: Any
    ) -> None:
        queued.append(kwargs)
        callback(factory(), **kwargs)

    monkeypatch.setattr(audit, "schedule_audit_write", schedule)
    service = audit.AuditService()
    kwargs = {
        "account_id": uuid4(),
        "tool_name": "read_record",
        "tool_args": {},
        "result": "upstream_error",
        "error_code": reason,
        "error_reason": reason,
    }
    if asynchronous:
        service.log_tool_call_async(db_factory=MagicMock, **kwargs)
        assert "synthetic-secret" not in str(queued)
    else:
        service.log_tool_call(db=MagicMock(), **kwargs)
    details = rows[0]["details"]
    for field in ["error_code", "error_reason"]:
        assert "synthetic-secret" not in details[field]
        assert audit.REDACTED in details[field]
