import pytest
from sqlalchemy.orm import Session
from uuid import uuid4

from preloop.models.models import Tracker
from preloop.models.schemas.flow import FlowCreate, FlowUpdate
from preloop.models.crud import crud_flow  # Import the CRUD instances
from preloop.models.models import Account

# Removed local db_session override, will use global one from conftest.py


@pytest.fixture(scope="function")
def test_tracker(db_session: Session, create_account) -> Tracker:
    """Creates a tracker for use in tests, ensuring an account exists."""
    from preloop.models.crud import crud_tracker
    from preloop.models.schemas.tracker import TrackerCreate  # Local import

    account = create_account()  # Use the fixture from conftest
    tracker_create = TrackerCreate(
        name=f"Test Tracker {uuid4()}",
        tracker_type="github",  # or any valid TrackerType
        account_id=account.id,
        api_key="testkey",
        url="https://example.com/tracker",  # Ensure URL is provided if needed by model validation
    )
    return crud_tracker.create(db=db_session, obj_in=tracker_create)


def test_create_flow(db_session: Session, create_account) -> None:
    """Test creating a new flow."""
    account: Account = create_account()
    flow_name = f"Test Flow {uuid4()}"
    flow_in_corrected = FlowCreate(
        name=flow_name,
        description="A test flow description",
        trigger_event_source="test_source",
        trigger_event_types=["test_event"],  # Use array field
        prompt_template="Test prompt {{data}}",
        agent_type="openhands",
        agent_config={"agent": "TestAgent"},
        account_id=account.id,
    )
    flow = crud_flow.create(
        db=db_session, flow_in=flow_in_corrected, account_id=account.id
    )
    assert flow is not None
    assert flow.name == flow_name
    assert flow.description == "A test flow description"
    assert flow.account_id == account.id


def test_get_flow(db_session: Session, create_account) -> None:
    """Test retrieving a flow by ID."""
    account: Account = create_account()
    flow_name = f"Test Flow for Get {uuid4()}"
    flow_in = FlowCreate(
        name=flow_name,
        trigger_event_source="test_get_source",
        trigger_event_types=["test_get_event"],  # Use array field
        prompt_template="Test get prompt",
        agent_type="openhands",
        agent_config={"agent": "GetAgent"},
        account_id=account.id,
    )
    created_flow = crud_flow.create(
        db=db_session, flow_in=flow_in, account_id=account.id
    )
    retrieved_flow = crud_flow.get(
        db=db_session, id=created_flow.id, account_id=account.id
    )
    assert retrieved_flow is not None
    assert retrieved_flow.id == created_flow.id
    assert retrieved_flow.name == flow_name


def test_get_flows_by_account(
    db_session: Session,
    create_account,  # Added create_account fixture
) -> None:
    """Test retrieving flows by account ID."""
    account: Account = create_account()
    flow_name1 = f"Org Flow 1 {uuid4()}"
    flow_in1 = FlowCreate(
        name=flow_name1,
        trigger_event_source="org_source1",
        trigger_event_types=["org_event1"],  # Use array field
        prompt_template="Org prompt1",
        agent_type="openhands",
        agent_config={"agent": "OrgAgent1"},
        account_id=account.id,
    )
    crud_flow.create(db=db_session, flow_in=flow_in1, account_id=account.id)

    flow_name2 = f"Org Flow 2 {uuid4()}"
    flow_in2 = FlowCreate(
        name=flow_name2,
        trigger_event_source="org_source2",
        trigger_event_types=["org_event2"],  # Use array field
        prompt_template="Org prompt2",
        agent_type="openhands",
        agent_config={"agent": "OrgAgent2"},
        account_id=account.id,
    )
    crud_flow.create(db=db_session, flow_in=flow_in2, account_id=account.id)

    # Create another account and flow to ensure we only get flows for the target account
    # Need another tracker for another account.
    # We will use the `create_tracker` and `create_account` fixtures from conftest.py
    # to create a new, independent account and tracker.

    other_account = create_account()
    other_flow_in = FlowCreate(
        name="Other Org Flow",
        trigger_event_source="other_org_source",
        trigger_event_types=["other_org_event"],  # Use array field
        prompt_template="Other org prompt",
        agent_type="openhands",
        agent_config={"agent": "OtherOrgAgent"},
        account_id=other_account.id,
    )
    crud_flow.create(db=db_session, flow_in=other_flow_in, account_id=other_account.id)

    flows = crud_flow.get_by_account(
        db=db_session,
        account_id=account.id,
    )
    assert flows is not None
    assert len(flows) == 2
    flow_names = {flow.name for flow in flows}
    assert flow_name1 in flow_names
    assert flow_name2 in flow_names


