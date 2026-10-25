from datetime import datetime, timedelta, timezone

from preloop.models.crud import (
    crud_ai_model,
    crud_api_usage,
    crud_flow,
    crud_flow_execution,
    crud_gateway_usage_search_document,
    crud_runtime_session,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate


def test_search_account_documents_isolates_runtime_session_with_legacy_fallback(
    db_session, test_user
):
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_key": "provider-secret",
        },
        account_id=test_user.account_id,
    )
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Gateway Search Flow",
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
            ai_model_id=ai_model.id,
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(flow_id=flow.id, status="RUNNING"),
    )
    selected_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="flow_execution",
        session_source_id=str(execution.id),
        session_reference="selected-session",
        runtime_principal_type="flow_execution",
        runtime_principal_id=str(execution.id),
        runtime_principal_name="Gateway Search Flow",
        started_at=execution.start_time,
        last_activity_at=execution.start_time,
    )
    sibling_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="custom",
        session_source_id="sibling-session",
        session_reference="sibling-session",
        runtime_principal_type="flow_execution",
        runtime_principal_id=str(execution.id),
        runtime_principal_name="Gateway Search Flow",
        started_at=execution.start_time,
        last_activity_at=execution.start_time,
    )

    selected_usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        ai_model_id=str(ai_model.id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        runtime_session_id=str(selected_session.id),
        model_alias="openai/gpt-5.4",
        provider_name="openai",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        estimated_cost=0.1,
    )
    sibling_usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        ai_model_id=str(ai_model.id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        runtime_session_id=str(sibling_session.id),
        model_alias="openai/gpt-5.4",
        provider_name="openai",
        prompt_tokens=20,
        completion_tokens=10,
        total_tokens=30,
        estimated_cost=0.2,
    )
    legacy_usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        ai_model_id=str(ai_model.id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        model_alias="openai/gpt-5.4",
        provider_name="openai",
        prompt_tokens=30,
        completion_tokens=15,
        total_tokens=45,
        estimated_cost=0.3,
    )

    crud_gateway_usage_search_document.upsert_for_api_usage(
        db_session,
        api_usage=selected_usage,
        searchable_text="selected runtime session interaction",
    )
    crud_gateway_usage_search_document.upsert_for_api_usage(
        db_session,
        api_usage=sibling_usage,
        searchable_text="sibling runtime session interaction",
    )
    crud_gateway_usage_search_document.upsert_for_api_usage(
        db_session,
        api_usage=legacy_usage,
        searchable_text="legacy unbound interaction",
    )

    start_date = datetime.now(timezone.utc) - timedelta(days=1)
    end_date = datetime.now(timezone.utc) + timedelta(days=1)

    result = crud_gateway_usage_search_document.search_account_documents(
        db_session,
        account_id=str(test_user.account_id),
        start_date=start_date,
        end_date=end_date,
        runtime_session_id=str(selected_session.id),
        flow_execution_id=str(execution.id),
        limit=20,
        offset=0,
    )

    returned_usage_ids = {item["api_usage_id"] for item in result["items"]}

    assert result["total"] == 2
    assert str(selected_usage.id) in returned_usage_ids
    assert str(legacy_usage.id) in returned_usage_ids
    assert str(sibling_usage.id) not in returned_usage_ids

    sibling_result = crud_gateway_usage_search_document.search_account_documents(
        db_session,
        account_id=str(test_user.account_id),
        start_date=start_date,
        end_date=end_date,
        runtime_session_id=str(sibling_session.id),
        flow_execution_id=str(execution.id),
        limit=20,
        offset=0,
    )

    sibling_usage_ids = {item["api_usage_id"] for item in sibling_result["items"]}

    assert sibling_result["total"] == 2
    assert str(sibling_usage.id) in sibling_usage_ids
    assert str(selected_usage.id) not in sibling_usage_ids
    assert str(legacy_usage.id) in sibling_usage_ids


def test_usage_meta_keys_picks_only_those_keys_in_sql(db_session, test_user):
    """The activity timeline reads seven keys, not the capture (#1689).

    A usage row's metadata carries the captured request and response; on a
    long session 50 timeline rows read 5.8 MB on prod. With
    ``usage_meta_keys`` only the named keys leave the database, and the
    deferred column is never loaded back row by row.
    """
    from sqlalchemy import event, text

    usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        model_alias="openai/gpt-5.4",
        provider_name="openai",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        estimated_cost=0.1,
        meta_data={
            "gateway_attempt": 2,
            "is_retry": True,
            "conversation_preview": {"messages": [{"text": "x" * 5000}]},
            "request": {"body": "y" * 5000},
        },
    )
    crud_gateway_usage_search_document.upsert_for_api_usage(
        db_session, api_usage=usage, searchable_text="slim metadata"
    )
    # A JSON null (or scalar) meta_data must not break the pick: jsonb_each
    # raises on a non-object.
    scalar_usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        model_alias="openai/gpt-5.4",
        provider_name="openai",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        estimated_cost=0.0,
    )
    db_session.execute(
        text("UPDATE api_usage SET meta_data = 'null'::jsonb WHERE id = :id"),
        {"id": scalar_usage.id},
    )
    crud_gateway_usage_search_document.upsert_for_api_usage(
        db_session, api_usage=scalar_usage, searchable_text="null metadata"
    )
    db_session.expire_all()
    statements: list[str] = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db_session.get_bind().engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        result = crud_gateway_usage_search_document.search_account_documents(
            db_session,
            account_id=str(test_user.account_id),
            limit=5,
            include_total=False,
            usage_meta_keys=("gateway_attempt", "is_retry", "transport"),
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    by_id = {row["api_usage_id"]: row for row in result["items"]}
    assert by_id[str(scalar_usage.id)]["meta_data"] == {}
    item = by_id[str(usage.id)]
    assert item["meta_data"]["gateway_attempt"] == 2
    assert item["meta_data"]["is_retry"] is True
    assert "conversation_preview" not in item["meta_data"]
    assert "request" not in item["meta_data"]
    assert "transport" not in item["meta_data"]
    usage_reads = [sql for sql in statements if "api_usage" in sql]
    # One read: no per-row load of the deferred column.
    assert len(usage_reads) == 1
    assert "api_usage.meta_data AS" not in usage_reads[0]

    full = crud_gateway_usage_search_document.search_account_documents(
        db_session, account_id=str(test_user.account_id), limit=5
    )
    full_by_id = {row["api_usage_id"]: row for row in full["items"]}
    assert "conversation_preview" in full_by_id[str(usage.id)]["meta_data"]
