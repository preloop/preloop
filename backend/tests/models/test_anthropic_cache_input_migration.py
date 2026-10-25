"""The Anthropic cache-inclusive input backfill is exact and reversible (#1955)."""

import importlib.util
import json
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, text

ANTHROPIC = {
    "input_tokens": 100,
    "output_tokens": 50,
    "cache_read_input_tokens": 10000,
    "cache_creation_input_tokens": 2000,
}

ROWS = {
    # name: (action_type, usage_source, prompt, total, usage_details)
    "anthropic": ("model_gateway", "provider", 100, 150, ANTHROPIC),
    # litellm-shaped usage already counts the cache in prompt_tokens.
    "openai_shape": (
        "model_gateway",
        "provider",
        12100,
        12150,
        {**ANTHROPIC, "prompt_tokens": 12100},
    ),
    # Already cache inclusive (a row written after the gateway fix).
    "already_inclusive": ("model_gateway", "provider", 12100, 12150, ANTHROPIC),
    "no_cache": (
        "model_gateway",
        "provider",
        100,
        150,
        {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0},
    ),
    "imported": ("imported_usage", "imported", 100, 150, ANTHROPIC),
}


def _load():
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261106_anthropic_cache_inclusive_input.py"
    )
    spec = importlib.util.spec_from_file_location("anthropic_cache_input", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _tokens(connection) -> dict:
    return {
        name: (prompt, total)
        for name, prompt, total in connection.execute(
            text("SELECT endpoint, prompt_tokens, total_tokens FROM api_usage")
        )
    }


def test_backfill_upgrade_is_idempotent_and_downgrade_reverts(
    db_engine: Engine,
) -> None:
    migration = _load()
    schema = "anthropic_cache_input_test_" + uuid4().hex
    with db_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        try:
            connection.execute(text(f"SET search_path TO {schema}"))
            connection.execute(
                text(
                    "CREATE TABLE api_usage (id uuid primary key, endpoint text, "
                    "action_type varchar(50), usage_source varchar(16), "
                    "prompt_tokens integer, total_tokens integer, meta_data jsonb)"
                )
            )
            for name, (action, source, prompt, total, details) in ROWS.items():
                connection.execute(
                    text(
                        "INSERT INTO api_usage VALUES (:id, :name, :action, "
                        ":source, :prompt, :total, CAST(:meta AS jsonb))"
                    ),
                    {
                        "id": str(uuid4()),
                        "name": name,
                        "action": action,
                        "source": source,
                        "prompt": prompt,
                        "total": total,
                        "meta": json.dumps({"usage_details": details}),
                    },
                )
            before = _tokens(connection)
            connection.commit()
            context = MigrationContext.configure(connection)
            with context.begin_transaction(), Operations.context(context):
                migration.upgrade()
                migration.upgrade()
            after = _tokens(connection)
            assert after["anthropic"] == (12100, 12150)
            for name in ("openai_shape", "already_inclusive", "no_cache", "imported"):
                assert after[name] == before[name], name

            with context.begin_transaction(), Operations.context(context):
                migration.downgrade()
            assert _tokens(connection) == before
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM api_usage "
                        "WHERE meta_data ? 'input_cache_backfill'"
                    )
                ).scalar_one()
                == 0
            )
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
            connection.commit()
