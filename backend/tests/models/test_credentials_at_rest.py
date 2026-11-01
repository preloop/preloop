"""Credentials stay encrypted at rest and keep working for live rows.

Synthetic rows only. Every check reads the raw column with SQL to prove the
stored form, and reads through the ORM to prove callers still get plaintext.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from preloop.models import credential_encryption, models
from preloop.models.credential_encryption import TARGETS, process_batch, run_all
from preloop.models.crud import crud_organization
from preloop.models.db.encrypted_types import is_sealed

SECRET = "synthetic-at-rest-secret"


def _raw(db: Session, table: str, column: str, row_id: uuid.UUID) -> Any:
    return db.execute(
        text(f'SELECT "{column}" FROM {table} WHERE id = :id'), {"id": row_id}
    ).scalar_one()


def _reload(db: Session, model: Any, row_id: uuid.UUID) -> Any:
    db.expire_all()
    return db.get(model, row_id)


@pytest.fixture
def rows(db_session, create_account, create_tracker, create_organization):
    """One live row per sealed column, written through the ORM."""
    account = create_account()
    tracker = create_tracker(account=account)
    tracker.api_key = SECRET
    org = create_organization(tracker=tracker)
    org.webhook_secret = SECRET
    webhook = models.Webhook(
        external_id="hook-1",
        url="https://hooks.example/1",
        secret=SECRET,
        events=["push"],
        organization_id=org.id,
    )
    server = models.MCPServer(
        account_id=account.id,
        name="srv",
        url="https://mcp.example",
        auth_type="bearer",
        auth_config={"token": SECRET, "headers": {"X-Api-Key": SECRET}},
    )
    flow = models.Flow(
        name="flow",
        account_id=account.id,
        prompt_template="p",
        trigger_event_source="webhook",
        agent_type="codex",
        agent_config={},
        webhook_config={
            "webhook_secret": SECRET,
            "employee_secret": SECRET,
            "dedupe_path": "id",
        },
    )
    workflow = models.ApprovalWorkflow(
        account_id=account.id,
        name="wf",
        approval_type="webhook",
        approval_config={
            "webhook_secret": SECRET,
            "api_key": SECRET,
            "webhook_url": "https://x",
        },
    )
    snapshot = models.PolicySnapshot(
        account_id=account.id,
        version_number=1,
        snapshot_data={
            "mcp_servers": [{"name": "srv", "auth_config": {"token": SECRET}}]
        },
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    agent = models.ManagedAgent(
        account_id=account.id,
        agent_kind="openclaw",
        session_source_type="test",
        session_source_id=str(uuid.uuid4()),
        display_name="agent",
        lifecycle_updated_at=now,
        last_seen_at=now,
    )
    db_session.add_all([webhook, server, flow, workflow, snapshot, agent])
    db_session.flush()
    agent_doc = {"baseUrl": "https://gw", "apiKey": SECRET, "runtime_session_id": "r1"}
    enrollment = models.ManagedAgentEnrollment(
        account_id=account.id,
        managed_agent_id=agent.id,
        adapter_key="openclaw",
        discovered_config=agent_doc,
        managed_config=agent_doc,
        backup_metadata={"env": {"ANTHROPIC_AUTH_TOKEN": SECRET}},
    )
    db_session.add(enrollment)
    db_session.flush()
    return {
        "tracker": tracker,
        "organization": org,
        "webhook": webhook,
        "mcp_server": server,
        "flow": flow,
        "approval_workflow": workflow,
        "policy_snapshot": snapshot,
        "managed_agent_enrollment": enrollment,
    }


def test_orm_writes_are_sealed_and_read_back_as_plaintext(db_session, rows) -> None:
    for target in TARGETS:
        row = rows[target.table]
        stored = _raw(db_session, target.table, target.column, row.id)
        assert SECRET not in repr(stored), target.name
        loaded = _reload(db_session, type(row), row.id)
        assert SECRET in repr(getattr(loaded, target.column)), target.name
    assert is_sealed(_raw(db_session, "webhook", "secret", rows["webhook"].id))


def test_live_credentials_keep_working(db_session, rows) -> None:
    tracker = _reload(db_session, models.Tracker, rows["tracker"].id)
    if tracker.credentials_secret is None:
        assert tracker.resolved_api_key == SECRET
    server = _reload(db_session, models.MCPServer, rows["mcp_server"].id)
    assert server.auth_config["headers"]["X-Api-Key"] == SECRET
    flow = _reload(db_session, models.Flow, rows["flow"].id)
    assert flow.webhook_config["employee_secret"] == SECRET
    workflow = _reload(
        db_session, models.ApprovalWorkflow, rows["approval_workflow"].id
    )
    assert workflow.approval_config["api_key"] == SECRET
    enrollment_id = rows["managed_agent_enrollment"].id
    enrollment = _reload(db_session, models.ManagedAgentEnrollment, enrollment_id)
    assert enrollment.managed_config["apiKey"] == SECRET
    # Non-secret fields stay queryable with JSONB operators.
    found = (
        db_session.query(models.ManagedAgentEnrollment.id)
        .filter(
            models.ManagedAgentEnrollment.id == enrollment_id,
            models.ManagedAgentEnrollment.managed_config["runtime_session_id"].astext
            == "r1",
        )
        .scalar()
    )
    assert found == enrollment_id
    # The generator compares the column with "" in SQL; it must still find the
    # stored secret instead of minting a new one.
    org_id = rows["organization"].id
    assert (
        crud_organization.ensure_webhook_secret(db_session, organization_id=org_id)
        == SECRET
    )


def _write_legacy_plaintext(db: Session, rows: dict[str, Any]) -> None:
    """Simulate rows written before encryption at rest."""
    for target in TARGETS:
        row_id = rows[target.table].id
        if target.cast is None:
            value: Any = SECRET
            assignment = f'"{target.column}" = :value'
        else:
            import json

            raw = {
                "mcp_auth_config": {"token": SECRET, "scope": "read"},
                "webhook_secret": {"api_key": SECRET},
                "agent_config": {"apiKey": SECRET, "model": "m"},
                "policy_snapshot": {
                    "mcp_servers": [{"name": "s", "auth_config": {"token": SECRET}}]
                },
            }[target.selector or ""]
            value = json.dumps(raw)
            assignment = f'"{target.column}" = CAST(:value AS {target.cast})'
        db.execute(
            text(f"UPDATE {target.table} SET {assignment} WHERE id = :id"),
            {"value": value, "id": row_id},
        )


def test_legacy_plaintext_rows_are_readable_before_the_backfill(
    db_session, rows
) -> None:
    _write_legacy_plaintext(db_session, rows)
    org = _reload(db_session, models.Organization, rows["organization"].id)
    assert org.webhook_secret == SECRET
    server = _reload(db_session, models.MCPServer, rows["mcp_server"].id)
    assert server.auth_config["token"] == SECRET


def _ours(target: Any, rows: dict[str, Any]) -> uuid.UUID:
    # Start just before our row so rows from other tests do not interfere.
    row_id = rows[target.table].id
    return uuid.UUID(int=row_id.int - 1)


def test_batch_dry_run_counts_without_writing(db_session, rows) -> None:
    _write_legacy_plaintext(db_session, rows)
    for target in TARGETS:
        result = process_batch(
            db_session, target, after=_ours(target, rows), batch_size=1, dry_run=True
        )
        assert (result.plaintext, result.changed) == (1, 1), target.name
        stored = _raw(db_session, target.table, target.column, rows[target.table].id)
        assert SECRET in repr(stored), target.name


def test_batch_seals_in_place_is_idempotent_and_reversible(db_session, rows) -> None:
    _write_legacy_plaintext(db_session, rows)
    for target in TARGETS:
        row_id = rows[target.table].id
        after = _ours(target, rows)
        first = process_batch(db_session, target, after=after, batch_size=1)
        assert first.changed == 1 and first.last_id == row_id, target.name
        assert SECRET not in repr(_raw(db_session, target.table, target.column, row_id))
        loaded = _reload(db_session, type(rows[target.table]), row_id)
        assert SECRET in repr(getattr(loaded, target.column)), target.name

        again = process_batch(db_session, target, after=after, batch_size=1)
        assert (again.plaintext, again.changed) == (0, 0), target.name

        back = process_batch(
            db_session, target, after=after, batch_size=1, decrypt=True
        )
        assert back.changed == 1, target.name
        assert SECRET in repr(_raw(db_session, target.table, target.column, row_id))


def test_registry_step_advances_cursor_and_finishes(db_session, rows) -> None:
    _write_legacy_plaintext(db_session, rows)
    target = next(t for t in TARGETS if t.name == "cred_webhook_secret")
    row_id = rows["webhook"].id
    db_session.execute(
        text(
            "UPDATE agent_usage_backfill SET last_usage_id = :c, ready = false "
            "WHERE name = :n"
        ),
        {"c": _ours(target, rows), "n": target.name},
    )
    step = credential_encryption.REGISTERED_STEPS[target.name]
    ready = False
    for _ in range(1000):
        ready = step(db_session)
        if ready:
            break
    assert ready
    assert is_sealed(_raw(db_session, "webhook", "secret", row_id))
    cursor = db_session.execute(
        text("SELECT last_usage_id FROM agent_usage_backfill WHERE name = :n"),
        {"n": target.name},
    ).scalar_one()
    assert cursor >= row_id


def test_run_all_reports_counts_and_leaves_nothing_plaintext(
    db_session, rows, monkeypatch
) -> None:
    _write_legacy_plaintext(db_session, rows)
    # run_all commits per batch; keep the test transaction intact.
    monkeypatch.setattr(db_session, "commit", db_session.flush)
    monkeypatch.setattr(db_session, "rollback", lambda: None)
    dry = run_all(db_session, dry_run=True)
    assert all(dry[t.name].plaintext >= 1 for t in TARGETS)
    run_all(db_session)
    after = run_all(db_session, dry_run=True)
    assert all(after[t.name].plaintext == 0 for t in TARGETS), after
