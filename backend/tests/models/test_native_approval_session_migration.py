"""Migration 20270103_native_approval_session backfills only what it can prove."""

import importlib.util
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_runtime_session


def _load() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20270103_native_approval_session.py"
    )
    spec = importlib.util.spec_from_file_location("native_approval_session_mig", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _session(db: Session, account_id: Any) -> models.RuntimeSession:
    return crud_runtime_session.upsert_by_source(
        db,
        account_id=account_id,
        session_source_type="claude_code",
        session_source_id=f"native-{uuid.uuid4().hex[:8]}",
        started_at=datetime.now(timezone.utc),
    )


def _approval(
    db: Session,
    user: models.User,
    *,
    origin_session: Optional[str],
    runtime_session_id: Optional[uuid.UUID] = None,
) -> models.ApprovalRequest:
    workflow = models.ApprovalWorkflow(
        account_id=user.account_id, name=f"wf-{uuid.uuid4().hex[:6]}"
    )
    tool = models.ToolConfiguration(
        account_id=user.account_id,
        tool_name=f"Bash-{uuid.uuid4().hex[:6]}",
        tool_source="agent",
    )
    db.add_all([workflow, tool])
    db.flush()
    tool_args: dict = {"command": "ls"}
    if origin_session is not None:
        tool_args["_preloop_origin"] = {
            "session_id": "native-session",
            "runtime_session_id": origin_session,
        }
    row = models.ApprovalRequest(
        account_id=user.account_id,
        tool_configuration_id=tool.id,
        approval_workflow_id=workflow.id,
        tool_name="Bash",
        tool_args=tool_args,
        status="approved",
        runtime_session_id=runtime_session_id,
    )
    db.add(row)
    db.flush()
    return row


def test_backfill_links_only_verified_same_account_origins(
    db_session: Session, test_user: models.User
) -> None:
    own = _session(db_session, test_user.account_id)
    other_account = models.Account(organization_name="Example other")
    db_session.add(other_account)
    db_session.flush()
    foreign = _session(db_session, other_account.id)
    already = _session(db_session, test_user.account_id)

    linked = _approval(db_session, test_user, origin_session=str(own.id))
    cross_account = _approval(db_session, test_user, origin_session=str(foreign.id))
    kept = _approval(
        db_session,
        test_user,
        origin_session=str(own.id),
        runtime_session_id=already.id,
    )
    malformed = _approval(db_session, test_user, origin_session="not-a-uuid")
    unknown = _approval(db_session, test_user, origin_session=str(uuid.uuid4()))
    no_origin = _approval(db_session, test_user, origin_session=None)

    migration = _load()
    with Operations.context(MigrationContext.configure(db_session.connection())):
        migration.upgrade()
    db_session.expire_all()

    assert db_session.get(models.ApprovalRequest, linked.id).runtime_session_id == (
        own.id
    )
    assert (
        db_session.get(models.ApprovalRequest, cross_account.id).runtime_session_id
        is None
    )
    assert db_session.get(models.ApprovalRequest, kept.id).runtime_session_id == (
        already.id
    )
    for row in (malformed, unknown, no_origin):
        assert db_session.get(models.ApprovalRequest, row.id).runtime_session_id is (
            None
        )

    # The downgrade leaves the links alone.
    with Operations.context(MigrationContext.configure(db_session.connection())):
        migration.downgrade()
    db_session.expire_all()
    assert db_session.get(models.ApprovalRequest, linked.id).runtime_session_id == (
        own.id
    )
