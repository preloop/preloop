"""Tests for API usage model and CRUD operations."""

from datetime import datetime, timedelta, timezone

from preloop.models.crud import (
    crud_ai_model,
    crud_api_usage,
    crud_flow,
    crud_flow_execution,
    crud_runtime_session,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate


def test_log_request(db_session, create_account, create_user):
    """Test logging an API request."""
    # Create an account and user
    account = create_account()
    user = create_user(account=account)

    # Log a request
    usage = crud_api_usage.log_request(
        db_session,
        username=user.username,
        endpoint="/api/v1/issues",
        method="GET",
        status_code=200,
        duration=0.123,
        action_type="list_issues",
    )

    # Verify usage attributes
    assert usage.user_id == user.id
    assert usage.endpoint == "/api/v1/issues"
    assert usage.method == "GET"
    assert usage.status_code == 200
    assert usage.duration == 0.123
    assert usage.action_type == "list_issues"
    assert usage.timestamp is not None


def test_get_user_usage(db_session, create_account, create_user):
    """Test getting API usage for a user."""
    # Create an account and user
    account = create_account()
    user = create_user(account=account)

    # Log a few requests
    crud_api_usage.log_request(
        db_session,
        username=user.username,
        endpoint="/api/v1/issues",
        method="GET",
        status_code=200,
        duration=0.123,
    )

    crud_api_usage.log_request(
        db_session,
        username=user.username,
        endpoint="/api/v1/issues",
        method="POST",
        status_code=201,
        duration=0.456,
    )

    # Get user usage
    usage_records = crud_api_usage.get_user_usage(
        db_session, username=user.username, days=1
    )

    # Should have 2 records
    assert len(usage_records) == 2
    assert usage_records[0].method == "POST"  # Most recent first
    assert usage_records[1].method == "GET"


def test_get_endpoint_stats(db_session, create_account, create_user):
    """Test getting endpoint statistics."""
    # Create an account and user
    account = create_account()
    user = create_user(account=account)

    # Use unique endpoints for this test to avoid conflicts with other test runs
    import uuid

    unique_suffix = str(uuid.uuid4())[:8]
    test_endpoints = [
        f"/api/v1/test_endpoint_stats_issues_{unique_suffix}",
        f"/api/v1/test_endpoint_stats_projects_{unique_suffix}",
    ]

    # Log requests to different endpoints
    for i in range(3):
        crud_api_usage.log_request(
            db_session,
            username=user.username,
            endpoint=test_endpoints[0],  # First unique endpoint
            method="GET",
            status_code=200,
            duration=0.1 + i * 0.1,
        )

    for i in range(2):
        crud_api_usage.log_request(
            db_session,
            username=user.username,
            endpoint=test_endpoints[1],  # Second unique endpoint
            method="GET",
            status_code=200,
            duration=0.2 + i * 0.1,
        )

    # Get endpoint stats
    all_stats = crud_api_usage.get_endpoint_stats(db_session, days=1)

    # Filter stats to only include those relevant to this test
    stats = [s for s in all_stats if s["endpoint"] in test_endpoints]

    # Should have stats for 2 endpoints relevant to this test
    assert len(stats) == 2

    # Sort stats by endpoint name to ensure consistent order for assertions
    stats.sort(key=lambda x: x["endpoint"])

    # First endpoint (alphabetically)
    assert stats[0]["endpoint"] == test_endpoints[0]
    assert stats[0]["request_count"] == 3
    assert 0.1 <= stats[0]["min_duration"] <= 0.3
    # Adjust avg_duration check due to potential floating point inaccuracies
    assert 0.19 <= stats[0]["avg_duration"] <= 0.21  # (0.1+0.2+0.3)/3 = 0.2
    assert 0.3 <= stats[0]["max_duration"] <= 0.4

    # Second endpoint (alphabetically)
    assert stats[1]["endpoint"] == test_endpoints[1]
    assert stats[1]["request_count"] == 2
    # (0.2+0.3)/2 = 0.25
    assert 0.24 <= stats[1]["avg_duration"] <= 0.26


def test_get_user_stats(
    db_session, create_account, create_user
):  # Added create_user fixture
    """Test getting user statistics."""
    # Create the accounts and users first to satisfy foreign key constraint
    account1 = create_account()
    user1 = create_user(account=account1)
    account2 = create_account()
    user2 = create_user(account=account2)

    test_usernames = {user1.username, user2.username}

    # Log API usage for the created users
    crud_api_usage.log_request(
        db_session,
        username=user1.username,
        endpoint="/api/v1/test",
        method="GET",
        status_code=200,
        duration=0.1,
    )

    crud_api_usage.log_request(
        db_session,
        username=user2.username,
        endpoint="/api/v1/test",
        method="GET",
        status_code=200,
        duration=0.2,
    )

    # User 1 has more requests
    for _i in range(2):
        crud_api_usage.log_request(
            db_session,
            username=user1.username,
            endpoint="/api/v1/test",
            method="GET",
            status_code=200,
            duration=0.3,
        )

    # Get user stats
    all_stats = crud_api_usage.get_user_stats(db_session, days=1, limit=10)

    # Filter stats to only include those relevant to this test
    stats = [s for s in all_stats if s["username"] in test_usernames]

    # Should have stats for 2 users relevant to this test
    assert len(stats) == 2

    # Create a mapping of username to stats
    stats_by_username = {s["username"]: s for s in stats}

    # Verify user1 has 3 requests
    assert user1.username in stats_by_username
    assert stats_by_username[user1.username]["request_count"] == 3

    # Verify user2 has 1 request
    assert user2.username in stats_by_username
    assert stats_by_username[user2.username]["request_count"] == 1


def test_api_usage_repr(db_session, create_account, create_user):
    """Test the __repr__ method of ApiUsage model."""
    # Create an account and user
    account = create_account()
    user = create_user(account=account)

    # Log a request
    usage = crud_api_usage.log_request(
        db_session,
        username=user.username,
        endpoint="/api/v1/test",
        method="POST",
        status_code=201,
        duration=0.5,
    )

    # Test repr
    repr_str = repr(usage)
    assert "ApiUsage" in repr_str
    assert "POST" in repr_str
    assert "/api/v1/test" in repr_str
    assert str(user.id) in repr_str


def test_get_user_usage_with_account_id(db_session, create_account, create_user):
    """Test getting API usage for a user with account filter."""
    # Create an account and user
    account = create_account()
    user = create_user(account=account)

    # Log a request
    crud_api_usage.log_request(
        db_session,
        username=user.username,
        endpoint="/api/v1/test",
        method="GET",
        status_code=200,
        duration=0.1,
    )

    # Get usage with account_id filter
    usage_records = crud_api_usage.get_user_usage(
        db_session, username=user.username, days=1, account_id=account.id
    )

    # Should have 1 record
    assert len(usage_records) == 1


def test_get_endpoint_stats_with_account_id(db_session, create_account, create_user):
    """Test getting endpoint statistics with account filter."""
    # Create an account and user
    account = create_account()
    user = create_user(account=account)

    # Log requests
    for i in range(2):
        crud_api_usage.log_request(
            db_session,
            username=user.username,
            endpoint="/api/v1/test_with_account",
            method="GET",
            status_code=200,
            duration=0.1 + i * 0.1,
        )

    # Get stats with account_id filter
    stats = crud_api_usage.get_endpoint_stats(db_session, days=1, account_id=account.id)

    # Should have stats (may include other endpoints from other tests)
    assert len(stats) >= 1


def test_get_user_stats_with_account_id(db_session, create_account, create_user):
    """Test getting user statistics with account filter."""
    # Create an account and user
    account = create_account()
    user = create_user(account=account)

    # Log requests
    for _i in range(2):
        crud_api_usage.log_request(
            db_session,
            username=user.username,
            endpoint="/api/v1/test",
            method="GET",
            status_code=200,
            duration=0.1,
        )

    # Get user stats with account_id filter
    stats = crud_api_usage.get_user_stats(db_session, days=1, account_id=account.id)

    # Should have stats for our user
    matching_stats = [s for s in stats if s["username"] == user.username]
    assert len(matching_stats) >= 1
    assert matching_stats[0]["request_count"] >= 2


def test_gateway_usage_filters_by_ai_model_id(db_session, create_account, create_user):
    """Gateway usage aggregations should honor ai_model_id filters."""
    account = create_account()
    user = create_user(account=account)
    ai_model_a = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Model A",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "secret-a",
        },
        account_id=account.id,
    )
    ai_model_b = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Model B",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_key": "secret-b",
        },
        account_id=account.id,
    )
    session_a = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account.id,
        session_source_type="custom",
        session_source_id="session-a",
        runtime_principal_type="flow_execution",
        runtime_principal_id="flow-a",
        runtime_principal_name="Flow A",
        started_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
    )
    session_b = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account.id,
        session_source_type="custom",
        session_source_id="session-b",
        runtime_principal_type="flow_execution",
        runtime_principal_id="flow-b",
        runtime_principal_name="Flow B",
        started_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
    )

    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(user.id),
        account_id=str(account.id),
        ai_model_id=str(ai_model_a.id),
        runtime_session_id=str(session_a.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        estimated_cost=0.1,
    )
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(user.id),
        account_id=str(account.id),
        ai_model_id=str(ai_model_b.id),
        runtime_session_id=str(session_b.id),
        model_alias="openai/gpt-5.4",
        provider_name="openai",
        prompt_tokens=20,
        completion_tokens=10,
        total_tokens=30,
        estimated_cost=0.2,
    )

    start_date = datetime.now(timezone.utc) - timedelta(days=1)
    end_date = datetime.now(timezone.utc) + timedelta(days=1)

    usage_by_session = crud_api_usage.get_gateway_usage_by_session(
        db_session,
        account_id=str(account.id),
        ai_model_id=str(ai_model_a.id),
        start_date=start_date,
        end_date=end_date,
    )
    timeseries = crud_api_usage.get_gateway_usage_timeseries(
        db_session,
        account_id=str(account.id),
        ai_model_id=str(ai_model_a.id),
        start_date=start_date,
        end_date=end_date,
    )

    assert len(usage_by_session) == 1
    assert usage_by_session[0]["ai_model_id"] == str(ai_model_a.id)
    assert usage_by_session[0]["total_tokens"] == 15
    assert len(timeseries) == 1
    assert timeseries[0]["total_tokens"] == 15


