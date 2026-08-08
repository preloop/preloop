"""Tests for execution metrics service."""

import pytest
from unittest.mock import MagicMock
from uuid import uuid4
from datetime import datetime, timezone

from preloop.models.crud import crud_api_usage, crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.execution_metrics import ExecutionMetricsService


@pytest.fixture
def mock_db():
    """Create a mock database session."""
    return MagicMock()


@pytest.fixture
def mock_execution():
    """Create a mock FlowExecution."""
    exec_model = MagicMock()
    exec_model.id = uuid4()
    exec_model.flow_id = uuid4()
    exec_model.start_time = datetime.now(timezone.utc)
    exec_model.end_time = None
    exec_model.mcp_usage_logs = None
    exec_model.execution_logs = None
    exec_model.log_entries = []
    return exec_model


class TestGetExecutionMetrics:
    """Test get_execution_metrics."""

    def test_execution_not_found_raises(self, mock_db):
        """When execution does not exist, raise ValueError."""
        mock_db.query.return_value.filter.return_value.first.return_value = None

        service = ExecutionMetricsService(mock_db)

        with pytest.raises(ValueError, match="not found"):
            service.get_execution_metrics(str(uuid4()))

    def test_returns_metrics_structure(self, mock_db, mock_execution):
        """Returns dict with expected keys."""
        mock_execution.mcp_usage_logs = []
        mock_execution.execution_logs = []
        mock_execution.log_entries = []

        mock_flow = MagicMock()
        mock_flow.id = mock_execution.flow_id
        mock_flow.account_id = uuid4()
        mock_flow.ai_model_id = None

        mock_account = MagicMock()
        mock_account.id = mock_flow.account_id
        mock_account.users = [MagicMock(id=uuid4())]

        mock_db.query.return_value.filter.return_value.first.side_effect = [
            mock_execution,
            None,
            mock_flow,
        ]
        mock_db.query.return_value.filter.return_value.one.return_value = MagicMock(
            api_requests=0,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            estimated_cost=0.0,
            priced_requests=0,
        )

        service = ExecutionMetricsService(mock_db)
        result = service.get_execution_metrics(str(mock_execution.id))

        assert "tool_calls" in result
        assert "api_requests" in result
        assert "token_usage" in result
        assert "estimated_cost" in result
        assert "has_pricing" in result
        assert result["tool_calls"] == 0
        assert result["api_requests"] == 0
        assert result["estimated_cost"] == 0.0
        assert result["has_pricing"] is False


class TestCountToolCalls:
    """Test _count_tool_calls."""

    def test_from_mcp_usage_logs(self, mock_db, mock_execution):
        """Count from mcp_usage_logs list."""
        mock_execution.mcp_usage_logs = [
            {"tool": "get_issue"},
            {"tool": "create_issue"},
        ]
        mock_execution.log_entries = []
        mock_execution.execution_logs = None

        service = ExecutionMetricsService(mock_db)
        count = service._count_tool_calls(mock_execution)

        assert count == 2

    def test_from_log_entries(self, mock_db, mock_execution):
        """Count from log_entries with tool_call and mcp_call types."""
        mock_execution.mcp_usage_logs = None
        mock_execution.execution_logs = None

        log1 = MagicMock()
        log1.log_type = "tool_call"
        log2 = MagicMock()
        log2.log_type = "mcp_call"
        log3 = MagicMock()
        log3.log_type = "log"
        mock_execution.log_entries = [log1, log2, log3]

        service = ExecutionMetricsService(mock_db)
        count = service._count_tool_calls(mock_execution)

        assert count == 2

    def test_from_legacy_execution_logs(self, mock_db, mock_execution):
        """Count from legacy JSONB execution_logs."""
        mock_execution.mcp_usage_logs = None
        mock_execution.log_entries = []
        mock_execution.execution_logs = [
            {"type": "log", "payload": {}},
            {"type": "tool_call", "payload": {}},
            {"type": "mcp_call", "payload": {}},
        ]

        service = ExecutionMetricsService(mock_db)
        count = service._count_tool_calls(mock_execution)

        assert count == 2

    def test_combined_sources(self, mock_db, mock_execution):
        """Count from multiple sources is additive."""
        mock_execution.mcp_usage_logs = [{"tool": "x"}]
        log1 = MagicMock()
        log1.log_type = "tool_call"
        mock_execution.log_entries = [log1]
        mock_execution.execution_logs = None

        service = ExecutionMetricsService(mock_db)
        count = service._count_tool_calls(mock_execution)

        assert count == 2


