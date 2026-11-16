"""Persist flow webhook rotations and preserve credentials across upgrades."""

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import models, schemas
from preloop.models.crud import crud_flow

SECRET = "synthetic-existing-webhook-secret"


def _create(db: Session, user: models.User) -> models.Flow:
    """Create an existing webhook flow using the storage write schema."""
    return crud_flow.create(
        db,
        account_id=user.account_id,
        flow_in=schemas.FlowCreate(
            name="Inbound events",
            agent_config={},
            prompt_template="Handle the event",
            trigger_event_source="webhook",
            trigger_event_types=["webhook"],
            webhook_config={
                "webhook_secret": SECRET,
                "dedupe_path": "event.id",
                "supersede_on_update": True,
            },
        ),
    )


def test_rotation_persists_and_redacted_settings_round_trip(
    db_session: Session, test_user: models.User, client: TestClient
) -> None:
    """Exercise real CRUD commits behind API serialization and partial updates."""
    flow = _create(db_session, test_user)
    assert flow.webhook_secret_rotated_at is None
    base = f"/api/v1/flows/{flow.id}"
    fetched = client.get(base)
    assert fetched.status_code == 200, fetched.text
    assert SECRET not in fetched.text
    response = client.put(
        base, json={"webhook_config": fetched.json()["webhook_config"]}
    )
    assert response.status_code == 200, response.text
    db_session.refresh(flow)
    assert flow.webhook_config["webhook_secret"] == SECRET
    rotated = client.post(f"{base}/webhook-secret/rotate")
    assert rotated.status_code == 200, rotated.text
    new_secret = rotated.json()["webhook_secret"]
    db_session.expire_all()
    stored = crud_flow.get(db_session, id=flow.id, account_id=test_user.account_id)
    assert stored.webhook_config == {
        **fetched.json()["webhook_config"],
        "webhook_secret": new_secret,
        "employee_secret": None,
    }
    assert stored.webhook_secret_rotated_at is not None
    after = client.get(base)
    assert new_secret not in after.text and SECRET not in after.text
    assert after.json()["webhook_secret_hint"] == new_secret[-4:]
    assert (
        client.post(f"/api/v1/webhooks/flows/{flow.id}/{SECRET}", json={}).status_code
        == 403
    )


def _migration() -> ModuleType:
    """Load the migration module without relying on its numeric module name."""
    path = (
        Path(__file__).resolve().parents[3]
        / "preloop/models/alembic/versions/20261210_flow_webhook_rotation.py"
    )
    spec = importlib.util.spec_from_file_location("flow_webhook_rotation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_preserves_existing_secret(
    db_session: Session, test_user: models.User
) -> None:
    """The upgrade adds metadata only, leaving existing encrypted JSON intact."""
    flow = _create(db_session, test_user)
    migration = _migration()
    operations = Operations(MigrationContext.configure(db_session.connection()))
    original = migration.op
    migration.op = operations
    try:
        migration.downgrade()
        migration.upgrade()
    finally:
        migration.op = original
    db_session.refresh(flow)
    assert flow.webhook_config["webhook_secret"] == SECRET
    assert flow.webhook_config["dedupe_path"] == "event.id"
    assert flow.webhook_secret_rotated_at is None
