"""DB-level updated_at trigger on ai_model (migration 20260806_ai_model_updated_at).

The ORM's onupdate=func.now() only covers ORM-issued UPDATEs. The demonstrated
failure mode (2026-08-06) was a hand-run psql UPDATE flipping the system
default model: updated_at kept reading the previous day, so the audit trail
was wrong. These tests issue raw SQL UPDATEs, exactly the path that bypasses
the ORM, and assert the database itself bumps the timestamp.

Requires the migrated schema (alembic upgrade head), like every other db test.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session


def _create_model_row(db_session: Session, account_id) -> uuid.UUID:
    model_id = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO ai_model "
            "(id, name, provider_name, model_identifier, account_id, is_default, "
            " created_at, updated_at) "
            "VALUES (:id, 'trigger-test', 'openai', 'gpt-test', :account_id, false, "
            " now() - interval '1 day', now() - interval '1 day')"
        ),
        {"id": model_id, "account_id": account_id},
    )
    return model_id


def test_raw_sql_update_bumps_updated_at(db_session: Session, create_account):
    """A raw SQL UPDATE (the manual-psql failure mode) must bump updated_at."""
    account = create_account()
    model_id = _create_model_row(db_session, account.id)

    before = db_session.execute(
        text("SELECT updated_at FROM ai_model WHERE id = :id"), {"id": model_id}
    ).scalar_one()

    db_session.execute(
        text("UPDATE ai_model SET is_default = true WHERE id = :id"),
        {"id": model_id},
    )

    after = db_session.execute(
        text("SELECT updated_at FROM ai_model WHERE id = :id"), {"id": model_id}
    ).scalar_one()

    assert after > before, (
        "raw SQL UPDATE must bump updated_at via the DB trigger; "
        f"before={before!r} after={after!r}"
    )


def test_noop_update_does_not_touch_updated_at(db_session: Session, create_account):
    """An UPDATE that changes nothing must not rewrite the audit timestamp."""
    account = create_account()
    model_id = _create_model_row(db_session, account.id)

    before = db_session.execute(
        text("SELECT updated_at FROM ai_model WHERE id = :id"), {"id": model_id}
    ).scalar_one()

    # Same value as inserted: OLD IS NOT DISTINCT FROM NEW, trigger skipped.
    db_session.execute(
        text("UPDATE ai_model SET is_default = false WHERE id = :id"),
        {"id": model_id},
    )

    after = db_session.execute(
        text("SELECT updated_at FROM ai_model WHERE id = :id"), {"id": model_id}
    ).scalar_one()

    assert after == before, (
        "no-op UPDATE must leave updated_at untouched; "
        f"before={before!r} after={after!r}"
    )


def test_explicit_updated_at_is_overridden_by_trigger(
    db_session: Session, create_account
):
    """Even an explicit stale updated_at in the statement is overridden.

    The trigger runs BEFORE UPDATE and overwrites NEW.updated_at, so a
    hand-written statement cannot accidentally (or deliberately) backdate the
    audit column while changing other fields.
    """
    account = create_account()
    model_id = _create_model_row(db_session, account.id)

    db_session.execute(
        text(
            "UPDATE ai_model SET is_default = true, "
            "updated_at = now() - interval '30 days' WHERE id = :id"
        ),
        {"id": model_id},
    )

    after = db_session.execute(
        text(
            "SELECT updated_at > now() - interval '1 hour' FROM ai_model WHERE id = :id"
        ),
        {"id": model_id},
    ).scalar_one()

    assert after is True, "trigger must overwrite an explicitly backdated updated_at"


def test_trigger_is_installed():
    """The trigger and function exist in the migrated schema (pure catalog read)."""
    import os

    import sqlalchemy

    engine = sqlalchemy.create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        trigger_count = conn.execute(
            text(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgname = 'trg_ai_model_set_updated_at' AND NOT tgisinternal"
            )
        ).scalar_one()
    engine.dispose()

    assert trigger_count == 1, (
        "trg_ai_model_set_updated_at must exist on ai_model (run alembic upgrade head)"
    )
