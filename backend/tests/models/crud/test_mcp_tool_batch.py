"""Execute batch tool reads against an isolated in-memory database."""

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import JSON, Column, MetaData, Table, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.mcp_tool import CRUDMCPTool


@pytest.mark.parametrize("server_count", [1, 20])
def test_batch_reads_only_selected_account_servers_in_one_query(
    server_count: int,
) -> None:
    engine = create_engine("sqlite://")
    metadata = MetaData()
    Table(
        "account",
        metadata,
        Column("id", models.MCPServer.account_id.type, primary_key=True),
    )
    server_table = models.MCPServer.__table__.to_metadata(metadata)
    tool_table = models.MCPTool.__table__.to_metadata(metadata)
    # SQLite exercises the ORM's joins/bindings without live database access.
    for table in (server_table, tool_table):
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    account_id, other_account_id = uuid4(), uuid4()
    selected_ids = [uuid4() for _ in range(server_count)]
    excluded_id, foreign_id = uuid4(), uuid4()
    rows = [(server_id, account_id) for server_id in selected_ids]
    rows.extend([(excluded_id, account_id), (foreign_id, other_account_id)])
    with engine.begin() as connection:
        connection.execute(
            server_table.insert(),
            [
                {
                    "id": sid,
                    "account_id": owner,
                    "name": str(sid),
                    "url": "https://example.com",
                }
                for sid, owner in rows
            ],
        )
        connection.execute(
            tool_table.insert(),
            [
                {
                    "id": uuid4(),
                    "mcp_server_id": sid,
                    "name": str(sid),
                    "input_schema": {"type": "object"},
                    "discovered_at": "2026-01-01",
                }
                for sid, _ in rows
            ],
        )
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def record_statement(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    with Session(engine) as session:
        result = CRUDMCPTool().get_by_servers_for_account(
            session,
            account_id=str(account_id),
            server_ids=[*selected_ids, foreign_id],
        )
        assert {row.mcp_server_id for row in result} == set(selected_ids)
        assert len(statements) == 1
        statements.clear()
        assert (
            CRUDMCPTool().get_by_servers_for_account(
                session, account_id=str(account_id), server_ids=[]
            )
            == []
        )
        assert statements == []
    engine.dispose()
