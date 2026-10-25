"""Attribution of gateway calls that name no runtime session."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.models.crud import crud_account
from preloop.models.crud.runtime_session import crud_runtime_session
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.runtime_session import RuntimeSession
from preloop.services.model_gateway_events import ModelGatewayEventEmitter

PRINCIPAL_TYPE = "claude_code"
PRINCIPAL_ID = "agent-principal"


def _account(db: Session) -> Any:
    return crud_account.create(
        db, obj_in={"organization_name": f"Org {uuid4()}", "is_active": True}
    )


def _session(
    db: Session,
    account_id: Any,
    *,
    suffix: str,
    last_activity_at: datetime,
    ended_at: datetime | None = None,
    per_run: bool = False,
) -> RuntimeSession:
    # Two id shapes exist: command-history sessions key the source id as
    # ``<principal>-<suffix>``, per-run sessions key the principal id as
    # ``<principal>:<run-id>``.
    row = RuntimeSession(
        id=uuid4(),
        account_id=account_id,
        session_source_type=PRINCIPAL_TYPE,
        session_source_id=(
            f"{PRINCIPAL_ID}:{suffix}" if per_run else f"{PRINCIPAL_ID}-{suffix}"
        ),
        session_reference=suffix,
        runtime_principal_type=PRINCIPAL_TYPE,
        runtime_principal_id=f"{PRINCIPAL_ID}:{suffix}" if per_run else PRINCIPAL_ID,
        started_at=last_activity_at,
        last_activity_at=last_activity_at,
        ended_at=ended_at,
    )
    db.add(row)
    db.commit()
    return row


def _unscoped_usage(account_id: Any) -> ApiUsage:
    return ApiUsage(
        id=uuid4(),
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        account_id=account_id,
        runtime_session_id=None,
        flow_execution_id=None,
        model_alias="openai/gpt-5",
        provider_name="openai",
        runtime_principal_type=PRINCIPAL_TYPE,
        runtime_principal_id=PRINCIPAL_ID,
        timestamp=datetime.now(UTC),
        meta_data={},
    )


def _emit(db: Session, usage: ApiUsage) -> list[str]:
    """Run the emitter and return the session ids it wrote activity to."""
    with patch(
        "preloop.services.model_gateway_events.crud_runtime_session_activity"
        ".log_model_gateway_call"
    ) as log_call:
        ModelGatewayEventEmitter(db).emit_for_usage(
            usage=usage,
            request_payload={"input": "prompt"},
            response_payload={"output_text": "answer"},
        )
    return [str(call.kwargs["runtime_session_id"]) for call in log_call.call_args_list]


def test_unscoped_gateway_call_attributed_with_single_active_session(
    db_session: Session,
) -> None:
    account = _account(db_session)
    now = datetime.now(UTC)
    # An old session that went quiet long ago does not make the principal
    # concurrent.
    _session(
        db_session, account.id, suffix="old", last_activity_at=now - timedelta(days=2)
    )
    live = _session(db_session, account.id, suffix="live", last_activity_at=now)

    assert _emit(db_session, _unscoped_usage(account.id)) == [str(live.id)]


def test_unscoped_gateway_call_not_attributed_with_two_active_sessions(
    db_session: Session, caplog: Any
) -> None:
    account = _account(db_session)
    now = datetime.now(UTC)
    _session(db_session, account.id, suffix="a", last_activity_at=now)
    _session(db_session, account.id, suffix="b", last_activity_at=now)

    with caplog.at_level(logging.DEBUG, logger="preloop.services.model_gateway_events"):
        written = _emit(db_session, _unscoped_usage(account.id))

    assert written == []
    messages = [
        record.getMessage()
        for record in caplog.records
        if "unattributed" in record.getMessage()
    ]
    assert len(messages) == 1
    assert "2 active sessions" in messages[0]


def test_count_active_by_principal_ignores_ended_quiet_and_other_principals(
    db_session: Session,
) -> None:
    account = _account(db_session)
    other_account = _account(db_session)
    now = datetime.now(UTC)
    _session(db_session, account.id, suffix="live", last_activity_at=now)
    _session(
        db_session,
        account.id,
        suffix="ended",
        last_activity_at=now,
        ended_at=now,
    )
    _session(
        db_session,
        account.id,
        suffix="quiet",
        last_activity_at=now - timedelta(hours=1),
    )
    _session(db_session, other_account.id, suffix="elsewhere", last_activity_at=now)

    assert (
        crud_runtime_session.count_active_by_principal(
            db_session,
            account_id=str(account.id),
            principal_type=PRINCIPAL_TYPE,
            principal_id=PRINCIPAL_ID,
        )
        == 1
    )


def test_unscoped_gateway_call_not_attributed_with_a_per_run_sibling(
    db_session: Session,
) -> None:
    account = _account(db_session)
    now = datetime.now(UTC)
    _session(db_session, account.id, suffix="cli", last_activity_at=now)
    _session(db_session, account.id, suffix="run-1", last_activity_at=now, per_run=True)

    assert _emit(db_session, _unscoped_usage(account.id)) == []
