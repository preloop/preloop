"""Account isolation and durable metadata-only MCP evidence storage."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account, crud_mcp_inventory, crud_mcp_server


def test_owned_revision_required_and_evidence_is_deduplicated(
    db_session: Session, test_user: models.User
) -> None:
    server = crud_mcp_server.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "name": "synthetic",
            "url": "http://example.com/mcp",
        },
    )
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Synthetic other"}
    )
    revision = server.updated_at.isoformat()
    now = datetime.now(UTC)
    kwargs = dict(
        server_id=server.id, revision=revision, evidence_class="observed_call", at=now
    )
    with pytest.raises(ValueError, match="not owned"):
        crud_mcp_inventory.observe(db_session, account_id=other.id, **kwargs)
    with pytest.raises(ValueError, match="not owned"):
        crud_mcp_inventory.declare(
            db_session,
            account_id=other.id,
            server_id=server.id,
            revision=revision,
            state="approved",
        )
    with pytest.raises(ValueError, match="not owned"):
        crud_mcp_inventory.observe(
            db_session,
            account_id=test_user.account_id,
            **{**kwargs, "revision": "stale"},
        )
    first = crud_mcp_inventory.observe(
        db_session, account_id=test_user.account_id, **kwargs
    )
    again = crud_mcp_inventory.observe(
        db_session,
        account_id=test_user.account_id,
        **{**kwargs, "at": now + timedelta(seconds=1)},
    )
    assert first.id == again.id and first.first_seen_at == now
    assert first.last_seen_at == now + timedelta(seconds=1)
    configured = crud_mcp_inventory.observe(
        db_session,
        account_id=test_user.account_id,
        **{**kwargs, "evidence_class": "configured_route"},
    )
    assert configured.id != first.id
    assert (
        crud_mcp_inventory.get(db_session, account_id=other.id, finding_id=first.id)
        is None
    )
    crud_mcp_inventory.declare(
        db_session,
        account_id=test_user.account_id,
        server_id=server.id,
        revision=revision,
        state="unapproved",
    )
    review = crud_mcp_inventory.review(
        db_session,
        account_id=test_user.account_id,
        server_id=server.id,
        revision=revision,
    )
    assert review.approval_state == "unapproved"
    assert server.status == "active"
    assert "url" not in models.MCPInventoryFinding.__table__.columns
    assert "arguments" not in models.MCPInventoryFinding.__table__.columns
    configured.ignored_until = now + timedelta(days=1)
    db_session.flush()
    crud_mcp_inventory.reconcile_configured(
        db_session, account_id=test_user.account_id, at=now + timedelta(seconds=2)
    )
    rows = crud_mcp_inventory.list_with_context(
        db_session, account_id=test_user.account_id
    )
    assert len(rows) == 2
    assert all(state == "unapproved" for _, state, _ in rows)
    assert all(updated_at == server.updated_at for _, _, updated_at in rows)
    configured = next(
        row for row, _, _ in rows if row.evidence_class == "configured_route"
    )
    assert configured.ignored_until == now + timedelta(days=1)
    assert configured.first_seen_at == now
    assert configured.last_seen_at == now + timedelta(seconds=2)
    assert crud_mcp_inventory.list_with_context(db_session, account_id=other.id) == []
    assert (
        len(
            crud_mcp_inventory.list_with_context(
                db_session, account_id=test_user.account_id, limit=1
            )
        )
        == 1
    )


@pytest.mark.parametrize("server_count", [1, 2])
def test_reconciliation_reports_registry_beyond_its_bound(
    db_session: Session,
    test_user: models.User,
    caplog: pytest.LogCaptureFixture,
    server_count: int,
) -> None:
    for index in range(server_count):
        crud_mcp_server.create(
            db_session,
            obj_in={
                "account_id": test_user.account_id,
                "name": f"synthetic-{index}",
                "url": "http://example.com/mcp",
            },
        )
    crud_mcp_inventory.reconcile_configured(
        db_session, account_id=test_user.account_id, at=datetime.now(UTC), limit=1
    )
    assert (
        len(
            crud_mcp_inventory.list_with_context(
                db_session, account_id=test_user.account_id
            )
        )
        == 1
    )
    assert ("MCP registry reconciliation is incomplete" in caplog.text) == (
        server_count > 1
    )
    assert "example.com" not in caplog.text


@pytest.mark.parametrize("evidence_class", ["configured_route", "observed_call"])
def test_digest_context_names_latest_actor_or_observed_member(
    db_session: Session, test_user: models.User, evidence_class: str
) -> None:
    """A later config actor cannot replace an observed call's verified member."""
    from uuid import uuid4
    from preloop.models.crud import crud_audit_log, crud_managed_agent, crud_user

    now = datetime.now(UTC)
    email = f"observed-{uuid4()}@example.com"
    person = models.Person(email=email, email_normalized=email)
    db_session.add(person)
    db_session.flush()
    test_user.person_id = person.id
    db_session.flush()
    actor = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "username": f"actor-{uuid4()}",
            "email": f"actor-{uuid4()}@example.com",
            "full_name": "Config actor",
            "hashed_password": "synthetic",
        },
    )
    agent = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=test_user.account_id,
        display_name="Observed agent",
        owner_user_id=test_user.id,
        commit=False,
    )
    server = crud_mcp_server.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "name": "Named route",
            "url": "https://example.com/secret-route",
        },
    )
    row = crud_mcp_inventory.observe(
        db_session,
        account_id=test_user.account_id,
        server_id=server.id,
        revision=server.updated_at.isoformat(),
        evidence_class=evidence_class,
        at=now,
        agent_id=agent.id,
        person_id=person.id,
    )
    for offset, action, actor_id in [
        (-2, "created", test_user.id),
        (-1, "updated", actor.id),
    ]:
        event = crud_audit_log.log_action(
            db_session,
            account_id=test_user.account_id,
            user_id=actor_id,
            action="configuration_change",
            resource_type="configuration",
            resource_id="mcp_server",
            status="success",
            details={
                "action": action,
                "new_value": {"id": str(server.id), "name": server.name},
            },
            commit=False,
        )
        event.timestamp = now + timedelta(minutes=offset)
    db_session.flush()
    context = crud_mcp_inventory.digest_context(
        db_session,
        account_id=test_user.account_id,
        start=now - timedelta(days=7),
        end=now + timedelta(minutes=1),
    )
    assert len(context) == 1
    item = context[0]
    assert item["MCPInventoryFinding"].id == row.id
    assert item["server_name"] == "Named route"
    assert item["agent_name"] == "Observed agent"
    assert item["change_action"] == "updated"
    expected = actor if evidence_class == "configured_route" else test_user
    assert item["user_id"] == expected.id
    assert item["user_name"] == expected.full_name
    assert "secret-route" not in str(item)
    other = crud_account.create(db_session, obj_in={"organization_name": "Other"})
    assert (
        crud_mcp_inventory.digest_context(
            db_session,
            account_id=other.id,
            start=now - timedelta(days=7),
            end=now + timedelta(minutes=1),
        )
        == []
    )
    # Malformed foreign references must not leak another account's identity.
    row.account_id = other.id
    db_session.flush()
    foreign = crud_mcp_inventory.digest_context(
        db_session,
        account_id=other.id,
        start=now - timedelta(days=7),
        end=now + timedelta(minutes=1),
    )[0]
    assert foreign["server_name"] is None
    assert foreign["agent_name"] is None
    assert foreign["user_name"] is None


