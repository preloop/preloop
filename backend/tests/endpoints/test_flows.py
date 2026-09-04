import uuid
from zoneinfo import ZoneInfo
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from pytest_mock import MockerFixture

from preloop.api.endpoints import flows
from preloop.models import schemas
from preloop.models.models.account import Account

from tests.conftest import maybe_await


@pytest.fixture
def mock_account(mocker: MockerFixture) -> Account:
    """Provides a mock Account object for testing."""
    account = MagicMock(spec=Account)
    account.id = uuid.uuid4()
    account.account_id = uuid.uuid4()
    account.email = "test@example.com"
    return account


@pytest.mark.asyncio
async def test_create_flow(mock_account: Account, mocker: MockerFixture):
    """Tests that a flow is created correctly."""
    # Arrange
    flow_in = schemas.FlowCreate(
        name="Test Flow",
        description="A test flow",
        trigger_event_source="github",
        trigger_event_types=["commit_to_main"],  # Use array field
        prompt_template="Test prompt",
        ai_model_id=uuid.uuid4(),
        agent_type="openhands",
        agent_config={"agent_type": "CodeActAgent"},
        allowed_mcp_servers=[],
        allowed_mcp_tools=[],
    )

    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    # Mock validation methods to return None (no conflicts)
    mock_crud_flow.get_by_name_and_account.return_value = None
    mock_crud_flow.get_global_preset_by_name.return_value = None

    flow_in.account_id = mock_account.id
    mock_crud_flow.create.return_value = schemas.FlowResponse(
        **flow_in.model_dump(),
        id=uuid.uuid4(),
        created_at=datetime.now(ZoneInfo("UTC")),
        updated_at=datetime.now(ZoneInfo("UTC")),
    )

    # Act
    result = await maybe_await(
        flows.create_flow(db=MagicMock(), flow_in=flow_in, current_user=mock_account)
    )

    # Assert
    assert result.name == flow_in.name
    mock_crud_flow.create.assert_called_once_with(
        db=mocker.ANY, flow_in=flow_in, account_id=mock_account.account_id
    )


@pytest.mark.asyncio
async def test_read_flows(mock_account: Account, mocker: MockerFixture):
    """Tests that flows are read correctly."""
    # Arrange
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    mock_crud_flow.get_multi.return_value = []

    # Act
    result = await maybe_await(
        flows.read_flows(db=MagicMock(), current_user=mock_account)
    )

    # Assert
    assert isinstance(result, list)
    mock_crud_flow.get_multi.assert_called_once_with(
        mocker.ANY, account_id=mock_account.account_id, skip=0, limit=100
    )


@pytest.mark.asyncio
async def test_read_flow(mock_account: Account, mocker: MockerFixture):
    """Tests that a single flow is read correctly."""
    # Arrange
    flow_id = uuid.uuid4()
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    mock_crud_flow.get.return_value = schemas.FlowResponse(
        id=flow_id,
        name="Test Flow",
        description="A test flow",
        trigger_event_source="github",
        trigger_event_types=["commit_to_main"],  # Use array field
        prompt_template="Test prompt",
        ai_model_id=uuid.uuid4(),
        created_at=datetime.now(ZoneInfo("UTC")),
        updated_at=datetime.now(ZoneInfo("UTC")),
        account_id=mock_account.account_id,
    )

    # Act
    result = await maybe_await(
        flows.read_flow(db=MagicMock(), flow_id=flow_id, current_user=mock_account)
    )

    # Assert
    assert result.id == flow_id
    mock_crud_flow.get.assert_called_once_with(
        db=mocker.ANY, id=flow_id, account_id=mock_account.account_id
    )


@pytest.mark.asyncio
async def test_update_flow(mock_account: Account, mocker: MockerFixture):
    """Tests that a flow is updated correctly."""
    # Arrange
    flow_id = uuid.uuid4()
    flow_update = schemas.FlowUpdate(name="Updated Name", current_user=mock_account)
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    mock_flow = MagicMock()
    mock_flow.name = "Original Name"  # Different from update name
    mock_flow.is_preset = False
    mock_crud_flow.get.return_value = mock_flow
    # Mock validation methods to return None (no conflicts)
    mock_crud_flow.get_by_name_and_account.return_value = None
    mock_crud_flow.get_global_preset_by_name.return_value = None

    mock_crud_flow.update.return_value = schemas.FlowResponse(
        id=flow_id,
        name=flow_update.name,
        description="A test flow",
        trigger_event_source="github",
        trigger_event_types=["commit_to_main"],  # Use array field
        prompt_template="Test prompt",
        ai_model_id=uuid.uuid4(),
        created_at=datetime.now(ZoneInfo("UTC")),
        updated_at=datetime.now(ZoneInfo("UTC")),
        account_id=mock_account.account_id,
    )

    # Act
    result = await maybe_await(
        flows.update_flow(
            db=MagicMock(),
            flow_id=flow_id,
            flow_in=flow_update,
            current_user=mock_account,
        )
    )

    # Assert
    assert result.name == flow_update.name
    mock_crud_flow.get.assert_called_once_with(
        db=mocker.ANY, id=flow_id, account_id=mock_account.account_id
    )
    mock_crud_flow.update.assert_called_once_with(
        db=mocker.ANY,
        db_obj=mock_flow,
        flow_in=flow_update,
        account_id=mock_account.account_id,
    )


@pytest.mark.asyncio
async def test_delete_flow(mock_account: Account, mocker: MockerFixture):
    """Tests that a flow is deleted correctly."""
    # Arrange
    flow_id = uuid.uuid4()
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get_running_by_flow.return_value = []
    mock_flow = MagicMock()
    mock_crud_flow.get.return_value = mock_flow

    # Act
    await maybe_await(
        flows.delete_flow(db=MagicMock(), flow_id=flow_id, current_user=mock_account)
    )

    # Assert
    mock_crud_flow.get.assert_called_once_with(
        db=mocker.ANY, id=flow_id, account_id=mock_account.account_id
    )
    mock_crud_flow.remove.assert_called_once_with(
        db=mocker.ANY, id=flow_id, account_id=mock_account.account_id
    )


@pytest.mark.asyncio
async def test_delete_flow_with_active_execution_conflict(
    mock_account: Account, mocker: MockerFixture
):
    """Deleting a flow with a running execution is refused with 409.

    A flow delete cascades to its executions and their logs. If an agent is
    still running, its log stream would reference a deleted execution row
    (foreign key violations in the log persister), so deletion must be
    refused until the executions are stopped.
    """
    # Arrange
    flow_id = uuid.uuid4()
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    running_execution = MagicMock()
    running_execution.status = "RUNNING"
    mock_crud_flow_execution.get_running_by_flow.return_value = [running_execution]
    mock_flow = MagicMock()
    mock_flow.is_preset = False
    mock_crud_flow.get.return_value = mock_flow

    # Act / Assert
    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.delete_flow(
                db=MagicMock(), flow_id=flow_id, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 409
    assert "active execution" in exc_info.value.detail
    assert "stop" in exc_info.value.detail.lower()
    # The flow must NOT have been removed.
    mock_crud_flow.remove.assert_not_called()
    # The guard checks the account-scoped running executions of this flow.
    mock_crud_flow_execution.get_running_by_flow.assert_called_once_with(
        mocker.ANY, flow_id=mock_flow.id, account_id=mock_account.account_id
    )


@pytest.mark.asyncio
async def test_read_flow_not_found(mock_account: Account, mocker: MockerFixture):
    """Tests that reading a non-existent flow raises HTTPException."""
    # Arrange
    flow_id = uuid.uuid4()
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    mock_crud_flow.get.return_value = None

    # Act & Assert
    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.read_flow(db=MagicMock(), flow_id=flow_id, current_user=mock_account)
        )

    assert exc_info.value.status_code == 404
    assert "not found" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_get_flow_execution_gateway_events(
    mock_account: Account, mocker: MockerFixture
):
    """Gateway event endpoint should return normalized model gateway log rows."""
    execution_id = uuid.uuid4()
    mock_execution = MagicMock()
    mock_execution.id = execution_id

    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get.return_value = mock_execution

    row = MagicMock()
    row.execution_id = execution_id
    row.timestamp = datetime.now(ZoneInfo("UTC"))
    row.log_type = "model_gateway_call"
    row.message = None
    row.metadata_ = {
        "outcome": "success",
        "model_alias": "openai/gpt-5",
        "estimated_cost": 0.1,
    }
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [row]
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    mock_db = MagicMock()
    mock_db.execute.return_value = mock_result

    result = await maybe_await(
        flows.get_flow_execution_gateway_events(
            db=mock_db,
            execution_id=execution_id,
            current_user=mock_account,
        )
    )

    assert result["source"] == "database"
    assert result["logs"][0]["type"] == "model_gateway_call"
    assert result["logs"][0]["payload"]["outcome"] == "success"
    assert result["logs"][0]["payload"]["model_alias"] == "openai/gpt-5"