class TestParseTokenUsage:
    """Test _parse_token_usage."""

    def test_no_logs_returns_zero(self, mock_db, mock_execution):
        """Empty logs return zero token usage."""
        mock_execution.log_entries = []
        mock_execution.execution_logs = None

        service = ExecutionMetricsService(mock_db)
        result = service._parse_token_usage(mock_execution)

        assert result["total_tokens"] == 0
        assert result["input_tokens"] == 0
        assert result["output_tokens"] == 0

    def test_parses_from_log_entries(self, mock_db, mock_execution):
        """Parse 'tokens used' pattern from log message."""
        log1 = MagicMock()
        log1.message = "tokens used\n1,234"
        log1.metadata_ = None
        mock_execution.log_entries = [log1]
        mock_execution.execution_logs = None

        service = ExecutionMetricsService(mock_db)
        result = service._parse_token_usage(mock_execution)

        assert result["total_tokens"] == 1234

    def test_parses_from_execution_logs_payload(self, mock_db, mock_execution):
        """Parse from legacy execution_logs payload content."""
        mock_execution.log_entries = []
        mock_execution.execution_logs = [
            {
                "payload": {
                    "content": "Summary:\ntokens used\n5,000",
                },
            },
        ]

        service = ExecutionMetricsService(mock_db)
        result = service._parse_token_usage(mock_execution)

        assert result["total_tokens"] == 5000

    def test_sums_multiple_occurrences(self, mock_db, mock_execution):
        """Sum multiple token usage mentions."""
        log1 = MagicMock()
        log1.message = "tokens used\n1,000"
        log1.metadata_ = None
        log2 = MagicMock()
        log2.message = "tokens used\n2,000"
        log2.metadata_ = None
        mock_execution.log_entries = [log1, log2]
        mock_execution.execution_logs = None

        service = ExecutionMetricsService(mock_db)
        result = service._parse_token_usage(mock_execution)

        assert result["total_tokens"] == 3000


class TestCountApiRequests:
    """Test _count_api_requests."""

    def test_no_start_time_returns_zero(self, mock_db, mock_execution):
        """When start_time is None, return 0."""
        mock_execution.start_time = None
        mock_db.query.return_value.filter.return_value.count.return_value = 0

        service = ExecutionMetricsService(mock_db)
        count = service._count_api_requests(mock_execution)

        assert count == 0

    def test_no_flow_returns_zero(self, mock_db, mock_execution):
        """When flow not found, return 0."""
        mock_db.query.return_value.filter.return_value.first.side_effect = [
            MagicMock(flow_id=mock_execution.flow_id, account_id=uuid4()),
            None,  # account not found
        ]
        mock_db.query.return_value.filter.return_value.count.return_value = 0

        service = ExecutionMetricsService(mock_db)
        count = service._count_api_requests(mock_execution)

        assert count == 0

    def test_returns_api_count(self, mock_db, mock_execution):
        """Return count of API requests in timeframe."""
        mock_flow = MagicMock()
        mock_flow.id = mock_execution.flow_id
        mock_flow.account_id = uuid4()

        mock_account = MagicMock()
        mock_account.id = mock_flow.account_id
        mock_account.users = [MagicMock(id=uuid4())]

        mock_db.query.return_value.filter.return_value.first.side_effect = [
            mock_flow,
            mock_account,
        ]
        mock_db.query.return_value.filter.return_value.count.return_value = 42

        service = ExecutionMetricsService(mock_db)
        count = service._count_api_requests(mock_execution)

        assert count == 42


