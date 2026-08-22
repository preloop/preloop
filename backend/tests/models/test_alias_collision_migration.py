"""Execute the alias-collision audit migration against a real Postgres DB.

`test_alembic_single_head.py` only checks the revision graph; nothing there
runs `upgrade()`. This matters because the migration's data-rewrite path
binds a JSON string into the JSONB ``meta_data`` column — a statement that
only executes when a collision exists, i.e. on exactly the accounts the
migration was written to fix — so a driver-level type error (text -> jsonb
needs an explicit cast) would abort ``alembic upgrade head`` at deploy time
while every graph test stayed green. These tests seed a collision and run
the migration's ``upgrade()`` through the psycopg driver used in production.
"""

import importlib.util
from datetime import datetime
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations

from preloop.models.crud import crud_ai_model
from preloop.services.model_runtime_resolver import effective_gateway_alias

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop"
    / "models"
    / "alembic"
    / "versions"
    / "20260822_gateway_alias_collision_audit.py"
)


def _load_migration():
    """Import the migration module by path (its name starts with digits)."""
    spec = importlib.util.spec_from_file_location(
        "alias_collision_audit_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(db_session) -> None:
    """Run the migration's ``upgrade()`` inside the test transaction."""
    migration = _load_migration()
    context = MigrationContext.configure(db_session.connection())
    with Operations.context(context):
        migration.upgrade()


def _payload(name: str, *, alias: str, managed_by: str | None = None) -> dict:
    meta: dict = {
        "gateway": {
            "enabled": True,
            "model_alias": alias,
            "provider_adapter": "preloop",
        }
    }
    if managed_by:
        meta["managed_by"] = managed_by
    return {
        "name": name,
        "provider_name": "zai",
        "model_identifier": "glm-5.3",
        "api_key": "test-key",
        "meta_data": meta,
    }


def _force_alias(db_session, model, alias: str) -> None:
    """Recreate the legacy collision shape (write-time checks now prevent it)."""
    model.meta_data = {
        **model.meta_data,
        "gateway": {**model.meta_data["gateway"], "model_alias": alias},
    }
    db_session.flush()


def test_upgrade_realiases_colliding_import_on_postgres(db_session, test_user):
    """User keeps the alias; the import is rewritten via a real JSONB UPDATE."""
    user_created = crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_payload("glm-5.3", alias="zai/glm-5.3"),
        account_id=test_user.account_id,
    )
    imported = crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_payload(
            "OpenCode zai/glm-5-3-turbo",
            alias="zai/glm-5.3-tmp",
            managed_by="preloop agents onboard",
        ),
        account_id=test_user.account_id,
    )
    _force_alias(db_session, imported, "zai/glm-5.3")

    _run_upgrade(db_session)

    db_session.expire_all()
    assert effective_gateway_alias(user_created) == "zai/glm-5.3"
    assert effective_gateway_alias(imported) == "zai/glm-5.3-2"
    # The rewrite must only touch the alias, not the rest of meta_data.
    assert imported.meta_data["managed_by"] == "preloop agents onboard"
    assert imported.meta_data["gateway"]["enabled"] is True


def test_upgrade_import_only_collision_keeps_oldest_on_postgres(db_session, test_user):
    """With no user-created row the oldest import keeps the alias."""
    first_import = crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_payload(
            "import one", alias="zai/glm-5.3", managed_by="preloop agents onboard"
        ),
        account_id=test_user.account_id,
    )
    second_import = crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_payload(
            "import two",
            alias="zai/glm-5.3-tmp",
            managed_by="preloop agents onboard",
        ),
        account_id=test_user.account_id,
    )
    _force_alias(db_session, second_import, "zai/glm-5.3")
    # Rows created inside one test transaction share a transaction-fixed
    # ``now()`` timestamp, which would leave "oldest" to the random-UUID
    # tiebreak. Real onboarding runs produce distinct timestamps, so pin
    # them explicitly to make "oldest import" deterministic here.
    first_import.created_at = datetime(2026, 8, 1, 12, 0, 0)
    second_import.created_at = datetime(2026, 8, 2, 12, 0, 0)
    db_session.flush()

    _run_upgrade(db_session)

    db_session.expire_all()
    assert effective_gateway_alias(first_import) == "zai/glm-5.3"
    assert effective_gateway_alias(second_import) == "zai/glm-5.3-2"


def test_upgrade_reports_but_never_rewrites_user_only_collision_on_postgres(
    db_session, test_user, caplog
):
    """User-vs-user collisions are audited, not silently renamed."""
    first = crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_payload("user one", alias="zai/glm-5.3"),
        account_id=test_user.account_id,
    )
    second = crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_payload("user two", alias="zai/glm-5.3-tmp"),
        account_id=test_user.account_id,
    )
    _force_alias(db_session, second, "zai/glm-5.3")

    with caplog.at_level("WARNING"):
        _run_upgrade(db_session)

    db_session.expire_all()
    assert effective_gateway_alias(first) == "zai/glm-5.3"
    assert effective_gateway_alias(second) == "zai/glm-5.3"
    assert any(
        "gateway_alias_collision_audit" in record.message
        and "UNRESOLVED" in (record.getMessage())
        for record in caplog.records
    )
