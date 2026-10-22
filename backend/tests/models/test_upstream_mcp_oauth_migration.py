"""The additive lifecycle migration leaves legacy MCP integrations intact."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from preloop.models import models


def test_upstream_oauth_upgrade_downgrade_preserves_legacy(db_session, create_account):
    account = create_account()
    legacy = models.MCPServer(
        account_id=account.id,
        name="Synthetic legacy",
        url="https://resource.example.com/mcp",
        auth_type="oauth",
        auth_config={"access_token": "synthetic-legacy"},
    )
    db_session.add(legacy)
    db_session.flush()
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261110_upstream_mcp_oauth.py"
    )
    spec = importlib.util.spec_from_file_location("upstream_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection = db_session.connection()
    migration.op = Operations(MigrationContext.configure(connection))
    migration.downgrade()
    assert "upstream_mcp_oauth_transaction" not in inspect(connection).get_table_names()
    for _ in range(2):
        migration.upgrade()
        assert "upstream_mcp_oauth_transaction" in inspect(connection).get_table_names()
        migration.downgrade()
    migration.upgrade()
    db_session.refresh(legacy)
    assert legacy.auth_config == {"access_token": "synthetic-legacy"}
