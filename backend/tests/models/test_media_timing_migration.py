"""The receipt backfill uses creation time and preserves already-known receipt times."""

from typing import Any

import importlib
from datetime import datetime, timezone

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from preloop.models import models
from tests.api.test_artifact_deposit import _session


def test_backfill_and_round_trip(db_session: Any, test_user: Any) -> None:
    migration = importlib.import_module(
        "preloop.models.alembic.versions.20261110_media_timing"
    )
    session = _session(db_session, test_user.account_id, "timing-migration")
    created = datetime(2026, 10, 1, 0, 0, 30)
    occurred = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rows = [
        models.RuntimeSessionActivity(
            account_id=test_user.account_id,
            runtime_session_id=session.id,
            activity_type="browser_step",
            timestamp=occurred,
            created_at=created,
            metadata_=metadata,
        )
        for metadata in (
            {"source_step_id": "old"},
            {"source_step_id": "new", "recorded_at": "2026-10-01T00:00:45Z"},
        )
    ]
    db_session.add_all(rows)
    db_session.flush()
    context = MigrationContext.configure(db_session.connection())
    with Operations.context(context):
        # Drop only the additive column to simulate pre-upgrade state. Keep
        # known receipt data to prove that rerunning a backfill preserves it.
        from alembic import op

        op.drop_column("runtime_session_artifact", "media_timing")
        migration.upgrade()
    db_session.expire_all()
    assert rows[0].metadata_["recorded_at"] == "2026-10-01T00:00:30.000000Z"
    assert rows[1].metadata_["recorded_at"] == "2026-10-01T00:00:45Z"
    assert rows[0].timestamp == occurred
    with Operations.context(context):
        migration.downgrade()
        migration.upgrade()
    columns = {
        col["name"]: col
        for col in inspect(db_session.connection()).get_columns(
            "runtime_session_artifact"
        )
    }
    assert columns["media_timing"]["nullable"]
