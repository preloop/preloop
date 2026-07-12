"""Tests for FlowTriggerService."""

import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.models.models import Flow, Account
from preloop.models.models.user import User
from preloop.models.schemas.flow import FlowCreate
from preloop.models.crud import crud_account, crud_user


@pytest.fixture
def test_account(db_session: Session) -> Account:
    """Create a test account (organization)."""
    account_data = {
        "organization_name": f"Test Org {uuid4().hex[:8]}",
        "is_active": True,
    }
    account = crud_account.create(db_session, obj_in=account_data)
    return account


@pytest.fixture
def test_user(db_session: Session, test_account: Account) -> User:
    """Create a test user for the account."""
    user_data = {
        "account_id": test_account.id,
        "email": f"flow_test_{uuid4().hex[:8]}@example.com",
        "username": f"flow_test_user_{uuid4().hex[:8]}",
        "full_name": "Flow Test User",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "test_password",
        "user_source": "local",
    }
    user = crud_user.create(db_session, obj_in=user_data)
    db_session.flush()
    db_session.refresh(user)
    return user


@pytest.fixture
def test_flow(db_session: Session, test_account: Account) -> Flow:
    """Create a test flow."""
    from preloop.models.crud import crud_flow

    flow_in = FlowCreate(
        name="Test Flow",
        description="A test flow",
        trigger_event_source="github",
        trigger_event_types=["push"],  # Use array field
        prompt_template="Test prompt: {{payload.message}}",
        agent_type="openhands",
        agent_config={"max_iterations": 10},
        account_id=test_account.id,
    )
    flow = crud_flow.create(db=db_session, flow_in=flow_in, account_id=test_account.id)
    return flow


@pytest.fixture
def disabled_flow(db_session: Session, test_account: Account) -> Flow:
    """Create a disabled test flow."""
    from preloop.models.crud import crud_flow

    flow_in = FlowCreate(
        name="Disabled Flow",
        description="A disabled test flow",
        trigger_event_source="github",
        trigger_event_types=["push"],  # Use array field
        prompt_template="Disabled prompt",
        agent_type="openhands",
        agent_config={"max_iterations": 5},
        is_enabled=False,
        account_id=test_account.id,
    )
    flow = crud_flow.create(db=db_session, flow_in=flow_in, account_id=test_account.id)
    return flow


@pytest.fixture
def conditional_flow(db_session: Session, test_account: Account) -> Flow:
    """Create a flow with trigger_config conditions."""
    from preloop.models.crud import crud_flow

    flow_in = FlowCreate(
        name="Conditional Flow",
        description="Flow with branch condition",
        trigger_event_source="github",
        trigger_event_types=["push"],  # Use array field
        trigger_config={"branch": "main"},
        prompt_template="Main branch push: {{payload.message}}",
        agent_type="openhands",
        agent_config={"max_iterations": 10},
        account_id=test_account.id,
    )
    flow = crud_flow.create(db=db_session, flow_in=flow_in, account_id=test_account.id)
    return flow


