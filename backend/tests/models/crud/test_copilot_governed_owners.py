"""Only active agents in the same account close a vendor inventory gap."""

from datetime import UTC, datetime
from uuid import uuid4

from preloop.models import models
from preloop.models.crud import crud_managed_agent
from sqlalchemy.orm import Session


def test_governed_owner_lookup_is_account_and_lifecycle_scoped(
    db_session: Session, test_user: models.User
) -> None:
    other = models.Account(
        id=uuid4(), organization_name="Other fixture", is_active=True
    )
    db_session.add(other)
    db_session.flush()
    owner = test_user.id
    for account_id, lifecycle in [
        (other.id, "active"),
        (test_user.account_id, "suspended"),
        (test_user.account_id, "decommissioned"),
    ]:
        db_session.add(
            models.ManagedAgent(
                id=uuid4(),
                account_id=account_id,
                owner_user_id=owner,
                session_source_type="claude_code",
                session_source_id=str(uuid4()),
                agent_kind="claude_code",
                display_name="Fixture agent",
                enrolled_via="runtime_session_token",
                lifecycle_state=lifecycle,
                lifecycle_updated_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC),
            )
        )
    db_session.flush()
    assert (
        crud_managed_agent.governed_owner_ids(
            db_session, account_id=test_user.account_id, user_ids=[owner]
        )
        == set()
    )
    assert crud_managed_agent.governed_owner_ids(
        db_session, account_id=other.id, user_ids=[owner]
    ) == {owner}
    assert (
        crud_managed_agent.governed_owner_ids(
            db_session, account_id=other.id, user_ids=[]
        )
        == set()
    )