@pytest.mark.asyncio
async def test_get_flow_execution_gateway_events_not_found(
    mock_account: Account, mocker: MockerFixture
):
    """Gateway event endpoint should raise 404 when execution is missing."""
    execution_id = uuid.uuid4()
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get.return_value = None

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.get_flow_execution_gateway_events(
                db=MagicMock(),
                execution_id=execution_id,
                current_user=mock_account,
            )
        )

    assert exc_info.value.status_code == 404
    assert "not found" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_update_flow_not_found(mock_account: Account, mocker: MockerFixture):
    """Tests that updating a non-existent flow raises HTTPException."""
    # Arrange
    flow_id = uuid.uuid4()
    flow_update = schemas.FlowUpdate(name="Updated Name", current_user=mock_account)
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    mock_crud_flow.get.return_value = None

    # Act & Assert
    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.update_flow(
                db=MagicMock(),
                flow_id=flow_id,
                flow_in=flow_update,
                current_user=mock_account,
            )
        )

    assert exc_info.value.status_code == 404
    assert "not found" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_delete_flow_not_found(mock_account: Account, mocker: MockerFixture):
    """Tests that deleting a non-existent flow raises HTTPException."""
    # Arrange
    flow_id = uuid.uuid4()
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    mock_crud_flow.get.return_value = None

    # Act & Assert
    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.delete_flow(
                db=MagicMock(), flow_id=flow_id, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 404
    assert "not found" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_read_presets(mock_account: Account, mocker: MockerFixture):
    """Tests that flow presets are read correctly."""
    # Arrange
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    global_preset = MagicMock()
    global_preset.is_preset = True
    global_preset.account_id = None
    account_preset = MagicMock()
    account_preset.is_preset = True
    account_preset.account_id = mock_account.account_id

    # The endpoint now uses get_presets_for_account which returns both
    mock_crud_flow.get_presets_for_account.return_value = [
        global_preset,
        account_preset,
    ]

    # Act
    result = await maybe_await(
        flows.read_presets(db=MagicMock(), current_user=mock_account)
    )

    # Assert
    assert len(result) == 2
    assert result[0] == global_preset
    assert result[1] == account_preset
    mock_crud_flow.get_presets_for_account.assert_called_once_with(
        mocker.ANY, account_id=mock_account.account_id
    )


def _make_preset(flow_id: uuid.UUID, ai_model_id=None):
    """Build a preset-like object with __dict__ support (like the ORM row)."""

    class PresetObj:
        pass

    preset = PresetObj()
    preset.id = flow_id
    preset.name = "Preset Flow"
    preset.description = "A preset"
    preset.is_preset = True
    preset.trigger_event_source = "github"
    preset.trigger_event_types = ["commit"]  # Use array field
    preset.prompt_template = "test"
    preset.ai_model_id = ai_model_id
    preset.agent_type = "codex"
    preset.agent_config = {"agent_type": "CodeActAgent"}
    preset.allowed_mcp_servers = []
    preset.allowed_mcp_tools = []
    return preset


def _make_ai_model(
    account_id,
    *,
    is_default: bool = False,
    provider_name: str = "openai",
    credentials_secret_id=None,
    api_key=None,
    meta_data=None,
    api_endpoint=None,
    created_at=None,
):
    """Build an AI-model-like object for clone binding tests."""
    from datetime import datetime, timezone

    model = MagicMock()
    model.id = uuid.uuid4()
    model.account_id = account_id
    model.is_default = is_default
    model.provider_name = provider_name
    model.credentials_secret_id = credentials_secret_id
    model.api_key = api_key
    model.meta_data = meta_data
    model.api_endpoint = api_endpoint
    model.model_kind = "llm"
    model.created_at = created_at or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return model


@pytest.mark.asyncio
async def test_clone_preset(mock_account: Account, mocker: MockerFixture):
    """Cloning a preset binds the account's default AI model at clone time."""
    # Arrange
    flow_id = uuid.uuid4()
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    mock_crud_ai_model = mocker.patch(
        "preloop.api.endpoints.flows.crud_ai_model",
        new_callable=MagicMock,
    )

    preset = _make_preset(flow_id)  # presets ship without a bound model
    mock_crud_flow.get.return_value = preset
    # Mock get_by_name_and_account to return None (no existing flow with that name)
    mock_crud_flow.get_by_name_and_account.return_value = None

    account_model = _make_ai_model(
        mock_account.account_id,
        is_default=True,
        credentials_secret_id=uuid.uuid4(),
    )
    mock_crud_ai_model.get_by_account.return_value = [account_model]

    # Convert mock_account.id to string for validation
    mock_account.id = str(mock_account.id)

    cloned_flow = MagicMock()
    cloned_flow.name = "Copy of Preset Flow"
    mock_crud_flow.create.return_value = cloned_flow

    # Act
    result = await maybe_await(
        flows.clone_preset(db=MagicMock(), flow_id=flow_id, current_user=mock_account)
    )

    # Assert
    assert result == cloned_flow
    mock_crud_flow.create.assert_called_once()
    flow_in = mock_crud_flow.create.call_args.kwargs["flow_in"]
    assert flow_in.ai_model_id == account_model.id


@pytest.mark.asyncio
async def test_clone_preset_prefers_default_model(
    mock_account: Account, mocker: MockerFixture
):
    """The account default model wins over newer non-default models."""
    from datetime import datetime, timezone

    flow_id = uuid.uuid4()
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow", new_callable=MagicMock
    )
    mock_crud_ai_model = mocker.patch(
        "preloop.api.endpoints.flows.crud_ai_model", new_callable=MagicMock
    )
    mock_crud_flow.get.return_value = _make_preset(flow_id)
    mock_crud_flow.get_by_name_and_account.return_value = None
    mock_account.id = str(mock_account.id)

    newer = _make_ai_model(
        mock_account.account_id,
        api_key="k",
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    default = _make_ai_model(
        mock_account.account_id,
        is_default=True,
        api_key="k",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    mock_crud_ai_model.get_by_account.return_value = [newer, default]
    mock_crud_flow.create.return_value = MagicMock()

    await maybe_await(
        flows.clone_preset(db=MagicMock(), flow_id=flow_id, current_user=mock_account)
    )

    flow_in = mock_crud_flow.create.call_args.kwargs["flow_in"]
    assert flow_in.ai_model_id == default.id


@pytest.mark.asyncio
async def test_clone_preset_skips_credential_less_models(
    mock_account: Account, mocker: MockerFixture
):
    """A model row with no credential source cannot be bound."""
    flow_id = uuid.uuid4()
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow", new_callable=MagicMock
    )
    mock_crud_ai_model = mocker.patch(
        "preloop.api.endpoints.flows.crud_ai_model", new_callable=MagicMock
    )
    mock_crud_flow.get.return_value = _make_preset(flow_id)
    mock_crud_flow.get_by_name_and_account.return_value = None
    mock_account.id = str(mock_account.id)

    bare = _make_ai_model(mock_account.account_id, is_default=True)  # no creds
    gateway = _make_ai_model(
        mock_account.account_id, meta_data={"gateway": {"enabled": True}}
    )
    mock_crud_ai_model.get_by_account.return_value = [bare, gateway]
    mock_crud_flow.create.return_value = MagicMock()

    await maybe_await(
        flows.clone_preset(db=MagicMock(), flow_id=flow_id, current_user=mock_account)
    )

    flow_in = mock_crud_flow.create.call_args.kwargs["flow_in"]
    assert flow_in.ai_model_id == gateway.id


@pytest.mark.asyncio
async def test_clone_preset_no_usable_model_fails_at_clone_time(
    mock_account: Account, mocker: MockerFixture
):
    """No usable account model: fail the CLONE with an actionable message,
    never let the first run die with a raw provider/gateway error."""
    flow_id = uuid.uuid4()
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow", new_callable=MagicMock
    )
    mock_crud_ai_model = mocker.patch(
        "preloop.api.endpoints.flows.crud_ai_model", new_callable=MagicMock
    )
    mock_crud_flow.get.return_value = _make_preset(flow_id)
    mock_crud_flow.get_by_name_and_account.return_value = None
    mock_crud_ai_model.get_by_account.return_value = []

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.clone_preset(
                db=MagicMock(), flow_id=flow_id, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 422
    detail = str(exc_info.value.detail)
    assert "codex" in detail
    assert "Add an AI model" in detail
    mock_crud_flow.create.assert_not_called()


@pytest.mark.asyncio
async def test_clone_preset_keeps_visible_preset_model(
    mock_account: Account, mocker: MockerFixture
):
    """A preset bound to a global model keeps that binding."""
    flow_id = uuid.uuid4()
    preset_model_id = uuid.uuid4()
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow", new_callable=MagicMock
    )
    mock_crud_ai_model = mocker.patch(
        "preloop.api.endpoints.flows.crud_ai_model", new_callable=MagicMock
    )
    mock_crud_flow.get.return_value = _make_preset(flow_id, ai_model_id=preset_model_id)
    mock_crud_flow.get_by_name_and_account.return_value = None
    mock_account.id = str(mock_account.id)

    global_model = _make_ai_model(None, api_key="k")  # account_id=None: global
    mock_crud_ai_model.get.return_value = global_model
    mock_crud_flow.create.return_value = MagicMock()

    await maybe_await(
        flows.clone_preset(db=MagicMock(), flow_id=flow_id, current_user=mock_account)
    )

    flow_in = mock_crud_flow.create.call_args.kwargs["flow_in"]
    assert flow_in.ai_model_id == preset_model_id
    mock_crud_ai_model.get_by_account.assert_not_called()


