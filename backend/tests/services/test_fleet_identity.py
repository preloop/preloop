"""Names and current enrollment stay independent of historical device evidence."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import fleet_identity, tool_registry


def test_current_names_and_lifecycle_are_account_scoped(
    db_session: Session, test_user: models.User
) -> None:
    """Renaming and decommissioning are visible without another device report."""
    now = datetime.now(UTC)
    agent = models.ManagedAgent(
        account_id=test_user.account_id,
        display_name="Example agent",
        agent_kind="claude_code",
        session_source_type="test",
        session_source_id="synthetic",
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=now,
    )
    server = models.MCPServer(
        account_id=test_user.account_id,
        name="Documentation",
        url="https://example.com/mcp",
    )
    db_session.add_all([agent, server])
    db_session.flush()
    entry, _ = tool_registry.get_or_create_seen(
        db_session,
        account_id=test_user.account_id,
        source="mcp",
        server_id=server.id,
        server_label=None,
        tool_name="fetch",
        agent_id=agent.id,
    )
    names = tool_registry.display_names(
        db_session, account_id=test_user.account_id, entries=[entry]
    )
    assert names == ({server.id: "Documentation"}, {agent.id: "Example agent"})
    assert tool_registry.display_names(
        db_session, account_id=uuid4(), entries=[entry]
    ) == ({}, {})
    agent.lifecycle_state = "decommissioned"
    agent.display_name = "Renamed agent"
    db_session.flush()
    current = fleet_identity.agent_identities(
        db_session, account_id=test_user.account_id, ids=[agent.id]
    )
    assert current == [
        {
            "id": str(agent.id),
            "name": "Renamed agent",
            "lifecycle_state": "decommissioned",
        }
    ]
    assert (
        fleet_identity.agent_identities(db_session, account_id=uuid4(), ids=[agent.id])
        == []
    )
    assert (
        tool_registry.display_names(
            db_session, account_id=test_user.account_id, entries=[entry]
        )[1][agent.id]
        == "Renamed agent"
    )


def test_reporter_name_requires_owned_observation(
    db_session: Session, test_user: models.User
) -> None:
    """Reporter metadata is named without disclosing any API key material."""
    assert (
        fleet_identity.observer_names(
            db_session, account_id=test_user.account_id, ids=[test_user.id]
        )
        == {}
    )
    now = datetime.now(UTC)
    db_session.add(
        models.DiscoveryObservation(
            account_id=test_user.account_id,
            workstation_fingerprint="a" * 64,
            source_ref=test_user.id,
            observation_id=uuid4(),
            ended_at=now,
            received_at=now,
            content_hash="b" * 64,
            evidence={},
        )
    )
    db_session.flush()
    assert fleet_identity.observer_names(
        db_session, account_id=test_user.account_id, ids=[test_user.id]
    ) == {str(test_user.id): test_user.username}
    assert (
        fleet_identity.observer_names(
            db_session, account_id=uuid4(), ids=[test_user.id]
        )
        == {}
    )