class TestFlowTriggerService:
    """Test suite for FlowTriggerService."""

    @pytest.mark.asyncio
    async def test_process_event_no_source_or_type(self, db_session: Session):
        """Test that events without source or type are ignored."""
        service = FlowTriggerService(db_session)

        # Event with no source
        await service.process_event({"type": "push"})

        # Event with no type
        await service.process_event({"source": "github"})

        # Event with neither
        await service.process_event({})

        # No flows should be triggered (no assertion needed, just verify no error)

    @pytest.mark.asyncio
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    async def test_process_event_matching_flow(
        self, mock_nats, db_session: Session, test_flow: Flow
    ):
        """Test that a matching flow is triggered."""
        mock_nats.return_value = AsyncMock()

        service = FlowTriggerService(db_session)

        event_data = {
            "source": "github",
            "type": "push",
            "payload": {"message": "Test commit"},
            "account_id": test_flow.account_id,
        }

        with patch.object(
            service, "_start_flow_execution", new_callable=AsyncMock
        ) as mock_run:
            await service.process_event(event_data)

            # Verify orchestrator was triggered
            mock_run.assert_called_once()
            assert mock_run.call_args[1]["flow"].id == test_flow.id

    @pytest.mark.asyncio
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    async def test_process_event_disabled_flow_not_triggered(
        self, mock_nats, db_session: Session, disabled_flow: Flow
    ):
        """Test that disabled flows are not triggered."""
        mock_nats.return_value = AsyncMock()

        service = FlowTriggerService(db_session)

        event_data = {
            "source": "github",
            "type": "push",
            "payload": {"message": "Test commit"},
            "account_id": disabled_flow.account_id,
        }

        with patch.object(
            service, "_start_flow_execution", new_callable=AsyncMock
        ) as mock_run:
            await service.process_event(event_data)

            # Verify orchestrator was NOT triggered
            mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_event_no_matching_flows(self, db_session: Session):
        """Test handling when no flows match the event."""
        service = FlowTriggerService(db_session)

        event_data = {
            "source": "gitlab",  # No flows for gitlab
            "type": "push",
            "payload": {"message": "Test commit"},
        }

        # Should not raise an error
        await service.process_event(event_data)

    @pytest.mark.asyncio
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    async def test_trigger_config_branch_match(
        self, mock_nats, db_session: Session, conditional_flow: Flow
    ):
        """Test that trigger_config branch condition works."""
        mock_nats.return_value = AsyncMock()

        service = FlowTriggerService(db_session)

        # Event with matching branch
        event_data = {
            "source": "github",
            "type": "push",
            "payload": {"branch": "main", "message": "Main branch commit"},
            "account_id": conditional_flow.account_id,
        }

        with patch.object(
            service, "_start_flow_execution", new_callable=AsyncMock
        ) as mock_run:
            await service.process_event(event_data)

            # Verify orchestrator was triggered
            mock_run.assert_called_once()

    @pytest.mark.asyncio
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    async def test_trigger_config_branch_no_match(
        self, mock_nats, db_session: Session, conditional_flow: Flow
    ):
        """Test that trigger_config branch condition filters correctly."""
        mock_nats.return_value = AsyncMock()

        service = FlowTriggerService(db_session)

        # Event with non-matching branch
        event_data = {
            "source": "github",
            "type": "push",
            "payload": {"branch": "develop", "message": "Dev branch commit"},
            "account_id": conditional_flow.account_id,
        }

        with patch.object(
            service, "_start_flow_execution", new_callable=AsyncMock
        ) as mock_run:
            await service.process_event(event_data)

            # Verify orchestrator was NOT triggered
            mock_run.assert_not_called()

    @pytest.mark.asyncio
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    async def test_trigger_config_list_match(
        self, mock_nats, db_session: Session, test_account: Account
    ):
        """Test trigger_config with list values (e.g., labels)."""
        from preloop.models.crud import crud_flow

        # Create flow with label condition
        flow_in = FlowCreate(
            name="Label Flow",
            trigger_event_source="github",
            trigger_event_types=["issue_created"],  # Use array field
            trigger_config={"labels": ["bug", "critical"]},
            prompt_template="Critical bug: {{payload.title}}",
            agent_type="openhands",
            agent_config={},
            account_id=test_account.id,
        )
        flow = crud_flow.create(
            db=db_session, flow_in=flow_in, account_id=test_account.id
        )

        mock_nats.return_value = AsyncMock()
        service = FlowTriggerService(db_session)

        # Event with matching label
        event_data = {
            "source": "github",
            "type": "issue_created",
            "payload": {"title": "Test issue", "labels": ["bug", "documentation"]},
            "account_id": test_account.id,
        }

        with patch.object(
            service, "_start_flow_execution", new_callable=AsyncMock
        ) as mock_run:
            await service.process_event(event_data)

            # Verify orchestrator was triggered
            mock_run.assert_called_once()

    @pytest.mark.asyncio
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    async def test_multiple_matching_flows(
        self,
        mock_nats,
        db_session: Session,
        test_account: Account,
    ):
        """Test that multiple matching flows are all triggered."""
        from preloop.models.crud import crud_flow

        # Create two flows for the same event
        flow1_in = FlowCreate(
            name="Flow 1",
            trigger_event_source="github",
            trigger_event_types=["push"],  # Use array field
            prompt_template="Flow 1 prompt",
            agent_type="openhands",
            agent_config={},
            account_id=test_account.id,
        )
        flow1 = crud_flow.create(
            db=db_session, flow_in=flow1_in, account_id=test_account.id
        )

        flow2_in = FlowCreate(
            name="Flow 2",
            trigger_event_source="github",
            trigger_event_types=["push"],  # Use array field
            prompt_template="Flow 2 prompt",
            agent_type="openhands",
            agent_config={},
            account_id=test_account.id,
        )
        flow2 = crud_flow.create(
            db=db_session, flow_in=flow2_in, account_id=test_account.id
        )

        mock_nats.return_value = AsyncMock()
        service = FlowTriggerService(db_session)

        event_data = {
            "source": "github",
            "type": "push",
            "payload": {"message": "Test commit"},
            "account_id": test_account.id,
        }

        with patch.object(
            service, "_start_flow_execution", new_callable=AsyncMock
        ) as mock_run:
            await service.process_event(event_data)

            # Verify orchestrator was triggered twice (once for each flow)
            assert mock_run.call_count == 2

    def test_matches_trigger_config_no_config(
        self, db_session: Session, test_flow: Flow
    ):
        """Test that flows without trigger_config always match."""
        service = FlowTriggerService(db_session)

        # Flow has no trigger_config
        test_flow.trigger_config = None

        event_data = {"source": "github", "type": "push", "payload": {}}

        assert service._matches_trigger_config(test_flow, event_data) is True

    def test_matches_trigger_config_simple_match(
        self, db_session: Session, conditional_flow: Flow
    ):
        """Test simple trigger_config matching."""
        service = FlowTriggerService(db_session)

        event_data = {
            "source": "github",
            "type": "push",
            "payload": {"branch": "main"},
        }

        assert service._matches_trigger_config(conditional_flow, event_data) is True

    def test_matches_trigger_config_simple_no_match(
        self, db_session: Session, conditional_flow: Flow
    ):
        """Test simple trigger_config non-matching."""
        service = FlowTriggerService(db_session)

        event_data = {
            "source": "github",
            "type": "push",
            "payload": {"branch": "develop"},
        }

        assert service._matches_trigger_config(conditional_flow, event_data) is False
