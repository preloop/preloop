"""Recover stranded hosted reservations from their measured usage rows."""

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy import select

from preloop.models import models
from preloop.models.crud import hosted_spend as ledger
from preloop.services import hosted_reservation_recovery as recovery

BASE = datetime(2026, 10, 8, 11, 0, 0)
RESERVED = Decimal("0.339")


@pytest.fixture
def hosted_model(db_session):
    model = models.AIModel(
        name="Hosted flash",
        provider_name="openai",
        model_identifier="flash",
        account_id=None,
        meta_data={
            "hosted": True,
            "hosted_metering": {
                "input_usd_per_million": "0.3",
                "output_usd_per_million": "1.2",
                "request_usd": "0",
            },
        },
    )
    db_session.add(model)
    db_session.flush()
    return model


@pytest.fixture
def account(db_session, test_user):
    account_id = test_user.account_id
    ledger.establish_baseline(
        db_session,
        account_id=account_id,
        lifetime_spent=0,
        month_spent=0,
        now=datetime.now(UTC),
        evidence="test baseline",
    )
    db_session.flush()
    return account_id


def _reservation(db_session, account_id, at, *, status="recovery_required"):
    row = ledger.reserve(
        db_session,
        account_id=account_id,
        operation_key=f"op-{at.isoformat()}-{status}",
        amount=RESERVED,
        lifetime_limit=None,
        monthly_limit=None,
        now=datetime.now(UTC),
    )
    if status != "reserved":
        ledger.mark_dispatched(db_session, account_id=account_id, reservation_id=row.id)
    if status == "recovery_required":
        ledger.settle(
            db_session, account_id=account_id, reservation_id=row.id, actual=None
        )
    row.created_at = at
    db_session.flush()
    return row


def _usage(db_session, account_id, model, ended, duration, prompt, completion):
    usage = models.ApiUsage(
        account_id=account_id,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=duration,
        ai_model_id=model.id,
        prompt_tokens=prompt,
        completion_tokens=completion,
    )
    db_session.add(usage)
    db_session.flush()
    usage.created_at = ended
    db_session.flush()
    return usage


def test_measured_cost_matches_hosted_call_formula():
    tariff = {
        "input_usd_per_million": Decimal("0.3"),
        "output_usd_per_million": Decimal("1.2"),
        "request_usd": Decimal("0"),
    }
    assert recovery.measured_cost(tariff, 85_000, 900) == Decimal("0.02658")


def test_dry_run_matches_and_apply_settles_with_audit(
    db_session, account, hosted_model
):
    stuck = _reservation(db_session, account, BASE + timedelta(seconds=1))
    usage = _usage(
        db_session, account, hosted_model, BASE + timedelta(seconds=40), 40, 85_000, 900
    )

    plan = recovery.recover(
        db_session, account_id=account, hosted_model_id=hosted_model.id
    )
    assert [
        (e.reservation_id, e.api_usage_id, e.actual_usd, e.outcome) for e in plan
    ] == [(str(stuck.id), str(usage.id), "0.0265800000", "would_settle")]
    db_session.refresh(stuck)
    assert stuck.status == "recovery_required"

    plan = recovery.recover(
        db_session, account_id=account, hosted_model_id=hosted_model.id, apply=True
    )
    assert plan[0].outcome == "settled"
    db_session.refresh(stuck)
    assert stuck.status == "settled"
    assert stuck.actual == Decimal("0.02658")
    snapshot = ledger.snapshot(db_session, account_id=account, now=datetime.now(UTC))
    assert snapshot["lifetime_reserved"] == 0
    audit = db_session.scalar(
        select(models.BillingOperation).where(
            models.BillingOperation.operation_key == f"hosted-recovery:{stuck.id}"
        )
    )
    assert str(usage.id) in audit.payload["evidence"]


def test_unmeasured_and_ambiguous_rows_need_manual_review(
    db_session, account, hosted_model
):
    # No usage row at all.
    orphan = _reservation(db_session, account, BASE)
    # Two attempts (a retry) inside one usage row.
    first = _reservation(db_session, account, BASE + timedelta(minutes=10))
    retry = _reservation(db_session, account, BASE + timedelta(minutes=10, seconds=3))
    _usage(
        db_session, account, hosted_model, BASE + timedelta(minutes=11), 70, 1000, 10
    )
    # A usage row that recorded no tokens.
    blind = _reservation(db_session, account, BASE + timedelta(minutes=20))
    _usage(
        db_session, account, hosted_model, BASE + timedelta(minutes=21), 70, None, None
    )
    # A call already settled still competes for the usage row it paid for.
    paid = _reservation(
        db_session, account, BASE + timedelta(minutes=30), status="dispatched"
    )
    ledger.settle(
        db_session, account_id=account, reservation_id=paid.id, actual="0.001"
    )
    late = _reservation(db_session, account, BASE + timedelta(minutes=30, seconds=2))
    _usage(
        db_session, account, hosted_model, BASE + timedelta(minutes=31), 70, 1000, 10
    )
    in_flight = _reservation(
        db_session, account, BASE + timedelta(minutes=40), status="dispatched"
    )

    plan = {
        e.reservation_id: e
        for e in recovery.recover(
            db_session, account_id=account, hosted_model_id=hosted_model.id, apply=True
        )
    }

    for row in (orphan, first, retry, blind, late, in_flight):
        assert plan[str(row.id)].outcome == recovery.MANUAL_REVIEW
        assert plan[str(row.id)].actual_usd is None
        db_session.refresh(row)
        assert row.status in {"recovery_required", "dispatched"}
    assert "other reservation" in plan[str(first.id)].reason
    assert "no measured token" in plan[str(blind.id)].reason
    assert "other reservation" in plan[str(late.id)].reason
    assert "in flight" in plan[str(in_flight.id)].reason


def test_account_model_is_refused_as_hosted_row(db_session, account, test_user):
    own = models.AIModel(
        name="Own",
        provider_name="openai",
        model_identifier="flash",
        account_id=test_user.account_id,
        meta_data={},
    )
    db_session.add(own)
    db_session.flush()
    with pytest.raises(ValueError):
        recovery.plan_recovery(db_session, account_id=account, hosted_model_id=own.id)


def test_script_prints_dry_run_table(db_session, account, hosted_model, monkeypatch):
    stuck = _reservation(db_session, account, BASE + timedelta(seconds=1))
    _usage(
        db_session, account, hosted_model, BASE + timedelta(seconds=40), 40, 85_000, 900
    )
    path = (
        Path(__file__).resolve().parents[3] / "scripts/recover_hosted_reservations.py"
    )
    spec = importlib.util.spec_from_file_location("recover_hosted_reservations", path)
    script = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = script
    spec.loader.exec_module(script)

    class _Shared:
        def __getattr__(self, name):
            return getattr(db_session, name)

        def commit(self):
            db_session.flush()

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(script, "get_db_session", lambda: iter([_Shared()]))
    result = CliRunner().invoke(
        script.main,
        ["--account-id", str(account), "--hosted-model-id", str(hosted_model.id)],
    )
    assert result.exit_code == 0, result.output
    assert str(stuck.id) in result.output
    assert "DRY RUN: 1 unresolved, 1 matched" in result.output
    assert "0 needs manual review" in result.output