def test_get_gateway_usage_by_model_strictly_isolates_runtime_session(
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
            name="Gateway Usage Flow",
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
        runtime_principal_name="Gateway Usage Flow",
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
        runtime_principal_name="Gateway Usage Flow",
        started_at=execution.start_time,
        last_activity_at=execution.start_time,
    )

    crud_api_usage.log_gateway_request(
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
    crud_api_usage.log_gateway_request(
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
    crud_api_usage.log_gateway_request(
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

    start_date = datetime.now(timezone.utc) - timedelta(days=1)
    end_date = datetime.now(timezone.utc) + timedelta(days=1)

    result = crud_api_usage.get_gateway_usage_by_model(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(selected_session.id),
        flow_execution_id=str(execution.id),
        start_date=start_date,
        end_date=end_date,
        limit=20,
    )

    assert len(result) == 1
    assert result[0]["request_count"] == 2
    assert result[0]["prompt_tokens"] == 40
    assert result[0]["completion_tokens"] == 20
    assert result[0]["total_tokens"] == 60
    assert result[0]["estimated_cost"] == 0.4

    sibling_result = crud_api_usage.get_gateway_usage_by_model(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(sibling_session.id),
        flow_execution_id=str(execution.id),
        start_date=start_date,
        end_date=end_date,
        limit=20,
    )

    assert len(sibling_result) == 1
    assert sibling_result[0]["request_count"] == 2
    assert sibling_result[0]["prompt_tokens"] == 50
    assert sibling_result[0]["completion_tokens"] == 25
    assert sibling_result[0]["total_tokens"] == 75
    assert sibling_result[0]["estimated_cost"] == 0.5


def test_get_statistics_for_user_aggregates_in_sql(
    db_session, create_account, create_user
):
    """SQL aggregation returns totals, date buckets, issue actions, endpoints."""
    account = create_account()
    user = create_user(account=account)
    now = datetime.now(timezone.utc)

    crud_api_usage.log_request(
        db_session,
        username=user.username,
        endpoint="/api/v1/issues",
        method="POST",
        status_code=201,
        duration=0.1,
        action_type="create_issue",
    )
    crud_api_usage.log_request(
        db_session,
        username=user.username,
        endpoint="/api/v1/issues",
        method="PATCH",
        status_code=200,
        duration=0.2,
        action_type="update_issue",
    )
    crud_api_usage.log_request(
        db_session,
        username=user.username,
        endpoint="/api/v1/issues/1/close",
        method="POST",
        status_code=200,
        duration=0.15,
        action_type="close_issue",
    )
    for _ in range(3):
        crud_api_usage.log_request(
            db_session,
            username=user.username,
            endpoint="/api/v1/search",
            method="GET",
            status_code=200,
            duration=0.05,
            action_type="search",
        )

    stats = crud_api_usage.get_statistics_for_user(
        db_session,
        username=user.username,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=1),
    )

    assert stats["total_requests"] == 6
    assert stats["issues_created"] == 1
    assert stats["issues_updated"] == 1
    assert stats["issues_closed"] == 1
    assert stats["requests_by_endpoint"]["/api/v1/search"] == 3
    assert stats["requests_by_endpoint"]["/api/v1/issues"] == 2
    assert sum(stats["requests_by_date"].values()) == 6
    today = now.date().isoformat()
    assert today in stats["requests_by_date"]


def test_get_statistics_for_user_caps_endpoints(
    db_session, create_account, create_user
):
    """requests_by_endpoint is limited to top N by count."""
    account = create_account()
    user = create_user(account=account)
    now = datetime.now(timezone.utc)

    for i in range(5):
        for _ in range(i + 1):
            crud_api_usage.log_request(
                db_session,
                username=user.username,
                endpoint=f"/api/v1/ep-{i}",
                method="GET",
                status_code=200,
                duration=0.01,
            )

    stats = crud_api_usage.get_statistics_for_user(
        db_session,
        username=user.username,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=1),
        endpoint_limit=2,
    )

    assert len(stats["requests_by_endpoint"]) == 2
    assert list(stats["requests_by_endpoint"].keys()) == [
        "/api/v1/ep-4",
        "/api/v1/ep-3",
    ]
    assert stats["total_requests"] == 15


def test_get_statistics_for_user_defaults_to_last_30_days(
    db_session, create_account, create_user
):
    """With no dates, only rows from the last 30 days are counted."""
    account = create_account()
    user = create_user(account=account)

    recent = crud_api_usage.log_request(
        db_session,
        username=user.username,
        endpoint="/api/v1/recent",
        method="GET",
        status_code=200,
        duration=0.01,
    )
    old = crud_api_usage.log_request(
        db_session,
        username=user.username,
        endpoint="/api/v1/old",
        method="GET",
        status_code=200,
        duration=0.01,
    )
    # Force the second row outside the default window.
    old.timestamp = datetime.now(timezone.utc) - timedelta(days=45)
    db_session.commit()

    stats = crud_api_usage.get_statistics_for_user(db_session, username=user.username)

    assert stats["total_requests"] == 1
    assert stats["requests_by_endpoint"] == {"/api/v1/recent": 1}
    assert recent.id is not None


def test_get_gateway_usage_by_model_filters_ids_and_honors_unlimited(
    db_session, test_user
):
    """``ai_model_ids`` filters in SQL and ``limit=None`` returns every group.

    Both are required by spend caps: the query orders by request count, so a
    limit would drop a low-volume, high-cost model from a SUM.
    """
    expensive_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Expensive Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5-expensive",
        },
        account_id=test_user.account_id,
    )
    for index in range(25):
        cheap_model = crud_ai_model.create_with_account(
            db=db_session,
            obj_in={
                "name": f"Cheap Model {index}",
                "provider_name": "anthropic",
                "model_identifier": f"claude-haiku-{index}",
            },
            account_id=test_user.account_id,
        )
        for _ in range(5):
            crud_api_usage.log_gateway_request(
                db_session,
                endpoint="/anthropic/v1/messages",
                method="POST",
                status_code=200,
                duration=0.1,
                user_id=str(test_user.id),
                account_id=str(test_user.account_id),
                ai_model_id=str(cheap_model.id),
                model_alias=f"anthropic/claude-haiku-{index}",
                provider_name="anthropic",
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                estimated_cost=0.001,
            )
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        ai_model_id=str(expensive_model.id),
        model_alias="openai/gpt-5-expensive",
        provider_name="openai",
        prompt_tokens=1000,
        completion_tokens=1000,
        total_tokens=2000,
        estimated_cost=7.5,
    )

    # Default (limit=20, request-count ordering) hides the expensive model.
    default_rows = crud_api_usage.get_gateway_usage_by_model(
        db_session, account_id=str(test_user.account_id)
    )
    assert len(default_rows) == 20
    assert str(expensive_model.id) not in {row["ai_model_id"] for row in default_rows}

    # Filtering by id keeps it regardless of request count.
    filtered_rows = crud_api_usage.get_gateway_usage_by_model(
        db_session,
        account_id=str(test_user.account_id),
        ai_model_ids=[str(expensive_model.id)],
        limit=None,
    )
    assert len(filtered_rows) == 1
    assert filtered_rows[0]["estimated_cost"] == 7.5

    # limit=None returns every group.
    all_rows = crud_api_usage.get_gateway_usage_by_model(
        db_session, account_id=str(test_user.account_id), limit=None
    )
    assert len(all_rows) == 26

    # An empty id sequence yields nothing (it is a filter, not "unset").
    assert (
        crud_api_usage.get_gateway_usage_by_model(
            db_session, account_id=str(test_user.account_id), ai_model_ids=[]
        )
        == []
    )