def test_digest_context_dates_deleted_routes_and_omits_undated_absence(
    db_session: Session, test_user: models.User
) -> None:
    """A removal this week includes older evidence using only safe audit fields."""
    from preloop.models.crud import crud_audit_log

    now = datetime.now(UTC)
    server = crud_mcp_server.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "name": "Removed route",
            "url": "https://example.com/secret-route",
        },
    )
    row = crud_mcp_inventory.observe(
        db_session,
        account_id=test_user.account_id,
        server_id=server.id,
        revision=server.updated_at.isoformat(),
        evidence_class="configured_route",
        at=now - timedelta(days=20),
    )
    identifier = server.id
    db_session.delete(server)
    db_session.flush()
    bounds = dict(
        account_id=test_user.account_id,
        start=now - timedelta(days=7),
        end=now + timedelta(minutes=1),
    )
    assert crud_mcp_inventory.digest_context(db_session, **bounds) == []
    event = crud_audit_log.log_action(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        action="configuration_change",
        resource_type="configuration",
        resource_id="mcp_server",
        status="success",
        details={
            "action": "deleted",
            "old_value": {
                "id": str(identifier),
                "name": "Removed route",
                "url": "secret-url",
            },
        },
        commit=False,
    )
    event.timestamp = now
    db_session.flush()
    (item,) = crud_mcp_inventory.digest_context(db_session, **bounds)
    assert item["MCPInventoryFinding"].id == row.id
    assert item["server_name"] == "Removed route"
    assert item["removed_at"].replace(tzinfo=UTC) == now
    assert item["user_id"] == test_user.id
    assert item["updated_at"] is None
    assert "secret-url" not in str(item)