def test_update_flow(db_session: Session, create_account) -> None:
    """Test updating an existing flow."""
    account: Account = create_account()
    flow_name = f"Initial Flow Name {uuid4()}"
    flow_in = FlowCreate(
        name=flow_name,
        trigger_event_source="update_source_initial",
        trigger_event_types=["update_event_initial"],  # Use array field
        prompt_template="Update prompt initial",
        agent_type="openhands",
        agent_config={"agent": "UpdateAgentInitial"},
        account_id=account.id,
    )
    created_flow = crud_flow.create(
        db=db_session, flow_in=flow_in, account_id=account.id
    )

    updated_flow_name = f"Updated Flow Name {uuid4()}"
    updated_description = "This is an updated description."
    flow_update_data = FlowUpdate(
        name=updated_flow_name,
        description=updated_description,
        account_id=account.id,
    )

    # Fetch the object first, as update_flow expects db_obj
    flow_to_update = crud_flow.get(
        db=db_session, id=created_flow.id, account_id=account.id
    )
    assert flow_to_update is not None  # Ensure it exists before update

    updated_flow = crud_flow.update(
        db=db_session,
        db_obj=flow_to_update,
        flow_in=flow_update_data,
        account_id=account.id,
    )
    assert updated_flow is not None
    assert updated_flow.id == created_flow.id
    assert updated_flow.name == updated_flow_name
    assert updated_flow.description == updated_description
    assert updated_flow.account_id == account.id


def test_remove_flow(db_session: Session, create_account) -> None:
    """Test removing a flow."""
    account: Account = create_account()
    flow_name = f"Flow to Remove {uuid4()}"
    flow_in = FlowCreate(
        name=flow_name,
        trigger_event_source="remove_source",
        trigger_event_types=["remove_event"],  # Use array field
        prompt_template="Remove prompt",
        agent_type="openhands",
        agent_config={"agent": "RemoveAgent"},
        account_id=account.id,
    )
    created_flow = crud_flow.create(
        db=db_session, flow_in=flow_in, account_id=account.id
    )
    flow_id_to_remove = created_flow.id

    removed_flow = crud_flow.remove(
        db=db_session, id=flow_id_to_remove, account_id=account.id
    )
    assert removed_flow is not None
    assert removed_flow.id == flow_id_to_remove

    retrieved_after_remove = crud_flow.get(
        db=db_session,
        id=flow_id_to_remove,
        account_id=account.id,
    )
    assert retrieved_after_remove is None


def test_get_flow_not_found(db_session: Session) -> None:
    """Test retrieving a non-existent flow."""
    non_existent_flow_id = uuid4()
    retrieved_flow = crud_flow.get(
        db=db_session, id=str(non_existent_flow_id), account_id=str(uuid4())
    )  # Changed to str
    assert retrieved_flow is None


# update_flow now requires an existing db_obj; not-found handling is covered by
# test_get_flow_not_found instead of a dedicated update-not-found test.


def test_remove_flow_not_found(db_session: Session) -> None:
    """Test removing a non-existent flow."""
    non_existent_flow_id = uuid4()
    removed_flow = crud_flow.remove(
        db=db_session, id=str(non_existent_flow_id), account_id=str(uuid4())
    )  # Changed to str
    assert removed_flow is None


def test_get_by_trigger_with_account_id(db_session: Session, create_account) -> None:
    """Test retrieving flows by trigger event with account filter."""
    account: Account = create_account()

    # Create a flow with specific trigger using the new array fields
    flow_in = FlowCreate(
        name=f"Trigger Test Flow {uuid4()}",
        trigger_event_source="github",
        trigger_event_types=["pull_request"],  # Use array field
        prompt_template="Test prompt",
        agent_type="openhands",
        agent_config={"agent": "TestAgent"},
        account_id=account.id,
    )
    created_flow = crud_flow.create(
        db=db_session, flow_in=flow_in, account_id=account.id
    )

    # Get flows by trigger with account_id
    flows = crud_flow.get_by_trigger(
        db=db_session,
        event_source="github",
        event_type="pull_request",
        account_id=account.id,
    )

    # Should find our flow
    assert len(flows) >= 1
    flow_ids = [f.id for f in flows]
    assert created_flow.id in flow_ids


def test_get_by_trigger_matches_legacy_dotted_issue_opened(
    db_session: Session, create_account
) -> None:
    """Clones that still store issue.opened must match issue_opened events."""
    account: Account = create_account()
    flow_in = FlowCreate(
        name=f"Legacy Triage {uuid4()}",
        trigger_event_source="github",
        trigger_event_types=["issue.opened"],
        prompt_template="Triage",
        agent_type="codex",
        agent_config={},
        account_id=account.id,
    )
    created_flow = crud_flow.create(
        db=db_session, flow_in=flow_in, account_id=account.id
    )

    flows = crud_flow.get_by_trigger(
        db=db_session,
        event_source="github",
        event_type="issue_opened",
        account_id=account.id,
    )
    flow_ids = [item.id for item in flows]
    assert created_flow.id in flow_ids


def test_get_by_trigger_matches_canonical_issue_opened(
    db_session: Session, create_account
) -> None:
    account: Account = create_account()
    flow_in = FlowCreate(
        name=f"Canonical Triage {uuid4()}",
        trigger_event_source="github",
        trigger_event_types=["issue_opened", "issue_updated"],
        prompt_template="Triage",
        agent_type="codex",
        agent_config={},
        account_id=account.id,
    )
    created_flow = crud_flow.create(
        db=db_session, flow_in=flow_in, account_id=account.id
    )

    opened = crud_flow.get_by_trigger(
        db=db_session,
        event_source="github",
        event_type="issue_opened",
        account_id=account.id,
    )
    updated = crud_flow.get_by_trigger(
        db=db_session,
        event_source="github",
        event_type="issue_updated",
        account_id=account.id,
    )
    assert created_flow.id in [item.id for item in opened]
    assert created_flow.id in [item.id for item in updated]
