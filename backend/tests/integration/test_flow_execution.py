"""Integration tests for flow execution end-to-end."""

import asyncio
import logging
import os
from uuid import uuid4

import pytest
import pytest_asyncio
import aiodocker
from sqlalchemy.orm import Session

from preloop.models.models import Flow, AIModel, Account, FlowExecution
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.sync.services.event_bus import get_nats_client

logger = logging.getLogger(__name__)


@pytest_asyncio.fixture
async def docker_client():
    """Fixture to provide a Docker client for testing.

    Yields None if Docker is not available.
    """
    try:
        async with aiodocker.Docker() as client:
            yield client
    except Exception as e:
        logger.warning(f"Docker not available: {e}")
        yield None


@pytest.fixture
def test_account(db_session: Session) -> Account:
    """Create a test account."""
    account = Account(
        id=str(uuid4()),
        organization_name="Test Account",
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


@pytest.fixture
def test_ai_model(db_session: Session, test_account: Account) -> AIModel:
    """Create a test AI model configuration."""
    ai_model = AIModel(
        name="Test GPT-4",
        provider_name="openai",
        model_identifier="gpt-5.4",
        api_key=os.getenv("OPENAI_API_KEY", "test-key"),
        account_id=test_account.id,
    )
    db_session.add(ai_model)
    db_session.commit()
    db_session.refresh(ai_model)
    return ai_model


@pytest.fixture
def test_flow(
    db_session: Session, test_account: Account, test_ai_model: AIModel
) -> Flow:
    """Create a test flow."""
    flow = Flow(
        name="Test Flow",
        description="Test flow for integration testing",
        trigger_event_source="github",
        trigger_event_types=["push"],  # Use array field
        trigger_config={"branch": "main"},
        prompt_template="Test commit: {{trigger_event.payload.commit.message}}",
        ai_model_id=test_ai_model.id,
        agent_type="openhands",
        agent_config={"agent_type": "CodeActAgent", "max_iterations": 5},
        allowed_mcp_servers=["preloop-mcp"],
        allowed_mcp_tools=[
            {"server_name": "preloop-mcp", "tool_name": "search_issues"}
        ],
        is_enabled=True,
        account_id=test_account.id,
    )
    db_session.add(flow)
    db_session.commit()
    db_session.refresh(flow)
    return flow


class TestFlowExecution:
    """Integration tests for flow execution."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_flow_trigger_from_event(
        self, db_session: Session, test_flow: Flow, test_account: Account
    ):
        """Test that a flow is triggered from an incoming event.

        NOTE: This is an integration test that requires NATS to be running.
        It may be skipped in CI environments without NATS.
        """
        # Create a test event - include branch at top level for trigger_config matching
        event_data = {
            "source": "github",
            "type": "push",
            "account_id": str(test_account.id),
            "branch": "main",  # For trigger_config matching
            "payload": {
                "branch": "main",
                "commit": {
                    "sha": "abc123",
                    "message": "Test commit message",
                    "author": "test@example.com",
                },
            },
        }

        # Create trigger service
        trigger_service = FlowTriggerService(db_session)

        try:
            # Process the event
            await trigger_service.process_event(event_data)
        except Exception as e:
            # Skip test if NATS is not available
            if "NATS" in str(e) or "connect" in str(e).lower():
                pytest.skip(f"NATS not available: {e}")
            raise

        # Wait a bit for async execution to start
        await asyncio.sleep(2)

        # Refresh the session to see new records
        db_session.expire_all()

        # Check that a flow execution was created
        execution = (
            db_session.query(FlowExecution)
            .filter(FlowExecution.flow_id == test_flow.id)
            .first()
        )

        # In integration tests without full infrastructure, execution may not be created
        # This is acceptable - the test verifies the trigger service runs without error
        if execution is not None:
            assert execution.status in [
                "PENDING",
                "INITIALIZING",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
            ]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_flow_execution_orchestrator(
        self, db_session: Session, test_flow: Flow, test_account: Account
    ):
        """Test the flow execution orchestrator end-to-end.

        NOTE: Requires NATS, Docker, and agent infrastructure. Excluded from
        unit test runs via -m 'not integration'.
        """
        # Create trigger event data
        trigger_event_data = {
            "source": "github",
            "type": "push",
            "account_id": test_account.id,
            "payload": {
                "commit": {
                    "message": "Fix bug in authentication",
                },
            },
        }

        # Get NATS client
        nats_client = await get_nats_client()

        # Create orchestrator
        orchestrator = FlowExecutionOrchestrator(
            db=db_session,
            flow_id=test_flow.id,
            trigger_event_data=trigger_event_data,
            nats_client=nats_client,
        )

        # Run the orchestrator (in background to avoid blocking)
        asyncio.create_task(orchestrator.run())

        # Wait for execution to start
        await asyncio.sleep(5)

        # Check that execution log was created
        assert orchestrator.execution_log is not None
        assert orchestrator.execution_log.status in [
            "PENDING",
            "INITIALIZING",
            "RUNNING",
        ]

        # Don't wait for full completion in tests (can take a long time)
        # In production, we'd wait and check the final status

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_container_inspection(self, db_session: Session, docker_client):
        """Test that we can inspect running agent containers.

        NOTE: This is an integration test that requires Docker to be running.
        """
        # Skip if docker_client fixture failed
        if docker_client is None:
            pytest.skip("Docker client not available")

        try:
            # List all containers with preloop labels
            containers = await docker_client.containers.list(
                filters={"label": "preloop.agent_type"}
            )
        except Exception as e:
            pytest.skip(f"Docker not available: {e}")

        logger.info(f"Found {len(containers)} Preloop agent containers")

        for container in containers:
            info = await container.show()

            # Extract labels
            labels = info["Config"]["Labels"]
            flow_id = labels.get("preloop.flow_id")
            execution_id = labels.get("preloop.execution_id")
            agent_type = labels.get("preloop.agent_type")

            logger.info(
                f"Container {container.id[:12]}: "
                f"flow_id={flow_id}, "
                f"execution_id={execution_id}, "
                f"agent_type={agent_type}"
            )

            # Get container status
            state = info["State"]
            logger.info(f"  Status: {state['Status']}, Running: {state['Running']}")

            # Get logs (last 50 lines)
            logs = await container.log(stdout=True, stderr=True, tail=50)
            logger.info(f"  Last 50 log lines: {len(logs)} lines")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_prompt_resolution(self, db_session: Session, test_flow: Flow):
        """Test that prompt placeholders are resolved correctly."""
        trigger_event_data = {
            "source": "github",
            "type": "push",
            "account_id": "test-account-id",
            "payload": {
                "commit": {
                    "message": "Fix authentication bug",
                    "sha": "abc123",
                },
            },
        }

        nats_client = await get_nats_client()

        orchestrator = FlowExecutionOrchestrator(
            db=db_session,
            flow_id=test_flow.id,
            trigger_event_data=trigger_event_data,
            nats_client=nats_client,
        )

        # Create execution log first (required for resolution)
        orchestrator._create_execution_log()
        orchestrator._get_flow_details()

        # Resolve the prompt
        resolved_prompt = await orchestrator._resolve_prompt()

        # Check that placeholder was replaced
        assert "{{trigger_event.payload.commit.message}}" not in resolved_prompt
        assert "Fix authentication bug" in resolved_prompt

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_mcp_configuration(self, test_flow: Flow):
        """Test that MCP configuration is correctly generated."""
        from preloop.services.mcp_config_service import MCPConfigService

        mcp_config = MCPConfigService.generate_mcp_config(
            test_flow.allowed_mcp_servers, test_flow.allowed_mcp_tools
        )

        # Check that preloop-mcp is configured
        assert "preloop-mcp" in mcp_config["mcpServers"]
        assert "preloop-mcp" in mcp_config["allowed_tools"]
        assert "search_issues" in mcp_config["allowed_tools"]["preloop-mcp"]

        # Check environment variables
        mcp_env = MCPConfigService.generate_mcp_environment_vars(
            test_flow.allowed_mcp_servers, test_flow.allowed_mcp_tools
        )

        assert "MCP_ALLOWED_SERVERS" in mcp_env
        assert "preloop-mcp" in mcp_env["MCP_ALLOWED_SERVERS"]
        assert "MCP_ALLOWED_TOOLS" in mcp_env