class TestCalculateCost:
    """Test _calculate_cost."""

    def test_zero_tokens_returns_zero(self, mock_db, mock_execution):
        """Zero tokens -> (0.0, False)."""
        service = ExecutionMetricsService(mock_db)
        cost, has_pricing = service._calculate_cost(mock_execution, {"total_tokens": 0})

        assert cost == 0.0
        assert has_pricing is False

    def test_no_flow_returns_zero(self, mock_db, mock_execution):
        """No flow -> (0.0, False)."""
        mock_db.query.return_value.filter.return_value.first.return_value = None

        service = ExecutionMetricsService(mock_db)
        cost, has_pricing = service._calculate_cost(
            mock_execution, {"total_tokens": 1000}
        )

        assert cost == 0.0
        assert has_pricing is False

    def test_no_ai_model_returns_zero(self, mock_db, mock_execution):
        """Flow has no ai_model_id -> (0.0, False)."""
        mock_flow = MagicMock()
        mock_flow.id = mock_execution.flow_id
        mock_flow.ai_model_id = None

        mock_db.query.return_value.filter.return_value.first.side_effect = [
            mock_flow,
        ]

        service = ExecutionMetricsService(mock_db)
        cost, has_pricing = service._calculate_cost(
            mock_execution, {"total_tokens": 1000}
        )

        assert cost == 0.0
        assert has_pricing is False

    def test_with_input_output_pricing(self, mock_db, mock_execution):
        """Calculate cost from input/output price per 1k."""
        mock_flow = MagicMock()
        mock_flow.id = mock_execution.flow_id
        mock_flow.ai_model_id = uuid4()

        mock_ai_model = MagicMock()
        mock_ai_model.meta_data = {
            "pricing": {
                "input_price_per_1k": 0.01,
                "output_price_per_1k": 0.03,
            }
        }
        mock_ai_model.model_parameters = None

        mock_db.query.return_value.filter.return_value.first.side_effect = [
            mock_flow,
            mock_ai_model,
        ]

        service = ExecutionMetricsService(mock_db)
        token_usage = {
            "total_tokens": 2000,
            "input_tokens": 1500,
            "output_tokens": 500,
        }
        cost, has_pricing = service._calculate_cost(mock_execution, token_usage)

        assert has_pricing is True
        # 1.5 * 0.01 + 0.5 * 0.03 = 0.015 + 0.015 = 0.03
        assert abs(cost - 0.03) < 0.0001

    def test_with_price_per_1k_fallback(self, mock_db, mock_execution):
        """Use price_per_1k when input/output not available."""
        mock_flow = MagicMock()
        mock_flow.id = mock_execution.flow_id
        mock_flow.ai_model_id = uuid4()

        mock_ai_model = MagicMock()
        mock_ai_model.meta_data = {
            "pricing": {
                "price_per_1k": 0.02,
            }
        }
        mock_ai_model.model_parameters = None

        mock_db.query.return_value.filter.return_value.first.side_effect = [
            mock_flow,
            mock_ai_model,
        ]

        service = ExecutionMetricsService(mock_db)
        token_usage = {"total_tokens": 1000, "input_tokens": 0, "output_tokens": 0}
        cost, has_pricing = service._calculate_cost(mock_execution, token_usage)

        assert has_pricing is True
        assert abs(cost - 0.02) < 0.0001

    def test_pricing_from_model_parameters(self, mock_db, mock_execution):
        """Pricing can come from model_parameters when meta_data has none."""
        mock_flow = MagicMock()
        mock_flow.id = mock_execution.flow_id
        mock_flow.ai_model_id = uuid4()

        mock_ai_model = MagicMock()
        mock_ai_model.meta_data = {}
        mock_ai_model.model_parameters = {
            "pricing": {"price_per_1k": 0.01},
        }

        mock_db.query.return_value.filter.return_value.first.side_effect = [
            mock_flow,
            mock_ai_model,
        ]

        service = ExecutionMetricsService(mock_db)
        cost, has_pricing = service._calculate_cost(
            mock_execution, {"total_tokens": 1000}
        )

        assert has_pricing is True
        assert abs(cost - 0.01) < 0.0001


def test_get_execution_metrics_prefers_gateway_usage(db_session, test_user):
    """Execution metrics should prefer explicit gateway attribution over log parsing."""
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Gateway Metrics Flow",
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
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
        FlowExecutionCreate(
            flow_id=flow.id,
            status="SUCCEEDED",
            mcp_usage_logs=[{"tool_name": "search"}],
            execution_logs=[
                {
                    "type": "agent_log_line",
                    "payload": {"line": "tokens used\n999"},
                }
            ],
        ),
    )
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=120,
        completion_tokens=30,
        total_tokens=150,
        estimated_cost=0.42,
    )

    metrics = ExecutionMetricsService(db_session).get_execution_metrics(
        str(execution.id)
    )

    assert metrics["tool_calls"] == 1
    assert metrics["api_requests"] == 1
    assert metrics["token_usage"]["total_tokens"] == 150
    assert metrics["token_usage"]["input_tokens"] == 120
    assert metrics["token_usage"]["output_tokens"] == 30
    assert metrics["estimated_cost"] == 0.42
    assert metrics["has_pricing"] is True


def test_get_execution_metrics_falls_back_to_log_parsing_without_gateway_usage(
    db_session, test_user
):
    """Execution metrics should keep legacy parsing for non-gateway executions."""
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Legacy Metrics Flow",
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
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
        FlowExecutionCreate(
            flow_id=flow.id,
            status="SUCCEEDED",
            mcp_usage_logs=[{"tool_name": "search"}, {"tool_name": "read"}],
        ),
    )
    execution.execution_logs = [
        {
            "type": "agent_log_line",
            "payload": {"line": "tokens used\n1,234"},
        }
    ]
    db_session.add(execution)
    db_session.commit()

    metrics = ExecutionMetricsService(db_session).get_execution_metrics(
        str(execution.id)
    )

    assert metrics["tool_calls"] == 2
    assert metrics["api_requests"] == 0
    assert metrics["token_usage"]["total_tokens"] == 1234
    # 1234 tokens were spent with no resolvable price. Reporting 0.0 here is
    # what made real spend look free; the volume is surfaced instead.
    assert metrics["estimated_cost"] is None
    assert metrics["has_pricing"] is False
    assert metrics["unpriced_tokens"] == 1234
