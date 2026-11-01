"""Applying current sensitive-data rules to stored rows (#2133)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from preloop.config import settings
from preloop.models import models
from preloop.services import audit_chain
from preloop.services.policy.schema import SensitiveDataConfig
from preloop.services.sensitive_data import reference, storage
from preloop.services.sensitive_data import stored_rows
from preloop.services.sensitive_data.stored_rows import (
    STORE_APPROVALS,
    STORE_AUDIT,
    STORE_SEARCH,
    apply_to_stored_rows,
)

EMAIL = "alice@example.com"
NO_LAG = timedelta(0)

REDACT_ONLY = SensitiveDataConfig.model_validate(
    {
        "rules": [
            {
                "id": "redact-email",
                "on": ["tool.args", "tool.result"],
                "types": ["email"],
                "action": "redact",
            }
        ]
    }
)

WITH_REFERENCE = SensitiveDataConfig.model_validate(
    {
        "rules": [
            {
                "id": "redact-email",
                "on": ["tool.args", "tool.result"],
                "types": ["email"],
                "action": "redact",
            }
        ],
        "reference_only": [
            {
                "id": "record-refs",
                # Stored rows do not record the server; the tool name decides.
                "scope": {"tools": ["read_record"], "servers": ["records"]},
                "keep_fields": ["$.consent_id"],
            }
        ],
    }
)


@pytest.fixture(autouse=True)
def chain_settings(monkeypatch):
    monkeypatch.setattr(settings, "audit_chain_enabled", True, raising=False)
    monkeypatch.setattr(settings, "audit_chain_seal_lag_seconds", 0, raising=False)
    monkeypatch.setattr(settings, "audit_chain_checkpoint_interval", 2, raising=False)


@pytest.fixture(autouse=True)
def salts(mocker):
    """Reference fingerprints use in-memory salts instead of a second session."""
    accounts: dict = {}

    def get(db, id):  # noqa: A002 - mirrors crud signature
        account = accounts.get(str(id))
        if account is None:
            account = MagicMock()
            account.meta_data = {}
            accounts[str(id)] = account
        return account

    mocker.patch.object(reference.crud_account, "get", side_effect=get)
    mocker.patch(
        "preloop.models.db.session.get_session_factory",
        return_value=lambda: MagicMock(),
    )
    mocker.patch.object(reference, "flag_modified")
    reference.invalidate_salt_cache()
    storage.invalidate_cache()
    yield accounts
    reference.invalidate_salt_cache()
    storage.invalidate_cache()


@pytest.fixture
def account_id(test_user):
    return test_user.account_id


def _tool_rows(db, account_id, count, *, tool="send_mail", start_minutes=600):
    base = datetime.now(UTC) - timedelta(minutes=start_minutes)
    rows = []
    for index in range(count):
        row = models.AuditLog(
            account_id=account_id,
            action="tool_call",
            resource_type="tool",
            resource_id=tool,
            status="success",
            details={
                "tool_name": tool,
                "tool_args": {"to": EMAIL, "n": index, "consent_id": f"c-{index}"},
                "duration_ms": 5,
            },
            timestamp=base + timedelta(seconds=index),
        )
        db.add(row)
        rows.append(row)
    # Plain rows between them, so the chain has rows the pass must not touch.
    db.add(
        models.AuditLog(
            account_id=account_id,
            action="permission_check",
            resource_type="tool",
            status="success",
            details={"note": EMAIL},
            timestamp=base + timedelta(seconds=count),
        )
    )
    db.flush()
    return rows


def _seal(db, account_id):
    return audit_chain.seal_account(db, account_id=account_id, lag=NO_LAG)


def _verify(db, account_id):
    return audit_chain.verify_chain(db, account_id=account_id)


def _stored_text(db, account_id):
    rows = db.execute(
        select(models.AuditLog)
        .where(
            models.AuditLog.account_id == account_id,
            models.AuditLog.action == "tool_call",
        )
        .execution_options(populate_existing=True)
    ).scalars()
    return [str(row.details) for row in rows]


def _run(db, account_id, **kwargs):
    kwargs.setdefault("config", REDACT_ONLY)
    kwargs.setdefault("stores", [STORE_AUDIT])
    return apply_to_stored_rows(db, account_id=account_id, **kwargs)


def test_sealed_rows_are_redacted_and_the_chain_still_verifies(
    db_session, account_id, test_user
):
    _tool_rows(db_session, account_id, 4)
    _seal(db_session, account_id)
    before = _verify(db_session, account_id)
    assert before["status"] == "ok" and before["checkpoints_verified"] >= 2

    report = _run(db_session, account_id, dry_run=False, actor_user_id=test_user.id)

    assert report.complete and report.error is None
    assert report.stores[STORE_AUDIT].changed == 4
    assert report.stores[STORE_AUDIT].counts == {"email": 4}
    assert report.rule_ids == ["redact-email"]
    assert report.chained_rows_rewritten == 4
    assert all(EMAIL not in text for text in _stored_text(db_session, account_id))
    assert all(
        "[REDACTED:email]" in text for text in _stored_text(db_session, account_id)
    )
    # The row outside the tool stores is untouched.
    plain = db_session.execute(
        select(models.AuditLog).where(
            models.AuditLog.action == "permission_check",
            models.AuditLog.account_id == account_id,
        )
    ).scalar_one()
    assert plain.details == {"note": EMAIL} and plain.redacted_row_hash is None

    # The pass seals its event, so the rewrite is anchored straight away.
    after = _verify(db_session, account_id)
    assert after["status"] == "ok", after["first_break"]
    assert after["redacted_rows"] == 4
    # Every checkpoint taken before the rewrite still verifies.
    assert after["checkpoints_verified"] >= before["checkpoints_verified"]
    assert after["checkpoint_failures"] == []

    event = db_session.get(models.AuditLog, report.events[0])
    assert event.action == audit_chain.ACTION_REDACTION_APPLIED
    assert event.user_id == test_user.id
    assert event.details["counts_by_type"] == {"email": 4}
    assert len(event.details["rows"]) == 4
    assert EMAIL not in str(event.details)


def test_segment_serves_the_redaction_fields(db_session, account_id):
    _tool_rows(db_session, account_id, 1)
    _seal(db_session, account_id)
    _run(db_session, account_id, dry_run=False)

    entry = audit_chain.chain_segment(db_session, account_id=account_id)["entries"][0]

    recomputed = audit_chain.hash_row(entry["payload"])
    assert recomputed != entry["row_hash"]
    assert recomputed == entry["redacted_row_hash"]
    assert entry["redaction_event_id"]


def test_a_rewrite_without_its_event_is_a_break(db_session, account_id):
    _tool_rows(db_session, account_id, 2)
    _seal(db_session, account_id)
    _run(db_session, account_id, dry_run=False)
    row = db_session.execute(
        select(models.AuditLog).where(
            models.AuditLog.account_id == account_id, models.AuditLog.chain_seq == 1
        )
    ).scalar_one()

    # Someone edits the redacted row again, outside the pass.
    row.details = {**row.details, "tool_args": {"to": "bob@example.com"}}
    db_session.flush()
    report = _verify(db_session, account_id)
    assert report["status"] == "broken"
    assert report["first_break"]["kind"] == audit_chain.BREAK_REDACTION_UNANCHORED

    # Or forges a matching redacted hash without an event listing it.
    row.redacted_row_hash = audit_chain.hash_row(
        audit_chain.canonical_row(row, seq=1, prev_hash=row.prev_hash)
    )
    db_session.flush()
    report = _verify(db_session, account_id)
    assert report["first_break"]["kind"] == audit_chain.BREAK_REDACTION_UNANCHORED


def test_an_edit_without_redaction_fields_is_still_a_row_hash_break(
    db_session, account_id
):
    _tool_rows(db_session, account_id, 2)
    _seal(db_session, account_id)
    row = db_session.execute(
        select(models.AuditLog).where(
            models.AuditLog.account_id == account_id, models.AuditLog.chain_seq == 1
        )
    ).scalar_one()
    row.details = {**row.details, "tool_args": {}}
    db_session.flush()

    assert (
        _verify(db_session, account_id)["first_break"]["kind"]
        == audit_chain.BREAK_ROW_HASH
    )


def test_dry_run_counts_and_writes_nothing(db_session, account_id):
    _tool_rows(db_session, account_id, 3)
    _seal(db_session, account_id)
    events_before = (
        db_session.execute(
            select(models.AuditLog).where(models.AuditLog.account_id == account_id)
        )
        .scalars()
        .all()
    )

    report = _run(db_session, account_id, dry_run=True)

    assert report.dry_run and report.complete
    assert report.stores[STORE_AUDIT].changed == 3
    assert report.stores[STORE_AUDIT].counts == {"email": 3}
    assert report.events == []
    assert all(EMAIL in text for text in _stored_text(db_session, account_id))
    after = (
        db_session.execute(
            select(models.AuditLog).where(models.AuditLog.account_id == account_id)
        )
        .scalars()
        .all()
    )
    assert len(after) == len(events_before)
    assert _verify(db_session, account_id)["redacted_rows"] == 0


def test_second_pass_is_a_no_op(db_session, account_id):
    _tool_rows(db_session, account_id, 3)
    _seal(db_session, account_id)
    first = _run(db_session, account_id, dry_run=False, config=WITH_REFERENCE)
    snapshot = _stored_text(db_session, account_id)

    second = _run(db_session, account_id, dry_run=False, config=WITH_REFERENCE)

    assert first.stores[STORE_AUDIT].changed == 3
    assert second.stores[STORE_AUDIT].changed == 0
    assert second.events == []
    assert _stored_text(db_session, account_id) == snapshot


def test_unsealed_rows_are_rewritten_and_sealed_redacted(db_session, account_id):
    _tool_rows(db_session, account_id, 2)

    report = _run(db_session, account_id, dry_run=False)
    _seal(db_session, account_id)

    assert report.chained_rows_rewritten == 0
    rows = (
        db_session.execute(
            select(models.AuditLog).where(
                models.AuditLog.account_id == account_id,
                models.AuditLog.action == "tool_call",
            )
        )
        .scalars()
        .all()
    )
    assert all(row.redacted_row_hash is None and row.redacted_at for row in rows)
    verdict = _verify(db_session, account_id)
    assert verdict["status"] == "ok" and verdict["redacted_rows"] == 0


def test_reference_only_rule_replaces_arguments_with_a_reference(
    db_session, account_id
):
    _tool_rows(db_session, account_id, 2, tool="read_record")
    _seal(db_session, account_id)

    report = _run(db_session, account_id, dry_run=False, config=WITH_REFERENCE)

    assert report.stores[STORE_AUDIT].references == 2
    assert "record-refs" in report.rule_ids
    rows = (
        db_session.execute(
            select(models.AuditLog)
            .where(
                models.AuditLog.account_id == account_id,
                models.AuditLog.action == "tool_call",
            )
            .order_by(models.AuditLog.chain_seq)
        )
        .scalars()
        .all()
    )
    record = rows[0].details["tool_args"]
    assert reference.is_reference_record(record)
    assert record["kept"] == {"$.consent_id": "c-0"}
    assert record["args_hmac"] and record["salt_id"]
    assert EMAIL not in str(rows[0].details)
    assert _verify(db_session, account_id)["status"] == "ok"


def test_tightened_reference_rule_drops_kept_fields(db_session, account_id):
    _tool_rows(db_session, account_id, 1, tool="read_record")
    _seal(db_session, account_id)
    _run(db_session, account_id, dry_run=False, config=WITH_REFERENCE)
    tighter = SensitiveDataConfig.model_validate(
        {
            "reference_only": [
                {
                    "id": "record-refs",
                    "scope": {"tools": ["read_record"]},
                    "keep_fields": [],
                }
            ]
        }
    )

    report = _run(db_session, account_id, dry_run=False, config=tighter)

    assert report.stores[STORE_AUDIT].changed == 1
    row = db_session.execute(
        select(models.AuditLog).where(
            models.AuditLog.account_id == account_id,
            models.AuditLog.action == "tool_call",
        )
    ).scalar_one()
    assert row.details["tool_args"]["kept"] == {}
    verdict = _verify(db_session, account_id)
    assert verdict["status"] == "ok" and verdict["redacted_rows"] == 1


def test_forged_reference_marker_is_treated_as_content(db_session, account_id):
    db_session.add(
        models.AuditLog(
            account_id=account_id,
            action="tool_call",
            status="success",
            details={
                "tool_name": "read_record",
                "tool_args": {
                    reference.REFERENCE_MARKER: True,
                    "schema": reference.RECORD_SCHEMA,
                    "smuggled": EMAIL,
                },
            },
            timestamp=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    db_session.flush()

    _run(db_session, account_id, dry_run=False, config=WITH_REFERENCE)

    assert all(EMAIL not in text for text in _stored_text(db_session, account_id))


def test_salt_rotation_between_passes_keeps_older_fingerprints(
    db_session, account_id, salts
):
    _tool_rows(db_session, account_id, 1, tool="read_record")
    _run(db_session, account_id, dry_run=False, config=WITH_REFERENCE)
    first = (
        db_session.execute(
            select(models.AuditLog).where(
                models.AuditLog.account_id == account_id,
                models.AuditLog.action == "tool_call",
            )
        )
        .scalar_one()
        .details["tool_args"]
    )
    reference.rotate_salt(MagicMock(), account_id)

    again = _run(db_session, account_id, dry_run=False, config=WITH_REFERENCE)

    assert again.stores[STORE_AUDIT].changed == 0
    ok, _salt = reference.verify_hmac(
        account_id,
        {"to": EMAIL, "n": 0, "consent_id": "c-0"},
        first["args_hmac"],
        salt_id=first["salt_id"],
    )
    assert ok


def test_max_batches_stops_with_a_cursor_and_resume_finishes(db_session, account_id):
    _tool_rows(db_session, account_id, 5)
    _seal(db_session, account_id)

    partial = _run(db_session, account_id, dry_run=False, batch_size=2, max_batches=1)

    assert not partial.complete
    assert partial.stores[STORE_AUDIT].changed == 2
    cursor = partial.as_dict()["stores"][STORE_AUDIT]["cursor"]
    assert cursor

    rest = _run(
        db_session,
        account_id,
        dry_run=False,
        batch_size=2,
        cursors={STORE_AUDIT: cursor},
    )

    assert rest.complete
    assert rest.stores[STORE_AUDIT].changed == 3
    assert all(EMAIL not in text for text in _stored_text(db_session, account_id))
    verdict = _verify(db_session, account_id)
    assert verdict["status"] == "ok" and verdict["redacted_rows"] == 5


def test_a_failed_batch_rolls_back_alone_and_a_rerun_completes(db_session, account_id):
    _tool_rows(db_session, account_id, 5)
    _seal(db_session, account_id)

    def fail_second(store, batch):
        if batch == 2:
            raise RuntimeError(EMAIL)  # content in a message must not surface

    failed = _run(
        db_session, account_id, dry_run=False, batch_size=2, on_batch=fail_second
    )

    assert failed.error == f"{STORE_AUDIT}: RuntimeError"
    assert EMAIL not in str(failed.as_dict())
    assert failed.stores[STORE_AUDIT].changed == 2
    assert len(failed.events) == 1
    texts = _stored_text(db_session, account_id)
    assert sum(EMAIL not in text for text in texts) == 2
    assert _verify(db_session, account_id)["status"] == "ok"

    rerun = _run(db_session, account_id, dry_run=False, batch_size=2)

    assert rerun.complete
    assert rerun.stores[STORE_AUDIT].changed == 3
    assert all(EMAIL not in text for text in _stored_text(db_session, account_id))
    verdict = _verify(db_session, account_id)
    assert verdict["status"] == "ok" and verdict["redacted_rows"] == 5


def test_window_limits_the_rows(db_session, account_id):
    rows = _tool_rows(db_session, account_id, 4)

    report = _run(
        db_session,
        account_id,
        dry_run=True,
        since=rows[2].timestamp,
        until=rows[3].timestamp,
    )

    assert report.stores[STORE_AUDIT].scanned == 1


def test_rejects_unknown_store_and_empty_window(db_session, account_id):
    with pytest.raises(ValueError):
        _run(db_session, account_id, stores=["nope"])
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        _run(db_session, account_id, since=now, until=now)


def _approval(db, account_id, *, status="approved", legal_hold=False):
    workflow = models.ApprovalWorkflow(
        account_id=account_id, name=f"wf-{status}-{legal_hold}", workflow_type="simple"
    )
    tool = models.ToolConfiguration(
        account_id=account_id,
        tool_name=f"send_mail_{status}_{legal_hold}",
        tool_source="builtin",
    )
    db.add_all([workflow, tool])
    db.flush()
    row = models.ApprovalRequest(
        account_id=account_id,
        tool_configuration_id=tool.id,
        approval_workflow_id=workflow.id,
        tool_name="send_mail",
        tool_args={"to": EMAIL},
        tool_result={"text": f"sent to {EMAIL}"},
        requested_at=datetime.now(UTC) - timedelta(hours=1),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        status=status,
        legal_hold=legal_hold,
    )
    db.add(row)
    db.flush()
    return row


def test_approvals_are_redacted_except_pending_and_held(db_session, account_id):
    done = _approval(db_session, account_id)
    pending = _approval(db_session, account_id, status="pending")
    held = _approval(db_session, account_id, legal_hold=True)

    report = _run(db_session, account_id, dry_run=False, stores=[STORE_APPROVALS])

    item = report.stores[STORE_APPROVALS]
    assert (item.scanned, item.changed) == (3, 1)
    assert item.skipped_pending == 1 and item.skipped_legal_hold == 1
    for row in (done, pending, held):
        db_session.refresh(row)
    assert done.tool_args == {"to": "[REDACTED:email]"}
    assert EMAIL not in str(done.tool_result)
    assert pending.tool_args == {"to": EMAIL}
    assert held.tool_args == {"to": EMAIL}
    event = db_session.get(models.AuditLog, report.events[0])
    assert event.details["store"] == STORE_APPROVALS and event.details["rows"] == []


def _runtime_session(db, account_id, *, legal_hold=False):
    now = datetime.now(UTC).replace(tzinfo=None)
    row = models.RuntimeSession(
        account_id=account_id,
        session_source_type="claude_code",
        session_source_id=f"redaction-{legal_hold}-{now.timestamp()}",
        started_at=now - timedelta(hours=1),
        last_activity_at=now,
        legal_hold=legal_hold,
    )
    db.add(row)
    db.flush()
    return row


def _chunk(db, account_id, session, text):
    from preloop.models.crud.session_search_document import content_hash_for

    row = models.SessionSearchDocument(
        account_id=account_id,
        runtime_session_id=session.id,
        source_kind="message",
        source_id=f"m-{session.id}",
        chunk_index=0,
        occurred_at=datetime.now(UTC) - timedelta(minutes=30),
        content=text,
        content_hash=content_hash_for(text),
        redaction_state="clear",
        embedding_state="embedded",
        embedding_model="test:model@3",
    )
    db.add(row)
    db.flush()
    return row


def test_search_index_and_timeline_are_redacted_and_reembedded(db_session, account_id):
    open_session = _runtime_session(db_session, account_id)
    held_session = _runtime_session(db_session, account_id, legal_hold=True)
    chunk = _chunk(db_session, account_id, open_session, f"mail {EMAIL} now")
    held_chunk = _chunk(db_session, account_id, held_session, f"mail {EMAIL} now")
    activity = models.RuntimeSessionActivity(
        account_id=account_id,
        runtime_session_id=open_session.id,
        activity_type="tool_call",
        tool_name="send_mail",
        summary=f"sent to {EMAIL}",
        timestamp=datetime.now(UTC) - timedelta(minutes=20),
    )
    db_session.add(activity)
    db_session.flush()
    # Account-wide rule: the index and timeline writers apply unscoped rules.
    config = SensitiveDataConfig.model_validate(
        {
            "rules": [
                {
                    "id": "redact-email",
                    "on": ["tool.args", "tool.result"],
                    "types": ["email"],
                    "action": "redact",
                }
            ]
        }
    )

    report = _run(
        db_session,
        account_id,
        dry_run=False,
        config=config,
        stores=[STORE_SEARCH, stored_rows.STORE_ACTIVITY],
    )

    assert report.stores[STORE_SEARCH].changed == 1
    assert report.stores[STORE_SEARCH].skipped_legal_hold == 1
    assert report.stores[stored_rows.STORE_ACTIVITY].changed == 1
    for row in (chunk, held_chunk, activity):
        db_session.refresh(row)
    assert chunk.content == "mail [REDACTED:email] now"
    assert chunk.redaction_state == "redacted"
    assert chunk.embedding_state == "pending" and chunk.embedding_model is None
    assert held_chunk.content == f"mail {EMAIL} now"
    assert activity.summary == "sent to [REDACTED:email]"


def test_audit_rows_of_a_held_session_are_skipped(db_session, account_id):
    held = _runtime_session(db_session, account_id, legal_hold=True)
    rows = _tool_rows(db_session, account_id, 2)
    rows[0].details = {**rows[0].details, "runtime_session_id": str(held.id)}
    db_session.flush()

    report = _run(db_session, account_id, dry_run=False)

    assert report.stores[STORE_AUDIT].skipped_legal_hold == 1
    assert report.stores[STORE_AUDIT].changed == 1


def test_endpoint_is_admin_only_and_dry_run_by_default(db_session, test_user, mocker):
    from fastapi import HTTPException

    from preloop.api.auth import router as auth_router
    from preloop.api.endpoints import policies

    # The account block read is covered by policy_store's tests; the salts
    # fixture stubs crud_account.get, so hand the block in here.
    mocker.patch.object(
        stored_rows, "load_sensitive_data_config", return_value=REDACT_ONLY
    )
    _tool_rows(db_session, test_user.account_id, 2)
    # The pass commits or rolls back per batch; it owns the session.
    db_session.commit()
    account = db_session.get(models.Account, test_user.account_id)
    request = policies.StoredRowRedactionRequest(stores=[STORE_AUDIT])

    mocker.patch.object(auth_router, "_is_account_admin", return_value=False)
    with pytest.raises(HTTPException) as denied:
        policies.sensitive_data_apply_to_stored(
            request=request, account=account, current_user=test_user, db=db_session
        )
    assert denied.value.status_code == 403

    mocker.patch.object(auth_router, "_is_account_admin", return_value=True)
    response = policies.sensitive_data_apply_to_stored(
        request=request, account=account, current_user=test_user, db=db_session
    )
    assert response.dry_run and response.complete
    assert response.stores[STORE_AUDIT].changed == 2
    assert response.redaction_events == []
    assert all(EMAIL in text for text in _stored_text(db_session, test_user.account_id))

    applied = policies.sensitive_data_apply_to_stored(
        request=policies.StoredRowRedactionRequest(stores=[STORE_AUDIT], dry_run=False),
        account=account,
        current_user=test_user,
        db=db_session,
    )
    assert (
        applied.stores[STORE_AUDIT].changed == 2 and len(applied.redaction_events) == 1
    )
    assert all(
        EMAIL not in text for text in _stored_text(db_session, test_user.account_id)
    )


def _first_redacted(db, account_id):
    return db.execute(
        select(models.AuditLog).where(
            models.AuditLog.account_id == account_id,
            models.AuditLog.chain_seq == 1,
        )
    ).scalar_one()


def test_an_unsealed_event_does_not_vouch(db_session, account_id, monkeypatch):
    _tool_rows(db_session, account_id, 2)
    _seal(db_session, account_id)
    # The pass cannot seal (chain off), so its event stays unsealed.
    monkeypatch.setattr(settings, "audit_chain_enabled", False, raising=False)
    report = _run(db_session, account_id, dry_run=False)
    event = db_session.get(models.AuditLog, report.events[0])
    assert event.chain_seq is None

    verdict = _verify(db_session, account_id)
    assert verdict["first_break"]["kind"] == audit_chain.BREAK_REDACTION_UNANCHORED

    _seal(db_session, account_id)
    assert _verify(db_session, account_id)["status"] == "ok"


def test_an_edited_event_does_not_vouch(db_session, account_id):
    _tool_rows(db_session, account_id, 2)
    _seal(db_session, account_id)
    report = _run(db_session, account_id, dry_run=False)
    event = db_session.get(models.AuditLog, report.events[0])
    rows = [dict(item) for item in event.details["rows"]]
    rows[0]["row_hash"] = "ff" * 32
    event.details = {**event.details, "rows": rows}
    db_session.flush()
    verdict = _verify(db_session, account_id)
    assert verdict["status"] == "broken"


def test_approvals_of_held_executions_and_sessions_are_skipped(db_session, account_id):
    held_session = _runtime_session(db_session, account_id, legal_hold=True)
    row = _approval(db_session, account_id)
    row.runtime_session_id = held_session.id
    db_session.flush()

    report = _run(db_session, account_id, dry_run=False, stores=[STORE_APPROVALS])

    assert report.stores[STORE_APPROVALS].skipped_legal_hold == 1
    db_session.refresh(row)
    assert row.tool_args == {"to": EMAIL}


def test_a_string_that_only_looks_like_a_reference_is_redacted(db_session, account_id):
    db_session.add(
        models.AuditLog(
            account_id=account_id,
            action="approval_tool_executed",
            status="success",
            details={
                "tool_name": "send_mail",
                "result_preview": f"[reference-only:x] send_mail {EMAIL}",
            },
            timestamp=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    db_session.flush()

    report = _run(db_session, account_id, dry_run=False)

    assert report.stores[STORE_AUDIT].changed == 1
    row = db_session.execute(
        select(models.AuditLog).where(
            models.AuditLog.account_id == account_id,
            models.AuditLog.action == "approval_tool_executed",
        )
    ).scalar_one()
    assert EMAIL not in row.details["result_preview"]


def test_a_removed_reference_rule_still_redacts_kept_values(db_session, account_id):
    db_session.add(
        models.AuditLog(
            account_id=account_id,
            action="tool_call",
            status="success",
            details={
                "tool_name": "read_record",
                "tool_args": {
                    reference.REFERENCE_MARKER: True,
                    "schema": reference.RECORD_SCHEMA,
                    "rule_id": "gone",
                    "kept": {"$.contact": EMAIL},
                    "args_hmac": "ab" * 16,
                },
            },
            timestamp=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    db_session.flush()

    report = _run(db_session, account_id, dry_run=False)

    assert report.stores[STORE_AUDIT].changed == 1
    stored = _stored_text(db_session, account_id)
    assert EMAIL not in stored[0] and "$.contact" in stored[0]


def test_agent_scoped_rules_match_a_managed_agent_principal(db_session, account_id):
    config = SensitiveDataConfig.model_validate(
        {
            "rules": [
                {
                    "id": "agent-email",
                    "on": ["tool.args"],
                    "types": ["email"],
                    "action": "redact",
                    "scope": {"agents": ["agent-7"]},
                }
            ]
        }
    )
    for principal in ("agent-7", "agent-8"):
        db_session.add(
            models.AuditLog(
                account_id=account_id,
                action="tool_call",
                status="success",
                details={
                    "tool_name": "send_mail",
                    "tool_args": {"to": EMAIL},
                    "runtime_principal_type": "managed_agent",
                    "runtime_principal_id": principal,
                },
                timestamp=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
    db_session.flush()

    report = _run(db_session, account_id, dry_run=False, config=config)

    assert report.stores[STORE_AUDIT].changed == 1


def test_a_malformed_cursor_is_a_value_error(db_session, account_id):
    with pytest.raises(ValueError):
        _run(db_session, account_id, cursors={STORE_AUDIT: ["2026-10-01T00:00:00"]})