@pytest.mark.asyncio
async def test_clone_preset_codex_needs_endpoint_for_custom_provider(
    mock_account: Account, mocker: MockerFixture
):
    """codex + non-OpenAI provider without endpoint/gateway is not usable."""
    flow_id = uuid.uuid4()
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow", new_callable=MagicMock
    )
    mock_crud_ai_model = mocker.patch(
        "preloop.api.endpoints.flows.crud_ai_model", new_callable=MagicMock
    )
    mock_crud_flow.get.return_value = _make_preset(flow_id)
    mock_crud_flow.get_by_name_and_account.return_value = None

    endpointless = _make_ai_model(
        mock_account.account_id, provider_name="acme", api_key="k"
    )
    mock_crud_ai_model.get_by_account.return_value = [endpointless]

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.clone_preset(
                db=MagicMock(), flow_id=flow_id, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_clone_preset_not_found(mock_account: Account, mocker: MockerFixture):
    """Tests that cloning a non-existent preset raises HTTPException."""
    # Arrange
    flow_id = uuid.uuid4()
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    mock_crud_flow.get.return_value = None

    # Act & Assert
    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.clone_preset(
                db=MagicMock(), flow_id=flow_id, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 404
    assert "not found" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_clone_preset_not_a_preset(mock_account: Account, mocker: MockerFixture):
    """Tests that cloning a non-preset flow raises HTTPException."""
    # Arrange
    flow_id = uuid.uuid4()
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )

    flow = MagicMock()
    flow.is_preset = False
    mock_crud_flow.get.return_value = flow

    # Act & Assert
    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.clone_preset(
                db=MagicMock(), flow_id=flow_id, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 404
    assert "not found" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_read_flow_executions(mock_account: Account, mocker: MockerFixture):
    """Tests that flow executions are read correctly."""
    # Arrange
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get_multi.return_value = []

    # Act
    result = await maybe_await(
        flows.read_flow_executions(db=MagicMock(), current_user=mock_account)
    )

    # Assert
    assert isinstance(result, list)
    mock_crud_flow_execution.get_multi.assert_called_once_with(
        mocker.ANY,
        account_id=mock_account.account_id,
        skip=0,
        limit=25,
        flow_id=None,
        statuses=None,
        eager_load=True,
        lightweight=True,
    )


def test_flow_execution_list_schema_excludes_detail_payloads():
    """List rows do not serialize heavy detail/log payloads."""
    fields = set(schemas.FlowExecutionListResponse.model_fields)

    assert "resolved_input_prompt" not in fields
    assert "actions_taken_summary" not in fields
    assert "mcp_usage_logs" not in fields
    assert "execution_logs" not in fields
    assert "result" not in fields


def test_flow_execution_schemas_expose_failure_category():
    """Failures must be groupable from the list view, not only readable.

    The category is the only field that lets a console (or an operator with
    curl) answer "what is breaking?" without parsing a hundred free-text
    error messages, so it has to be on the lightweight list row too.
    """
    assert "failure_category" in schemas.FlowExecutionListResponse.model_fields
    assert "failure_category" in schemas.FlowExecutionResponse.model_fields
    assert "runner" in schemas.FlowExecutionListResponse.model_fields
    assert "runner" in schemas.FlowExecutionResponse.model_fields


def test_lightweight_execution_list_loads_the_failure_category():
    """The column must be in load_only, or the list lazy-loads it per row."""
    import inspect

    from preloop.models.crud.flow_execution import CRUDFlowExecution

    source = inspect.getsource(CRUDFlowExecution.get_multi)
    assert "FlowExecution.failure_category" in source
    assert "FlowExecution.runner_id" in source
    assert "FlowExecution.agent_session_reference" in source


@pytest.mark.asyncio
async def test_read_flow_executions_filters_and_caps_limit(
    mock_account: Account, mocker: MockerFixture
):
    """Flow execution lists are bounded and filtered server-side."""
    flow_id = uuid.uuid4()
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get_multi.return_value = []

    result = await maybe_await(
        flows.read_flow_executions(
            db=MagicMock(),
            skip=5,
            limit=500,
            flow_id=flow_id,
            status=["running,failed", "pending"],
            current_user=mock_account,
        )
    )

    assert result == []
    mock_crud_flow_execution.get_multi.assert_called_once_with(
        mocker.ANY,
        account_id=mock_account.account_id,
        skip=5,
        limit=100,
        flow_id=flow_id,
        statuses=["RUNNING", "FAILED", "PENDING"],
        eager_load=True,
        lightweight=True,
    )


@pytest.mark.asyncio
async def test_read_flow_execution(mock_account: Account, mocker: MockerFixture):
    """Tests that reading a single flow execution works correctly."""
    # Arrange
    execution_id = uuid.uuid4()
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )

    execution = MagicMock()
    execution.id = execution_id
    mock_crud_flow_execution.get.return_value = execution

    # Act
    result = await maybe_await(
        flows.read_flow_execution(
            db=MagicMock(), execution_id=execution_id, current_user=mock_account
        )
    )

    # Assert
    assert result == execution
    mock_crud_flow_execution.get.assert_called_once_with(
        db=mocker.ANY, id=execution_id, account_id=mock_account.account_id
    )


@pytest.mark.asyncio
async def test_read_flow_execution_hydrates_mcp_usage_logs_from_activity(
    mock_account: Account, mocker: MockerFixture
):
    """When mcp_usage_logs is empty, tool calls are loaded via activity CRUD."""
    execution_id = uuid.uuid4()
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_activity = mocker.patch(
        "preloop.api.endpoints.flows.crud_runtime_session_activity",
        new_callable=MagicMock,
    )

    execution = MagicMock()
    execution.id = execution_id
    execution.mcp_usage_logs = None
    mock_crud_flow_execution.get.return_value = execution

    row = MagicMock()
    row.timestamp = datetime.now(ZoneInfo("UTC"))
    row.tool_name = "t"
    row.server_name = "s"
    row.status = "success"
    row.summary = None
    row.metadata_ = {}
    mock_crud_activity.list_tool_calls_for_flow_execution.return_value = [row]

    db = MagicMock()
    result = await maybe_await(
        flows.read_flow_execution(
            db=db, execution_id=execution_id, current_user=mock_account
        )
    )

    assert result is execution
    mock_crud_activity.list_tool_calls_for_flow_execution.assert_called_once_with(
        db,
        account_id=mock_account.account_id,
        flow_execution_id=execution_id,
    )
    assert execution.mcp_usage_logs is not None
    assert len(execution.mcp_usage_logs) == 1
    assert execution.mcp_usage_logs[0]["tool_name"] == "t"


@pytest.mark.asyncio
async def test_read_flow_execution_not_found(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that reading a non-existent flow execution raises HTTPException."""
    # Arrange
    execution_id = uuid.uuid4()
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get.return_value = None

    # Act & Assert
    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.read_flow_execution(
                db=MagicMock(), execution_id=execution_id, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 404
    assert "not found" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_get_flow_execution_result(mock_account: Account, mocker: MockerFixture):
    """Tests that the structured result artifact is returned when present."""
    execution_id = uuid.uuid4()
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )

    execution = MagicMock()
    execution.id = execution_id
    execution.status = "SUCCEEDED"
    execution.result = {
        "schema": "preloop.eval.result/v1",
        "status": "pass",
        "summary": "All checks passed",
        "metrics": {"latency_ms": 42},
    }
    mock_crud_flow_execution.get.return_value = execution

    result = await maybe_await(
        flows.get_flow_execution_result(
            db=MagicMock(), execution_id=execution_id, current_user=mock_account
        )
    )

    assert result == {
        "execution_id": str(execution_id),
        "status": "SUCCEEDED",
        "result": execution.result,
    }
    mock_crud_flow_execution.get.assert_called_once_with(
        db=mocker.ANY, id=execution_id, account_id=mock_account.account_id
    )


@pytest.mark.asyncio
async def test_get_flow_execution_result_no_artifact(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that 404 is raised when the execution reported no result."""
    execution_id = uuid.uuid4()
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )

    execution = MagicMock()
    execution.id = execution_id
    execution.result = None
    mock_crud_flow_execution.get.return_value = execution

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.get_flow_execution_result(
                db=MagicMock(), execution_id=execution_id, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 404
    assert "result" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_get_flow_execution_evidence(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that the captured evidence pack is served as a tar.gz download."""
    execution_id = uuid.uuid4()
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )

    execution = MagicMock()
    execution.id = execution_id
    execution.evidence_archive = b"\x1f\x8b-fake-gzip-bytes"
    mock_crud_flow_execution.get.return_value = execution

    response = await maybe_await(
        flows.get_flow_execution_evidence(
            db=MagicMock(), execution_id=execution_id, current_user=mock_account
        )
    )

    assert response.body == b"\x1f\x8b-fake-gzip-bytes"
    assert response.media_type == "application/gzip"
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="evidence-{execution_id}.tar.gz"'
    )
    mock_crud_flow_execution.get.assert_called_once_with(
        db=mocker.ANY, id=execution_id, account_id=mock_account.account_id
    )


@pytest.mark.asyncio
async def test_get_flow_execution_evidence_none_captured(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that 404 is raised when no evidence pack was captured."""
    execution_id = uuid.uuid4()
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )

    execution = MagicMock()
    execution.id = execution_id
    execution.evidence_archive = None
    mock_crud_flow_execution.get.return_value = execution

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.get_flow_execution_evidence(
                db=MagicMock(), execution_id=execution_id, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 404
    assert "evidence" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_get_flow_execution_evidence_execution_not_found(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that 404 is raised for a non-existent execution."""
    execution_id = uuid.uuid4()
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get.return_value = None

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.get_flow_execution_evidence(
                db=MagicMock(), execution_id=execution_id, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 404
    assert "not found" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_get_flow_execution_workspace(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that the captured workspace snapshot is served as a tar.gz download."""
    execution_id = uuid.uuid4()
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )

    execution = MagicMock()
    execution.id = execution_id
    execution.workspace_snapshot = b"\x1f\x8b-fake-workspace-gzip"
    mock_crud_flow_execution.get.return_value = execution

    response = await maybe_await(
        flows.get_flow_execution_workspace(
            db=MagicMock(), execution_id=execution_id, current_user=mock_account
        )
    )

    assert response.body == b"\x1f\x8b-fake-workspace-gzip"
    assert response.media_type == "application/gzip"
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="workspace-{execution_id}.tar.gz"'
    )
    mock_crud_flow_execution.get.assert_called_once_with(
        db=mocker.ANY, id=execution_id, account_id=mock_account.account_id
    )


@pytest.mark.asyncio
async def test_get_flow_execution_workspace_none_captured(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that 404 is raised when no workspace snapshot was captured."""
    execution_id = uuid.uuid4()
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )

    execution = MagicMock()
    execution.id = execution_id
    execution.workspace_snapshot = None
    mock_crud_flow_execution.get.return_value = execution

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.get_flow_execution_workspace(
                db=MagicMock(), execution_id=execution_id, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 404
    assert "workspace" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_get_flow_execution_workspace_execution_not_found(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that 404 is raised for a non-existent execution."""
    execution_id = uuid.uuid4()
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get.return_value = None

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.get_flow_execution_workspace(
                db=MagicMock(), execution_id=execution_id, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 404
    assert "not found" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_get_flow_execution_result_execution_not_found(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that 404 is raised for a non-existent execution."""
    execution_id = uuid.uuid4()
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get.return_value = None

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.get_flow_execution_result(
                db=MagicMock(), execution_id=execution_id, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 404
    assert "not found" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_send_execution_command_execution_not_found(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that sending a command to non-existent execution raises HTTPException."""
    # Arrange
    execution_id = uuid.uuid4()
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get.return_value = None

    # Mock get_nats_client
    mocker.patch(
        "preloop.sync.services.event_bus.get_nats_client",
        return_value=MagicMock(),
    )

    command_data = schemas.FlowExecutionCommand(
        command="stop",
        payload={},
    )

    # Act & Assert
    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.send_execution_command(
                db=MagicMock(),
                execution_id=execution_id,
                command_data=command_data,
                current_user=mock_account,
            )
        )

    assert exc_info.value.status_code == 404
    assert "not found" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_send_execution_command_stop_success(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that sending a stop command works correctly."""
    # Arrange
    execution_id = uuid.uuid4()
    flow_id = uuid.uuid4()

    mock_execution = MagicMock()
    mock_execution.id = execution_id
    mock_execution.flow_id = flow_id
    mock_execution.status = "RUNNING"
    mock_execution.agent_session_reference = "test-session-123"

    mock_flow = MagicMock()
    mock_flow.agent_type = "codex"

    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get.return_value = mock_execution
    mock_crud_flow_execution.update.return_value = mock_execution

    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    mock_crud_flow.get.return_value = mock_flow

    # Mock get_nats_client
    mock_nats_client = MagicMock()
    mock_get_nats_client = mocker.patch(
        "preloop.sync.services.event_bus.get_nats_client",
        return_value=mock_nats_client,
    )

    # Mock CodexAgent
    mock_agent = MagicMock()
    mock_agent.get_logs = mocker.AsyncMock(return_value=["log line 1", "log line 2"])
    mock_agent.stop = mocker.AsyncMock()
    mocker.patch(
        "preloop.agents.codex.CodexAgent",
        return_value=mock_agent,
    )

    # Mock FlowExecutionOrchestrator.send_command
    mock_send_command = mocker.patch(
        "preloop.services.flow_orchestrator.FlowExecutionOrchestrator.send_command",
        new=mocker.AsyncMock(),
    )

    command_data = schemas.FlowExecutionCommand(
        command="stop",
        payload={},
    )

    # Act
    result = await maybe_await(
        flows.send_execution_command(
            db=MagicMock(),
            execution_id=execution_id,
            command_data=command_data,
            current_user=mock_account,
        )
    )

    # Assert
    assert result == {"status": "stopped"}
    mock_get_nats_client.assert_called_once()
    mock_agent.stop.assert_called_once_with("test-session-123")

    # Update is called once (status). Logs are persisted via append_log.
    assert mock_crud_flow_execution.update.call_count == 1
    assert mock_crud_flow_execution.append_log.call_count == 2  # 2 log lines

    # Verify the final update call has status='STOPPED'
    final_call = mock_crud_flow_execution.update.call_args
    assert final_call.kwargs["obj_in"].status == "STOPPED"
    assert final_call.kwargs["obj_in"].error_message == "Manually stopped by user"

    # Verify send_command was called with nats_client
    mock_send_command.assert_called_once()
    call_kwargs = mock_send_command.call_args.kwargs
    assert call_kwargs["execution_id"] == str(execution_id)
    assert call_kwargs["command"] == "stop"
    assert call_kwargs["nats_client"] == mock_nats_client


@pytest.mark.asyncio
async def test_send_execution_command_other_command_success(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that sending a non-stop command works correctly via NATS."""
    # Arrange
    execution_id = uuid.uuid4()

    mock_execution = MagicMock()
    mock_execution.id = execution_id

    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get.return_value = mock_execution

    # Mock get_nats_client
    mock_nats_client = MagicMock()
    mock_get_nats_client = mocker.patch(
        "preloop.sync.services.event_bus.get_nats_client",
        return_value=mock_nats_client,
    )

    # Mock FlowExecutionOrchestrator.send_command
    mock_send_command = mocker.patch(
        "preloop.services.flow_orchestrator.FlowExecutionOrchestrator.send_command",
        new=mocker.AsyncMock(),
    )

    command_data = schemas.FlowExecutionCommand(
        command="send_message",
        payload={"message": "test message"},
    )

    # Act
    result = await maybe_await(
        flows.send_execution_command(
            db=MagicMock(),
            execution_id=execution_id,
            command_data=command_data,
            current_user=mock_account,
        )
    )

    # Assert
    assert result == {"status": "command_sent"}
    mock_get_nats_client.assert_called_once()

    # Verify send_command was called with nats_client
    mock_send_command.assert_called_once()
    call_kwargs = mock_send_command.call_args.kwargs
    assert call_kwargs["execution_id"] == str(execution_id)
    assert call_kwargs["command"] == "send_message"
    assert call_kwargs["payload"] == {"message": "test message"}
    assert call_kwargs["nats_client"] == mock_nats_client


@pytest.mark.asyncio
async def test_send_execution_command_nats_failure(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that command sending fails gracefully when NATS is unavailable."""
    # Arrange
    execution_id = uuid.uuid4()

    mock_execution = MagicMock()
    mock_execution.id = execution_id

    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get.return_value = mock_execution

    # Mock get_nats_client to raise an exception
    mocker.patch(
        "preloop.sync.services.event_bus.get_nats_client",
        side_effect=Exception("NATS connection failed"),
    )

    # Mock FlowExecutionOrchestrator.send_command to raise an exception
    mocker.patch(
        "preloop.services.flow_orchestrator.FlowExecutionOrchestrator.send_command",
        new=mocker.AsyncMock(side_effect=RuntimeError("NATS client not available")),
    )

    command_data = schemas.FlowExecutionCommand(
        command="send_message",
        payload={"message": "test message"},
    )

    # Act & Assert
    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.send_execution_command(
                db=MagicMock(),
                execution_id=execution_id,
                command_data=command_data,
                current_user=mock_account,
            )
        )

    assert exc_info.value.status_code == 500
    assert "Failed to send command" in str(exc_info.value.detail)


# ============================================================================
# Retry Flow Execution Tests
# ============================================================================


@pytest.mark.asyncio
async def test_retry_flow_execution_not_found(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that retrying a non-existent execution raises 404."""
    # Arrange
    execution_id = uuid.uuid4()
    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get.return_value = None

    # Act & Assert
    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.retry_flow_execution(
                db=MagicMock(), execution_id=execution_id, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 404
    assert "not found" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_retry_flow_execution_non_retryable_status(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that retrying an execution in a non-retryable status raises 400."""
    # Arrange
    execution_id = uuid.uuid4()
    flow_id = uuid.uuid4()

    mock_execution = MagicMock()
    mock_execution.id = execution_id
    mock_execution.flow_id = flow_id
    mock_execution.status = "RUNNING"  # Not retryable

    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get.return_value = mock_execution

    # Act & Assert
    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.retry_flow_execution(
                db=MagicMock(), execution_id=execution_id, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 400
    assert "cannot be retried" in str(exc_info.value.detail).lower()
    assert "RUNNING" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_retry_flow_execution_succeeded_not_retryable(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that retrying a succeeded execution raises 400."""
    # Arrange
    execution_id = uuid.uuid4()
    flow_id = uuid.uuid4()

    mock_execution = MagicMock()
    mock_execution.id = execution_id
    mock_execution.flow_id = flow_id
    mock_execution.status = "SUCCEEDED"  # Not retryable

    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get.return_value = mock_execution

    # Act & Assert
    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.retry_flow_execution(
                db=MagicMock(), execution_id=execution_id, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 400
    assert "cannot be retried" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_retry_flow_execution_flow_deleted(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that retrying an execution whose flow was deleted raises 404."""
    # Arrange
    execution_id = uuid.uuid4()
    flow_id = uuid.uuid4()

    mock_execution = MagicMock()
    mock_execution.id = execution_id
    mock_execution.flow_id = flow_id
    mock_execution.status = "FAILED"  # Retryable

    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get.return_value = mock_execution

    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    mock_crud_flow.get.return_value = None  # Flow no longer exists

    # Act & Assert
    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.retry_flow_execution(
                db=MagicMock(), execution_id=execution_id, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 404
    assert "no longer exists" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_trigger_flow_execution_records_who_ran_it(
    mock_account: Account, mocker: MockerFixture
):
    """A manual run carries the operator's name into the execution subject.

    Without it the console can only say "Manual Test Run" for every run of
    every flow, which is exactly the row that wave 4 set out to fix.
    """
    # Arrange
    flow_id = uuid.uuid4()
    execution_id = uuid.uuid4()

    mock_flow = MagicMock()
    mock_flow.id = flow_id
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    mock_crud_flow.get.return_value = mock_flow

    mock_trigger_service = MagicMock()
    mock_trigger_service.trigger_flow = mocker.AsyncMock(
        return_value={"id": str(execution_id), "status": "PENDING"}
    )
    mocker.patch(
        "preloop.services.flow_trigger_service.FlowTriggerService",
        return_value=mock_trigger_service,
    )

    # Act
    await maybe_await(
        flows.trigger_flow_execution(
            db=MagicMock(),
            flow_id=flow_id,
            current_user=mock_account,
            trigger_event_data={"issue": 7},
        )
    )

    # Assert
    call_kwargs = mock_trigger_service.trigger_flow.call_args.kwargs
    assert call_kwargs["test_mode"] is True
    assert call_kwargs["triggered_by"] == mock_account.email


def test_display_name_prefers_the_person_over_the_login():
    """The row should read like a person, not like a database column."""
    user = MagicMock()
    user.full_name = "Jane Doe"
    user.username = "jdoe"
    user.email = "jane.doe@example.com"
    assert flows._display_name(user) == "Jane Doe"

    user.full_name = "   "
    assert flows._display_name(user) == "jdoe"

    user.username = None
    assert flows._display_name(user) == "jane.doe@example.com"


@pytest.mark.asyncio
async def test_retry_flow_execution_success_failed(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that retrying a failed execution works correctly."""
    # Arrange
    execution_id = uuid.uuid4()
    flow_id = uuid.uuid4()
    new_execution_id = uuid.uuid4()

    mock_execution = MagicMock()
    mock_execution.id = execution_id
    mock_execution.flow_id = flow_id
    mock_execution.status = "FAILED"
    mock_execution.trigger_event_details = {"event": "test", "data": {"pr": 123}}

    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get.return_value = mock_execution

    mock_flow = MagicMock()
    mock_flow.id = flow_id
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    mock_crud_flow.get.return_value = mock_flow

    # Mock the FlowTriggerService - patch at the source since it's imported inside the function
    mock_trigger_service = MagicMock()
    mock_trigger_service.trigger_flow = mocker.AsyncMock(
        return_value={
            "id": str(new_execution_id),
            "status": "PENDING",
            "flow_id": str(flow_id),
        }
    )
    mocker.patch(
        "preloop.services.flow_trigger_service.FlowTriggerService",
        return_value=mock_trigger_service,
    )

    # Act
    result = await maybe_await(
        flows.retry_flow_execution(
            db=MagicMock(), execution_id=execution_id, current_user=mock_account
        )
    )

    # Assert - backend returns { id, status, flow_id }
    assert result["id"] == str(new_execution_id)
    assert result["status"] == "PENDING"

    # Verify trigger_flow was called with correct parameters
    mock_trigger_service.trigger_flow.assert_called_once()
    call_kwargs = mock_trigger_service.trigger_flow.call_args.kwargs
    assert call_kwargs["flow_id"] == flow_id
    assert call_kwargs["test_mode"] is False
    assert call_kwargs["trigger_event_data"] == mock_execution.trigger_event_details
    assert call_kwargs["retry_of_execution_id"] == execution_id
    # A retry is attributed to whoever pressed retry, not to the original run.
    assert call_kwargs["triggered_by"] == mock_account.email


@pytest.mark.asyncio
async def test_retry_flow_execution_success_stopped(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that retrying a stopped execution works correctly."""
    # Arrange
    execution_id = uuid.uuid4()
    flow_id = uuid.uuid4()
    new_execution_id = uuid.uuid4()

    mock_execution = MagicMock()
    mock_execution.id = execution_id
    mock_execution.flow_id = flow_id
    mock_execution.status = "STOPPED"
    mock_execution.trigger_event_details = {"event": "push"}

    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get.return_value = mock_execution

    mock_flow = MagicMock()
    mock_flow.id = flow_id
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    mock_crud_flow.get.return_value = mock_flow

    # Mock the FlowTriggerService - patch at the source since it's imported inside the function
    mock_trigger_service = MagicMock()
    mock_trigger_service.trigger_flow = mocker.AsyncMock(
        return_value={
            "id": str(new_execution_id),
            "status": "PENDING",
            "flow_id": str(flow_id),
        }
    )
    mocker.patch(
        "preloop.services.flow_trigger_service.FlowTriggerService",
        return_value=mock_trigger_service,
    )

    # Act
    result = await maybe_await(
        flows.retry_flow_execution(
            db=MagicMock(), execution_id=execution_id, current_user=mock_account
        )
    )

    # Assert - backend returns { id, status, flow_id }
    assert result["id"] == str(new_execution_id)

    # Verify retry_of_execution_id is passed
    call_kwargs = mock_trigger_service.trigger_flow.call_args.kwargs
    assert call_kwargs["retry_of_execution_id"] == execution_id


@pytest.mark.asyncio
async def test_retry_flow_execution_success_timeout(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that retrying a timed-out execution works correctly."""
    # Arrange
    execution_id = uuid.uuid4()
    flow_id = uuid.uuid4()
    new_execution_id = uuid.uuid4()

    mock_execution = MagicMock()
    mock_execution.id = execution_id
    mock_execution.flow_id = flow_id
    mock_execution.status = "TIMEOUT"
    mock_execution.trigger_event_details = {}

    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get.return_value = mock_execution

    mock_flow = MagicMock()
    mock_flow.id = flow_id
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    mock_crud_flow.get.return_value = mock_flow

    # Mock the FlowTriggerService - patch at the source since it's imported inside the function
    mock_trigger_service = MagicMock()
    mock_trigger_service.trigger_flow = mocker.AsyncMock(
        return_value={
            "id": str(new_execution_id),
            "status": "PENDING",
            "flow_id": str(flow_id),
        }
    )
    mocker.patch(
        "preloop.services.flow_trigger_service.FlowTriggerService",
        return_value=mock_trigger_service,
    )

    # Act
    result = await maybe_await(
        flows.retry_flow_execution(
            db=MagicMock(), execution_id=execution_id, current_user=mock_account
        )
    )

    # Assert - backend returns { id, status, flow_id }
    assert result["id"] == str(new_execution_id)


@pytest.mark.asyncio
async def test_retry_flow_execution_success_cancelled(
    mock_account: Account, mocker: MockerFixture
):
    """Tests that retrying a cancelled execution works correctly."""
    # Arrange
    execution_id = uuid.uuid4()
    flow_id = uuid.uuid4()
    new_execution_id = uuid.uuid4()

    mock_execution = MagicMock()
    mock_execution.id = execution_id
    mock_execution.flow_id = flow_id
    mock_execution.status = "CANCELLED"
    mock_execution.trigger_event_details = {
        "pr_url": "https://github.com/org/repo/pull/1"
    }

    mock_crud_flow_execution = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution",
        new_callable=MagicMock,
    )
    mock_crud_flow_execution.get.return_value = mock_execution

    mock_flow = MagicMock()
    mock_flow.id = flow_id
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow",
        new_callable=MagicMock,
    )
    mock_crud_flow.get.return_value = mock_flow

    # Mock the FlowTriggerService - patch at the source since it's imported inside the function
    mock_trigger_service = MagicMock()
    mock_trigger_service.trigger_flow = mocker.AsyncMock(
        return_value={
            "id": str(new_execution_id),
            "status": "PENDING",
            "flow_id": str(flow_id),
        }
    )
    mocker.patch(
        "preloop.services.flow_trigger_service.FlowTriggerService",
        return_value=mock_trigger_service,
    )

    # Act
    result = await maybe_await(
        flows.retry_flow_execution(
            db=MagicMock(), execution_id=execution_id, current_user=mock_account
        )
    )

    # Assert - backend returns { id, status, flow_id }
    assert result["id"] == str(new_execution_id)

    # Verify trigger_event_data is preserved from original execution
    call_kwargs = mock_trigger_service.trigger_flow.call_args.kwargs
    assert call_kwargs["trigger_event_data"] == mock_execution.trigger_event_details


# ---------------------------------------------------------------------------
# Schedule (cron) trigger endpoint guards
# ---------------------------------------------------------------------------

from preloop.models.schemas.flow import (  # noqa: E402
    CronSchedule,
    DailySchedule,
    IntervalSchedule,
    WeeklySchedule,
)


def _schedule_flow_create(**overrides) -> schemas.FlowCreate:
    """Build a FlowCreate payload for a schedule-triggered flow."""
    payload = dict(
        name="Nightly Scan",
        prompt_template="Run the nightly scan",
        agent_type="openhands",
        agent_config={},
        trigger_event_source="schedule",
        schedule_config=CronSchedule(expr="0 2 * * *", timezone="UTC"),
    )
    payload.update(overrides)
    return schemas.FlowCreate(**payload)


def _mock_crud_flow_no_conflicts(mocker: MockerFixture) -> MagicMock:
    mock_crud_flow = mocker.patch(
        "preloop.api.endpoints.flows.crud_flow", new_callable=MagicMock
    )
    mock_crud_flow.get_by_name_and_account.return_value = None
    mock_crud_flow.get_global_preset_by_name.return_value = None
    return mock_crud_flow


@pytest.mark.asyncio
async def test_create_schedule_flow_without_config_rejected(
    mock_account: Account, mocker: MockerFixture
):
    """trigger_event_source='schedule' without schedule_config is a 400."""
    _mock_crud_flow_no_conflicts(mocker)
    flow_in = _schedule_flow_create(schedule_config=None)

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.create_flow(
                db=MagicMock(), flow_in=flow_in, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 400
    assert "schedule_config" in exc_info.value.detail


@pytest.mark.asyncio
async def test_create_flow_with_schedule_config_defaults_source(
    mock_account: Account, mocker: MockerFixture
):
    """schedule_config alone implies a schedule trigger (like webhook does).

    The source is defaulted, trigger_event_types is forced to ['schedule'],
    no webhook secret is generated, and the response carries schedule_state.
    """
    mock_crud_flow = _mock_crud_flow_no_conflicts(mocker)
    flow_in = _schedule_flow_create(
        trigger_event_source=None, trigger_event_types=["schedule"]
    )

    def fake_create(db, flow_in, account_id):
        return schemas.FlowResponse(
            **flow_in.model_dump(),
            id=uuid.uuid4(),
            created_at=datetime.now(ZoneInfo("UTC")),
            updated_at=datetime.now(ZoneInfo("UTC")),
        )

    mock_crud_flow.create.side_effect = fake_create

    result = await maybe_await(
        flows.create_flow(db=MagicMock(), flow_in=flow_in, current_user=mock_account)
    )

    assert flow_in.trigger_event_source == "schedule"
    assert flow_in.trigger_event_types == ["schedule"]
    assert flow_in.webhook_config is None
    assert result.schedule_state is not None
    assert result.schedule_state["active"] is True
    assert result.schedule_state["cron"] == "0 2 * * *"
    assert result.schedule_state["timezone"] == "UTC"
    assert result.schedule_state["next_run_at"] is not None


@pytest.mark.asyncio
async def test_create_flow_forces_schedule_event_types(
    mock_account: Account, mocker: MockerFixture
):
    """Client-supplied trigger_event_types are overridden for schedules."""
    mock_crud_flow = _mock_crud_flow_no_conflicts(mocker)
    flow_in = _schedule_flow_create(trigger_event_types=["pull_request_created"])
    mock_crud_flow.create.return_value = schemas.FlowResponse(
        **flow_in.model_dump(exclude={"trigger_event_types"}),
        trigger_event_types=["schedule"],
        id=uuid.uuid4(),
        created_at=datetime.now(ZoneInfo("UTC")),
        updated_at=datetime.now(ZoneInfo("UTC")),
    )

    await maybe_await(
        flows.create_flow(db=MagicMock(), flow_in=flow_in, current_user=mock_account)
    )

    assert flow_in.trigger_event_types == ["schedule"]
    mock_crud_flow.create.assert_called_once()


@pytest.mark.asyncio
async def test_create_flow_schedule_config_with_other_source_rejected(
    mock_account: Account, mocker: MockerFixture
):
    """schedule_config on a non-schedule trigger source is a 400, not inert."""
    _mock_crud_flow_no_conflicts(mocker)
    flow_in = _schedule_flow_create(
        trigger_event_source="github", trigger_event_types=["commit_to_main"]
    )

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.create_flow(
                db=MagicMock(), flow_in=flow_in, current_user=mock_account
            )
        )

    assert exc_info.value.status_code == 400
    assert "trigger_event_source" in exc_info.value.detail


@pytest.mark.asyncio
async def test_update_schedule_flow_cannot_clear_config(
    mock_account: Account, mocker: MockerFixture
):
    """Explicitly clearing schedule_config on a schedule flow is a 400."""
    mock_crud_flow = _mock_crud_flow_no_conflicts(mocker)
    mock_flow = MagicMock()
    mock_flow.name = "Nightly Scan"
    mock_flow.trigger_event_source = "schedule"
    mock_flow.schedule_config = {"cron": "0 2 * * *", "timezone": "UTC"}
    mock_flow.source_preset_id = None
    mock_crud_flow.get.return_value = mock_flow

    flow_update = schemas.FlowUpdate(schedule_config=None)

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.update_flow(
                db=MagicMock(),
                flow_id=uuid.uuid4(),
                flow_in=flow_update,
                current_user=mock_account,
            )
        )

    assert exc_info.value.status_code == 400
    assert "schedule_config" in exc_info.value.detail
    mock_crud_flow.update.assert_not_called()


@pytest.mark.asyncio
async def test_update_flow_to_schedule_source_requires_config(
    mock_account: Account, mocker: MockerFixture
):
    """Switching a flow to schedule without a stored/sent config is a 400."""
    mock_crud_flow = _mock_crud_flow_no_conflicts(mocker)
    mock_flow = MagicMock()
    mock_flow.name = "Webhook Flow"
    mock_flow.trigger_event_source = "webhook"
    mock_flow.schedule_config = None
    mock_flow.source_preset_id = None
    mock_crud_flow.get.return_value = mock_flow

    flow_update = schemas.FlowUpdate(trigger_event_source="schedule")

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.update_flow(
                db=MagicMock(),
                flow_id=uuid.uuid4(),
                flow_in=flow_update,
                current_user=mock_account,
            )
        )

    assert exc_info.value.status_code == 400
    assert "schedule_config" in exc_info.value.detail
    mock_crud_flow.update.assert_not_called()


@pytest.mark.asyncio
async def test_update_flow_schedule_config_on_webhook_flow_rejected(
    mock_account: Account, mocker: MockerFixture
):
    """Sending schedule_config to a webhook flow (source unchanged) is a 400."""
    mock_crud_flow = _mock_crud_flow_no_conflicts(mocker)
    mock_flow = MagicMock()
    mock_flow.name = "Webhook Flow"
    mock_flow.trigger_event_source = "webhook"
    mock_flow.schedule_config = None
    mock_flow.source_preset_id = None
    mock_crud_flow.get.return_value = mock_flow

    flow_update = schemas.FlowUpdate(
        schedule_config=CronSchedule(expr="0 2 * * *", timezone="UTC")
    )

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            flows.update_flow(
                db=MagicMock(),
                flow_id=uuid.uuid4(),
                flow_in=flow_update,
                current_user=mock_account,
            )
        )

    assert exc_info.value.status_code == 400
    assert "trigger_event_source" in exc_info.value.detail
    mock_crud_flow.update.assert_not_called()


@pytest.mark.asyncio
async def test_update_flow_switch_to_webhook_generates_secret(
    mock_account: Account, mocker: MockerFixture
):
    """Switching a flow with no webhook_config to a webhook trigger
    auto-generates a webhook secret (mirrors the create path; cloned
    presets start with trigger_event_source=None and no secret)."""
    mock_crud_flow = _mock_crud_flow_no_conflicts(mocker)
    flow_id = uuid.uuid4()
    mock_flow = MagicMock()
    mock_flow.name = "Cloned Preset Flow"
    mock_flow.trigger_event_source = None
    mock_flow.webhook_config = None
    mock_flow.schedule_config = None
    mock_flow.source_preset_id = None
    mock_flow.is_enabled = True
    mock_crud_flow.get.return_value = mock_flow

    flow_update = schemas.FlowUpdate(
        trigger_event_source="webhook",
        trigger_event_types=["webhook"],
    )
    mock_crud_flow.update.return_value = schemas.FlowResponse(
        id=flow_id,
        name="Cloned Preset Flow",
        trigger_event_source="webhook",
        trigger_event_types=["webhook"],
        prompt_template="p",
        created_at=datetime.now(ZoneInfo("UTC")),
        updated_at=datetime.now(ZoneInfo("UTC")),
    )

    await maybe_await(
        flows.update_flow(
            db=MagicMock(),
            flow_id=flow_id,
            flow_in=flow_update,
            current_user=mock_account,
        )
    )

    assert flow_update.webhook_config is not None
    assert flow_update.webhook_config.webhook_secret
    assert len(flow_update.webhook_config.webhook_secret) >= 32


@pytest.mark.asyncio
async def test_update_flow_webhook_keeps_existing_secret(
    mock_account: Account, mocker: MockerFixture
):
    """Updating a webhook flow that already has a secret does not
    overwrite it."""
    mock_crud_flow = _mock_crud_flow_no_conflicts(mocker)
    flow_id = uuid.uuid4()
    mock_flow = MagicMock()
    mock_flow.name = "Webhook Flow"
    mock_flow.trigger_event_source = "webhook"
    mock_flow.webhook_config = {"webhook_secret": "existing-secret"}
    mock_flow.schedule_config = None
    mock_flow.source_preset_id = None
    mock_flow.is_enabled = True
    mock_crud_flow.get.return_value = mock_flow

    flow_update = schemas.FlowUpdate(name="Renamed Webhook Flow")
    mock_crud_flow.update.return_value = schemas.FlowResponse(
        id=flow_id,
        name="Renamed Webhook Flow",
        trigger_event_source="webhook",
        trigger_event_types=["webhook"],
        prompt_template="p",
        created_at=datetime.now(ZoneInfo("UTC")),
        updated_at=datetime.now(ZoneInfo("UTC")),
    )

    await maybe_await(
        flows.update_flow(
            db=MagicMock(),
            flow_id=flow_id,
            flow_in=flow_update,
            current_user=mock_account,
        )
    )

    assert flow_update.webhook_config is None


@pytest.mark.asyncio
async def test_update_flow_switch_to_schedule_forces_event_types(
    mock_account: Account, mocker: MockerFixture
):
    """Switching source to schedule with a config forces trigger_event_types."""
    mock_crud_flow = _mock_crud_flow_no_conflicts(mocker)
    flow_id = uuid.uuid4()
    mock_flow = MagicMock()
    mock_flow.name = "Webhook Flow"
    mock_flow.trigger_event_source = "webhook"
    mock_flow.schedule_config = None
    mock_flow.source_preset_id = None
    mock_flow.is_enabled = True
    mock_crud_flow.get.return_value = mock_flow

    flow_update = schemas.FlowUpdate(
        trigger_event_source="schedule",
        schedule_config=CronSchedule(expr="*/30 * * * *", timezone="UTC"),
    )
    mock_crud_flow.update.return_value = schemas.FlowResponse(
        id=flow_id,
        name="Webhook Flow",
        trigger_event_source="schedule",
        trigger_event_types=["schedule"],
        schedule_config=CronSchedule(expr="*/30 * * * *", timezone="UTC"),
        prompt_template="p",
        created_at=datetime.now(ZoneInfo("UTC")),
        updated_at=datetime.now(ZoneInfo("UTC")),
    )

    result = await maybe_await(
        flows.update_flow(
            db=MagicMock(),
            flow_id=flow_id,
            flow_in=flow_update,
            current_user=mock_account,
        )
    )

    assert flow_update.trigger_event_types == ["schedule"]
    mock_crud_flow.update.assert_called_once_with(
        db=mocker.ANY,
        db_obj=mock_flow,
        flow_in=flow_update,
        account_id=mock_account.account_id,
    )
    assert result.schedule_state is not None
    assert result.schedule_state["cron"] == "*/30 * * * *"


# ---------------------------------------------------------------------------
# Schedule preview endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_schedule_daily(mock_account: Account):
    """Preview returns a description and the next 3 run times."""
    preview_in = schemas.SchedulePreviewRequest(
        schedule_config=DailySchedule(at="06:30", timezone="Europe/Athens")
    )

    result = await maybe_await(
        flows.preview_flow_schedule(
            db=MagicMock(), preview_in=preview_in, current_user=mock_account
        )
    )

    assert result.type == "daily"
    assert result.description == "Daily at 06:30 (Europe/Athens)"
    assert result.timezone == "Europe/Athens"
    assert len(result.next_run_times) == 3
    # Consecutive daily runs are 24h apart (mod DST)
    gap = result.next_run_times[1] - result.next_run_times[0]
    assert 23 * 3600 <= gap.total_seconds() <= 25 * 3600


@pytest.mark.asyncio
async def test_preview_schedule_interval(mock_account: Account):
    preview_in = schemas.SchedulePreviewRequest(
        schedule_config=IntervalSchedule(every=6, unit="hours")
    )

    result = await maybe_await(
        flows.preview_flow_schedule(
            db=MagicMock(), preview_in=preview_in, current_user=mock_account
        )
    )

    assert result.type == "interval"
    assert result.description == "Every 6 hours"
    assert len(result.next_run_times) == 3
    gap = result.next_run_times[1] - result.next_run_times[0]
    assert gap.total_seconds() == 6 * 3600


@pytest.mark.asyncio
async def test_preview_schedule_weekly_and_cron(mock_account: Account):
    weekly = await maybe_await(
        flows.preview_flow_schedule(
            db=MagicMock(),
            preview_in=schemas.SchedulePreviewRequest(
                schedule_config=WeeklySchedule(days=["fri", "mon"], at="09:00")
            ),
            current_user=mock_account,
        )
    )
    assert weekly.description == "Weekly on Mon, Fri at 09:00 (UTC)"
    assert len(weekly.next_run_times) == 3

    cron = await maybe_await(
        flows.preview_flow_schedule(
            db=MagicMock(),
            preview_in=schemas.SchedulePreviewRequest(
                schedule_config=CronSchedule(expr="0 2 * * *")
            ),
            current_user=mock_account,
        )
    )
    assert cron.type == "cron"
    assert len(cron.next_run_times) == 3


def test_preview_request_rejects_invalid_config():
    """Invalid schedule configs fail schema validation (FastAPI 422 path)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        schemas.SchedulePreviewRequest(
            schedule_config={"type": "interval", "every": 1, "unit": "minutes"}
        )
    with pytest.raises(ValidationError):
        schemas.SchedulePreviewRequest(schedule_config={"type": "daily", "at": "24:99"})
    # Absurd `every` must fail as a pydantic ValidationError (-> 422), not
    # leak the underlying timedelta OverflowError as an HTTP 500.
    with pytest.raises(ValidationError, match="maximum"):
        schemas.SchedulePreviewRequest(
            schedule_config={
                "type": "interval",
                "every": 1_000_000_000_000,
                "unit": "days",
            }
        )


def test_preview_request_accepts_legacy_cron_shape():
    req = schemas.SchedulePreviewRequest(
        schedule_config={"cron": "0 2 * * *", "timezone": "UTC"}
    )
    assert req.schedule_config.type == "cron"
    assert req.schedule_config.expr == "0 2 * * *"
