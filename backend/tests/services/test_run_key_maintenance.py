"""Run-scoped keys of ended executions and sessions are deactivated.

Keys a person created, durable managed-agent credentials and keys of runs
still in progress are never touched. Names are never evidence.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_api_key
from preloop.services.run_key_maintenance import (
    AUDIT_ACTION,
    deactivate_ended_run_keys,
    main,
)

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def _key(db: Session, user: models.User, label: str, **fields: Any) -> models.ApiKey:
    key, _ = crud_api_key.create_runtime_key(
        db,
        name=f"{label} {uuid4().hex[:6]}",
        account_id=user.account_id,
        user_id=user.id,
        commit=False,
        **fields,
    )
    return key


@pytest.fixture
def fixture_keys(db_session: Session, test_user: models.User) -> dict[str, Any]:
    flow = models.Flow(
        account_id=test_user.account_id,
        name="Key flow",
        prompt_template="Example",
        agent_config={},
    )
    db_session.add(flow)
    db_session.flush()
    finished = models.FlowExecution(flow_id=flow.id, status="SUCCEEDED")
    running = models.FlowExecution(flow_id=flow.id, status="RUNNING")
    db_session.add_all([finished, running])
    ended_session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id=f"src-{uuid4().hex[:6]}",
        started_at=NOW - timedelta(hours=3),
        ended_at=NOW - timedelta(hours=1),
    )
    live_session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id=f"src-{uuid4().hex[:6]}",
        started_at=NOW - timedelta(hours=3),
    )
    db_session.add_all([ended_session, live_session])
    db_session.flush()
    future = NOW + timedelta(hours=1)
    keys = {
        "finished_run": _key(
            db_session,
            test_user,
            "Flow Execution",
            expires_at=future,
            context_data={"flow_execution_id": str(finished.id)},
        ),
        "deleted_run": _key(
            db_session,
            test_user,
            "Flow Execution",
            expires_at=future,
            context_data={"flow_execution_id": str(uuid4())},
        ),
        "expired_running": _key(
            db_session,
            test_user,
            "Flow Execution",
            expires_at=NOW - timedelta(minutes=5),
            context_data={"flow_execution_id": str(running.id)},
        ),
        "ended_session": _key(
            db_session,
            test_user,
            "Session",
            expires_at=future,
            context_data={"runtime_session_id": str(ended_session.id)},
        ),
        # Kept: run in progress, live session, hand-made, durable credential.
        "running": _key(
            db_session,
            test_user,
            "Flow Execution",
            expires_at=future,
            context_data={"flow_execution_id": str(running.id)},
        ),
        "live_session": _key(
            db_session,
            test_user,
            "Session",
            expires_at=future,
            context_data={"runtime_session_id": str(live_session.id)},
        ),
        "hand_made_expired": _key(
            db_session,
            test_user,
            "Managed Agent Credential: Example / run-1",
            expires_at=NOW - timedelta(days=1),
        ),
        "durable": _key(
            db_session,
            test_user,
            "Managed Agent Credential: Example / run-2",
            expires_at=NOW - timedelta(days=1),
            context_data={
                "credential_kind": "managed_agent_durable",
                "managed_agent_id": str(uuid4()),
                "runtime_session_id": str(ended_session.id),
            },
        ),
    }
    db_session.flush()
    return keys


ENDED = {"finished_run", "deleted_run", "expired_running", "ended_session"}


def _active(db: Session, keys: dict[str, Any]) -> set[str]:
    db.expire_all()
    return {
        name for name, key in keys.items() if db.get(models.ApiKey, key.id).is_active
    }


def test_dry_run_counts_and_changes_nothing(
    db_session: Session, test_user: models.User, fixture_keys: dict[str, Any]
) -> None:
    result = deactivate_ended_run_keys(
        db_session, dry_run=True, account_id=test_user.account_id
    )

    assert sum(result.matched.values()) == 4
    assert result.matched == {
        "execution_ended": 2,
        "expired": 1,
        "session_ended": 1,
    }
    assert result.deactivated == 0
    assert _active(db_session, fixture_keys) == set(fixture_keys)


def test_apply_deactivates_ended_run_keys_in_audited_batches(
    db_session: Session, test_user: models.User, fixture_keys: dict[str, Any]
) -> None:
    result = deactivate_ended_run_keys(
        db_session, dry_run=False, batch_size=3, account_id=test_user.account_id
    )

    assert result.deactivated == 4
    assert result.batches == 2
    assert result.complete
    assert _active(db_session, fixture_keys) == set(fixture_keys) - ENDED
    audits = (
        db_session.query(models.AuditLog)
        .filter_by(action=AUDIT_ACTION, account_id=test_user.account_id)
        .all()
    )
    assert sorted(a.details["batch"] for a in audits) == [1, 2]
    assert sum(a.details["deactivated"] for a in audits) == 4
    # A second pass finds nothing.
    again = deactivate_ended_run_keys(
        db_session, dry_run=False, account_id=test_user.account_id
    )
    assert again.deactivated == 0 and again.batches == 0


def test_pass_is_bounded(
    db_session: Session, test_user: models.User, fixture_keys: dict[str, Any]
) -> None:
    result = deactivate_ended_run_keys(
        db_session,
        dry_run=False,
        batch_size=1,
        max_batches=2,
        account_id=test_user.account_id,
    )
    assert result.deactivated == 2
    assert result.complete is False


def test_command_defaults_to_dry_run(
    db_session: Session,
    test_user: models.User,
    fixture_keys: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _sessions() -> Any:
        yield db_session

    monkeypatch.setattr("preloop.models.db.session.get_db_session", _sessions)
    main(["--account-id", str(test_user.account_id)])
    out = capsys.readouterr().out
    assert '"mode": "dry-run"' in out
    assert '"matched_total": 4' in out
    assert _active(db_session, fixture_keys) == set(fixture_keys)


def test_runs_inside_the_flush_grace_keep_their_keys(
    db_session: Session, test_user: models.User
) -> None:
    flow = models.Flow(
        account_id=test_user.account_id,
        name="Grace flow",
        prompt_template="Example",
        agent_config={},
    )
    db_session.add(flow)
    db_session.flush()
    just_ended = models.FlowExecution(
        flow_id=flow.id, status="SUCCEEDED", end_time=NOW - timedelta(seconds=10)
    )
    long_ended = models.FlowExecution(
        flow_id=flow.id, status="FAILED", end_time=NOW - timedelta(minutes=10)
    )
    session_just_ended = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id=f"src-{uuid4().hex[:6]}",
        started_at=NOW - timedelta(hours=1),
        ended_at=NOW - timedelta(seconds=10),
    )
    db_session.add_all([just_ended, long_ended, session_just_ended])
    db_session.flush()
    future = NOW + timedelta(hours=1)
    keys = {
        "just_ended": _key(
            db_session,
            test_user,
            "Flow Execution",
            expires_at=future,
            context_data={"flow_execution_id": str(just_ended.id)},
        ),
        "long_ended": _key(
            db_session,
            test_user,
            "Flow Execution",
            expires_at=future,
            context_data={"flow_execution_id": str(long_ended.id)},
        ),
        "session_just_ended": _key(
            db_session,
            test_user,
            "Session",
            expires_at=future,
            context_data={"runtime_session_id": str(session_just_ended.id)},
        ),
    }
    db_session.flush()

    result = deactivate_ended_run_keys(
        db_session, dry_run=False, account_id=test_user.account_id
    )

    assert result.deactivated == 1
    assert _active(db_session, keys) == {"just_ended", "session_just_ended"}
