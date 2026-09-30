"""Execute PostgreSQL aggregate SQL against synthetic in-memory relations.

The PostgreSQL integration suite covers schema-backed persistence separately.
This small SQL harness exercises window ranking and join cardinality without
requiring a database service, including more sessions than a console page.
"""

import re
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_mock_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Query, Session

from preloop.models.crud import crud_api_usage


START = datetime(2026, 8, 1, 9, 0, 0, 123456, tzinfo=UTC)
END = START + timedelta(days=7)
ACCOUNT = "00000000-0000-4000-8000-000000000001"
FOREIGN = "00000000-0000-4000-8000-000000000002"


def test_full_window_aggregate_sql_attribution_ranking_and_reconciliation() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.create_function(
        "concat", -1, lambda *values: "".join(str(value or "") for value in values)
    )
    conn.create_function("json_build_array", -1, lambda *values: json.dumps(values))
    conn.executescript("""
    CREATE TABLE api_usage (id TEXT, account_id TEXT, timestamp TEXT,
        action_type TEXT, meta_data TEXT, runtime_session_id TEXT,
        runtime_principal_type TEXT, runtime_principal_id TEXT,
        runtime_principal_name TEXT, ai_model_id TEXT, model_alias TEXT,
        provider_name TEXT, flow_id TEXT);
    CREATE TABLE managed_agent (id TEXT, account_id TEXT, session_source_type TEXT,
        session_source_id TEXT, runtime_session_id TEXT, display_name TEXT);
    CREATE TABLE runtime_session (id TEXT, account_id TEXT,
        runtime_principal_type TEXT, runtime_principal_id TEXT, runtime_principal_name TEXT);
    CREATE TABLE ai_model (id TEXT, account_id TEXT, name TEXT);
    """)
    conn.execute(
        "INSERT INTO managed_agent VALUES (?, ?, 'example', 'leading', NULL, 'Same name')",
        ("agent-a", ACCOUNT),
    )
    conn.execute(
        "INSERT INTO managed_agent VALUES (?, ?, 'example', 'session-owner', 'session-own', 'Same name')",
        ("agent-b", ACCOUNT),
    )
    conn.execute(
        "INSERT INTO managed_agent VALUES (?, ?, 'example', 'duplicate', 'session-own', 'Duplicate')",
        ("agent-c", ACCOUNT),
    )
    conn.execute(
        "INSERT INTO managed_agent VALUES (?, ?, 'example', 'foreign', 'session-foreign', 'Foreign agent')",
        ("agent-d", FOREIGN),
    )
    conn.execute(
        "INSERT INTO runtime_session VALUES ('session-own', ?, NULL, NULL, NULL)",
        (ACCOUNT,),
    )
    conn.execute(
        "INSERT INTO runtime_session VALUES ('session-foreign', ?, 'example', 'foreign', 'Foreign agent')",
        (FOREIGN,),
    )
    conn.execute(
        "INSERT INTO ai_model VALUES ('model-foreign', ?, 'Foreign model')", (FOREIGN,)
    )
    conn.execute(
        "INSERT INTO ai_model VALUES ('model-own', ?, 'Fallback model')", (ACCOUNT,)
    )

    def usage(**overrides: object) -> None:
        values = dict(
            id=str(uuid4()),
            account_id=ACCOUNT,
            timestamp=str(START),
            action_type="model_gateway",
            meta_data=None,
            runtime_session_id=None,
            runtime_principal_type=None,
            runtime_principal_id=None,
            runtime_principal_name=None,
            ai_model_id=None,
            model_alias="model-a",
            provider_name="example",
            flow_id=None,
        )
        values.update(overrides)
        conn.execute(
            "INSERT INTO api_usage VALUES (" + ",".join("?" for _ in values) + ")",
            tuple(values.values()),
        )

    for index in range(260):
        conn.execute(
            "INSERT INTO runtime_session VALUES (?, ?, NULL, NULL, NULL)",
            (f"session-{index}", ACCOUNT),
        )
        usage(
            runtime_session_id=f"session-{index}",
            runtime_principal_type="example",
            runtime_principal_id="leading",
            flow_id="flow-example" if index % 2 else None,
        )
    usage(
        runtime_session_id="session-own",
        runtime_principal_type="example",
        runtime_principal_id="leading",
    )
    usage(runtime_session_id="session-own", model_alias="model-b")
    usage(
        runtime_session_id="session-foreign",
        runtime_principal_type="example",
        runtime_principal_id="foreign",
        model_alias=None,
        ai_model_id="model-foreign",
    )
    usage(
        model_alias=None,
        ai_model_id="model-own",
        runtime_principal_type="named",
        runtime_principal_id="1",
        runtime_principal_name="Named agent",
    )
    usage(
        model_alias=None,
        ai_model_id="model-own",
        runtime_principal_type="named",
        runtime_principal_id="1",
        runtime_principal_name="Named agent",
    )
    usage(
        model_alias="model-c",
        runtime_principal_type="named",
        runtime_principal_id="2",
        runtime_principal_name="Named agent",
    )
    usage(model_alias="model-d")
    usage(model_alias="excluded", timestamp=str(END))
    usage(model_alias="excluded", timestamp=str(START - timedelta(microseconds=1)))
    usage(model_alias="excluded", action_type="imported")
    usage(model_alias="excluded", meta_data='{"purpose":"replay_validation"}')
    usage(model_alias="excluded", account_id=FOREIGN)
    statements: list[str] = []

    def execute(query: Query) -> list[SimpleNamespace]:
        compiled = query.statement.compile(dialect=postgresql.dialect())
        sql = re.sub(r"%\(([^)]+)\)s", r":\1", str(compiled)).replace("::UUID", "")
        params = {
            key: str(value) if isinstance(value, datetime) else value
            for key, value in compiled.params.items()
        }
        statements.append(sql)
        return [SimpleNamespace(**dict(row)) for row in conn.execute(sql, params)]

    db = Session(bind=create_mock_engine("postgresql://", lambda *args: None))
    with patch.object(Query, "all", execute):
        agents = crud_api_usage.get_gateway_usage_by_agent(
            db, account_id=ACCOUNT, start_date=START, end_date=END
        )
        models = crud_api_usage.get_gateway_usage_by_model(
            db, account_id=ACCOUNT, start_date=START, end_date=END, digest_ranking=True
        )
    assert len(statements) == 2
    assert agents[0]["agent_id"] == "agent-a"
    assert agents[0]["request_count"] == 261
    assert agents[-1]["total_count"] == 267
    assert agents[-1]["request_count"] == 2
    assert agents[-1]["other_count"] == 1
    assert agents[1]["name"] != agents[0]["name"]
    assert models[0]["name"] == "model-a"
    assert models[0]["request_count"] == 261
    assert models[1]["name"] == "Fallback model"
    assert models[2]["name"] == "model-b"
    assert models[-1]["request_count"] == 1
    assert models[-1]["other_count"] == 2
    for rows in (agents, models):
        assert (
            sum(row["request_count"] for row in rows) + rows[-1]["other_count"] == 267
        )
    assert all(
        row["name"] not in ("Foreign agent", "Foreign model", "excluded")
        for row in agents + models
    )
    # Unknown identities never occupy one of the three named slots. A
    # harness/source without a named identity remains unattributed.
    conn.execute("DELETE FROM api_usage")
    usage(model_alias=None, runtime_principal_type="example")
    usage(
        model_alias=None,
        runtime_principal_id="orphan",
        runtime_principal_name="Incomplete",
    )
    with patch.object(Query, "all", execute):
        unknown_agents = crud_api_usage.get_gateway_usage_by_agent(
            db, account_id=ACCOUNT, start_date=START, end_date=END
        )
        unknown_models = crud_api_usage.get_gateway_usage_by_model(
            db, account_id=ACCOUNT, start_date=START, end_date=END, digest_ranking=True
        )
    for rows in (unknown_agents, unknown_models):
        assert len(rows) == 1
        assert rows[0]["name"] is None
        assert rows[0]["request_count"] == rows[0]["total_count"] == 2
        assert rows[0]["other_count"] == 0

    # Null and empty aliases retain the existing tuple identity even when
    # both fall back to the same stored model name. Provider identity stays
    # separate too, rather than being merged by a visible display label.
    conn.execute("DELETE FROM api_usage")
    usage(model_alias=None, ai_model_id="model-own")
    usage(model_alias="", ai_model_id="model-own")
    usage(model_alias="same", provider_name="provider-a")
    usage(model_alias="same", provider_name="provider-b")
    with patch.object(Query, "all", execute):
        aliases = crud_api_usage.get_gateway_usage_by_model(
            db, account_id=ACCOUNT, start_date=START, end_date=END, digest_ranking=True
        )
    assert len({row["identity"] for row in aliases if row["name"] is not None}) == 3
    assert aliases[-1]["other_count"] == 1
    assert aliases[-1]["request_count"] == 0
    assert aliases[-1]["total_count"] == 4
    assert aliases[0]["name"] != aliases[1]["name"]
    db.close()
    conn.close()
