"""Receipt transaction, replay and bounded lock regression tests."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.callback_receipt import (
    CallbackReplayConflictError,
    callback_digest_epoch,
    callback_fingerprint,
    crud_callback_receipt,
)


def context(verdict: dict | None = None) -> tuple[MagicMock, SimpleNamespace, dict]:
    """Supply one locked receipt, with real SQL statements inspected below."""
    db = MagicMock(spec=Session)
    row = SimpleNamespace(
        id=uuid4(), body_digest="a" * 64, verdict=verdict, evidence=None
    )
    db.query.return_value.filter.return_value.first.return_value = (uuid4(),)
    db.query.return_value.filter.return_value.populate_existing.return_value.with_for_update.return_value.one.return_value = row
    args = {
        "account_id": uuid4(),
        "integration_id": uuid4(),
        "delivery_digest": "b" * 64,
        "body_digest": "a" * 64,
    }
    return db, row, args


def test_evaluation_and_audit_share_one_commit() -> None:
    db, row, args = context()
    evaluator = MagicMock(
        return_value=({"action": "deny"}, {"reason": "approval_required"})
    )
    result = crud_callback_receipt.complete_once(db, evaluate=evaluator, **args)
    assert result is row
    evaluator.assert_called_once_with(row.id)
    db.commit.assert_called_once()
    db.rollback.assert_not_called()
    statement = str(
        db.execute.call_args_list[2].args[0].compile(dialect=postgresql.dialect())
    )
    assert (
        "ON CONFLICT ON CONSTRAINT uq_callback_receipt_delivery DO NOTHING" in statement
    )
    assert "transcript" not in statement


def test_completed_retry_never_evaluates_or_audits_again() -> None:
    db, row, args = context({"action": "allow", "reference_id": "safe"})
    evaluator = MagicMock()
    assert (
        crud_callback_receipt.complete_once(db, evaluate=evaluator, **args).verdict
        == row.verdict
    )
    evaluator.assert_not_called()


def test_changed_replay_rolls_back_without_evaluation() -> None:
    db, row, args = context({"action": "allow"})
    args["body_digest"] = "c" * 64
    evaluator = MagicMock()
    with pytest.raises(CallbackReplayConflictError):
        crud_callback_receipt.complete_once(db, evaluate=evaluator, **args)
    evaluator.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_called_once()


def test_failed_evaluator_releases_uncommitted_reservation() -> None:
    db, row, args = context()
    with pytest.raises(RuntimeError):
        crud_callback_receipt.complete_once(
            db,
            evaluate=MagicMock(side_effect=RuntimeError("safe synthetic failure")),
            **args,
        )
    db.rollback.assert_called_once()
    db.commit.assert_not_called()
    assert row.verdict is None


def test_integration_owner_is_checked_before_reservation() -> None:
    db, row, args = context()
    db.query.return_value.filter.return_value.first.return_value = None
    with pytest.raises(ValueError, match="belong"):
        crud_callback_receipt.complete_once(db, evaluate=MagicMock(), **args)
    assert db.execute.call_count == 2
    db.rollback.assert_called_once()


@pytest.mark.parametrize(
    "changes",
    [
        {"wait_timeout_ms": 99999},
        {"retention_seconds": 1},
        {"delivery_digest": "personal@example.com"},
    ],
)
def test_invalid_bounds_reject_before_database(changes: dict) -> None:
    db, row, args = context()
    args.update(changes)
    with pytest.raises(ValueError):
        crud_callback_receipt.complete_once(db, evaluate=MagicMock(), **args)
    db.execute.assert_not_called()


def test_lock_and_statement_deadlines_are_explicit() -> None:
    db, row, args = context({"action": "deny"})
    crud_callback_receipt.complete_once(
        db, evaluate=MagicMock(), wait_timeout_ms=1700, **args
    )
    params = [call.args[0].compile().params for call in db.execute.call_args_list[:2]]
    assert any("1700ms" in value.values() for value in params)
    assert any("5000ms" in value.values() for value in params)


def test_pruning_is_bounded_and_skips_other_workers_locks() -> None:
    db = MagicMock(spec=Session)
    db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.with_for_update.return_value.all.return_value = []
    assert crud_callback_receipt.prune(db, now=datetime.now(timezone.utc)) == 0
    db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.with_for_update.assert_called_once_with(
        skip_locked=True
    )
    with pytest.raises(ValueError):
        crud_callback_receipt.prune(db, now=datetime.now(timezone.utc), limit=1001)


def test_schema_uniqueness_includes_tenant_and_integration() -> None:
    constraint = next(
        c
        for c in models.CallbackReceipt.__table__.constraints
        if c.name == "uq_callback_receipt_delivery"
    )
    assert [column.name for column in constraint.columns] == [
        "account_id",
        "integration_id",
        "delivery_digest",
    ]
    assert "body" not in models.CallbackReceipt.__table__.columns


def test_private_callback_drops_error_and_transaction_payloads() -> None:
    from preloop.utils.sentry_filters import (
        register_private_callback_prefix,
        sentry_before_send,
        sentry_before_send_transaction,
    )

    register_private_callback_prefix("/api/v1/example-private-callback/")
    event = {
        "request": {
            "url": "https://example.com/api/v1/example-private-callback/opaque",
            "data": "synthetic private text",
        },
        "exception": {"values": [{"value": "synthetic private text"}]},
    }
    assert sentry_before_send(event, {}) is None
    assert sentry_before_send_transaction(event, {}) is None
    ordinary = {"request": {"url": "https://example.com/api/v1/ordinary"}}
    assert sentry_before_send_transaction(ordinary, {}) is ordinary
    background = {"transaction": "/api/v1/example-private-callback/opaque"}
    assert sentry_before_send(background, {}) is None
    from preloop.utils.sentry_filters import unregister_private_callback_prefix

    unregister_private_callback_prefix("/api/v1/example-private-callback/")
    assert sentry_before_send(background, {}) is background


def test_binding_reuse_and_epoch_drift_roll_back_config() -> None:
    from preloop.models.crud.callback_receipt import (
        CallbackBindingConflictError,
        crud_callback_key_binding,
    )

    for conflict in ["existing_key", "epoch"]:
        db = MagicMock(spec=Session)
        db.query.return_value.filter.return_value.first.side_effect = [
            (uuid4(),),
            (uuid4(),) if conflict == "epoch" else None,
        ]
        db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = (
            SimpleNamespace(id=uuid4()) if conflict == "existing_key" else None
        )
        with pytest.raises(CallbackBindingConflictError):
            crud_callback_key_binding.bind(
                db,
                account_id=uuid4(),
                integration_id=uuid4(),
                signing_key_digest="a" * 64,
                digest_epoch=(
                    "b" * 32 if conflict == "epoch" else callback_digest_epoch()
                ),
                now=datetime.now(timezone.utc),
            )
        db.rollback.assert_called_once()
        db.add.assert_not_called()


def test_previous_key_overlap_is_strictly_bounded() -> None:
    from datetime import timedelta
    from preloop.models.crud.callback_receipt import crud_callback_key_binding

    db = MagicMock(spec=Session)
    now = datetime.now(timezone.utc)
    row = SimpleNamespace(expires_at=now + timedelta(seconds=120))
    db.query.return_value.filter.return_value.first.return_value = row
    args = {
        "account_id": uuid4(),
        "integration_id": uuid4(),
        "signing_key_digest": "a" * 64,
        "digest_epoch": callback_digest_epoch(),
    }
    assert crud_callback_key_binding.assert_usable(db, now=now, **args)
    assert not crud_callback_key_binding.assert_usable(db, now=row.expires_at, **args)


def test_binding_serializes_epoch_and_cross_tenant_registration() -> None:
    from preloop.models.crud.callback_receipt import (
        _CALLBACK_BINDING_LOCK_KEY,
        crud_callback_key_binding,
    )

    db = MagicMock(spec=Session)
    db.query.return_value.filter.return_value.first.side_effect = [(uuid4(),), None]
    db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = None
    now = datetime(2026, 10, 10, 2, tzinfo=timezone(timedelta(hours=2)))
    crud_callback_key_binding.bind(
        db,
        account_id=uuid4(),
        integration_id=uuid4(),
        signing_key_digest="a" * 64,
        digest_epoch=callback_digest_epoch(),
        now=now,
    )
    assert "pg_advisory_xact_lock" in str(db.execute.call_args_list[1].args[0])
    params = db.execute.call_args_list[1].args[0].compile().params
    assert _CALLBACK_BINDING_LOCK_KEY in params.values()
    assert abs(_CALLBACK_BINDING_LOCK_KEY) > 2**32
    assert db.add.call_args.args[0].created_at == datetime(2026, 10, 10)
    db.add.assert_called_once()
    db.flush.assert_called_once()
    db.commit.assert_not_called()


def test_private_callback_namespace_includes_exact_admin_base_path() -> None:
    from preloop.utils.sentry_filters import (
        register_private_callback_prefix,
        sentry_before_send,
        sentry_before_send_transaction,
    )

    register_private_callback_prefix("/api/v1/synthetic-private-integration/")
    event = {
        "request": {
            "url": "https://example.com/api/v1/synthetic-private-integration",
            "data": {"signing_secret": "synthetic-secret"},
        }
    }
    assert sentry_before_send(event, {}) is None
    assert sentry_before_send_transaction(event, {}) is None
    unrelated = {
        "request": {
            "url": "https://example.com/api/v1/synthetic-private-integration-unrelated"
        }
    }
    assert sentry_before_send_transaction(unrelated, {}) is unrelated


@pytest.mark.parametrize("total,unsupported", [(3, 1), (0, 0)])
def test_health_aggregates_are_tenant_scoped_and_content_free(
    total: int, unsupported: int
) -> None:
    db = MagicMock(spec=Session)
    latest = datetime(2026, 10, 10) if total else None
    db.query.return_value.filter.return_value.one.return_value = (total, latest)
    db.query.return_value.filter.return_value.scalar.return_value = unsupported
    account_id, integration_id = uuid4(), uuid4()
    result = crud_callback_receipt.health(
        db, account_id=account_id, integration_id=integration_id
    )
    assert result == {
        "callback_count": total,
        "unsupported_event_count": unsupported,
        "last_callback_at": latest,
    }
    filters = db.query.return_value.filter.call_args_list
    aggregate = select(*db.query.call_args_list[0].args).where(*filters[0].args)
    unsupported_query = select(*db.query.call_args_list[1].args).where(*filters[1].args)
    compiled = unsupported_query.compile(dialect=postgresql.dialect())
    assert "count(callback_receipt.id)" in str(aggregate)
    assert "max(callback_receipt.created_at)" in str(aggregate)
    assert "callback_receipt.evidence ->>" in str(compiled)
    assert {account_id, integration_id, "reason", "unsupported_event"} <= set(
        compiled.params.values()
    )
    db.commit.assert_not_called()


def test_retire_changes_only_scoped_overlap_without_commit() -> None:
    from preloop.models.crud.callback_receipt import crud_callback_key_binding

    db = MagicMock(spec=Session)
    row = SimpleNamespace(expires_at=None)
    db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = row
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(seconds=120)
    account_id, integration_id = uuid4(), uuid4()
    crud_callback_key_binding.retire(
        db,
        account_id=account_id,
        integration_id=integration_id,
        signing_key_digest="a" * 64,
        now=now,
        expires_at=expiry,
    )
    assert row.expires_at == expiry
    filters = db.query.return_value.filter.call_args.args
    compiled = (
        select(models.CallbackKeyBinding)
        .where(*filters)
        .compile(dialect=postgresql.dialect())
    )
    assert {account_id, integration_id, "a" * 64} <= set(compiled.params.values())
    db.query.return_value.filter.return_value.with_for_update.assert_called_once_with()
    db.flush.assert_called_once()
    db.commit.assert_not_called()


def test_empty_verdict_is_not_committed() -> None:
    db, row, args = context()
    with pytest.raises(ValueError, match="completed verdict"):
        crud_callback_receipt.complete_once(
            db, evaluate=MagicMock(return_value=(None, {})), **args
        )
    assert row.verdict is None
    db.commit.assert_not_called()
    db.rollback.assert_called_once()


def test_fingerprint_is_domain_separated() -> None:
    left = callback_fingerprint("webhook", "synthetic-secret")
    right = callback_fingerprint("other", "synthetic-secret")
    assert left != right
    assert left == callback_fingerprint("webhook", "synthetic-secret")
    assert len(left) == 64
    assert len(callback_digest_epoch()) == 32


def test_non_hex_binding_is_rejected_before_database() -> None:
    from preloop.models.crud.callback_receipt import crud_callback_key_binding

    db = MagicMock(spec=Session)
    with pytest.raises(ValueError, match="fingerprint"):
        crud_callback_key_binding.bind(
            db,
            account_id=uuid4(),
            integration_id=uuid4(),
            signing_key_digest="whsec_" + "K" * 58,
            digest_epoch=callback_digest_epoch(),
            now=datetime.now(timezone.utc),
        )
    db.execute.assert_not_called()


def test_retire_rejects_unbounded_overlap_before_lookup() -> None:
    from preloop.models.crud.callback_receipt import (
        CallbackBindingConflictError,
        crud_callback_key_binding,
    )

    db = MagicMock(spec=Session)
    now = datetime.now(timezone.utc)
    with pytest.raises(CallbackBindingConflictError):
        crud_callback_key_binding.retire(
            db,
            account_id=uuid4(),
            integration_id=uuid4(),
            signing_key_digest="a" * 64,
            now=now,
            expires_at=now + timedelta(days=30),
        )
    db.query.assert_not_called()


@pytest.mark.parametrize(
    "prefix", ["/", "/api/", "/api/v1/", "//api/v1/private/", "/api/v1/../"]
)
def test_private_callback_prefix_cannot_disable_global_telemetry(prefix: str) -> None:
    from preloop.utils.sentry_filters import register_private_callback_prefix

    with pytest.raises(ValueError):
        register_private_callback_prefix(prefix)
