"""Reachable native session ids reported by an Agent Control plugin."""

from datetime import UTC, datetime

from preloop.models.crud import crud_managed_agent
from preloop.models.crud.managed_agent import CONTROL_REACHABLE_SESSION_IDS_CAP

from tests.endpoints.test_agent_control_persistence import _setup_controllable_agent


def _touch(db_session, agent, reachable):
    return crud_managed_agent.touch_last_seen_for_principal(
        db_session,
        account_id=agent.account_id,
        session_source_type=agent.session_source_type,
        session_source_id=agent.session_source_id,
        observed_at=datetime.now(UTC).replace(tzinfo=None),
        control_heartbeat_at=datetime.now(UTC).replace(tzinfo=None),
        control_reachable_session_ids=reachable,
        commit=True,
    )


def test_reachable_session_ids_persisted_and_capped(client, db_session, test_user):
    agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="crud-reachable"
    )
    assert agent.control_reachable_session_ids is None

    many = [f"ses-{i}" for i in range(CONTROL_REACHABLE_SESSION_IDS_CAP + 20)]
    row = _touch(db_session, agent, ["ses-0", "", 7, " ses-0 ", *many])
    db_session.expire_all()
    stored = crud_managed_agent.get(db_session, id=row.id).control_reachable_session_ids

    # Plugins report oldest first: the newest 50 survive the cap.
    assert CONTROL_REACHABLE_SESSION_IDS_CAP == 50
    assert len(stored) == 50
    assert stored == many[-50:]
    assert len(set(stored)) == 50

    # A frame without the list leaves it alone; an empty list clears it.
    _touch(db_session, agent, None)
    db_session.expire_all()
    assert (
        len(crud_managed_agent.get(db_session, id=row.id).control_reachable_session_ids)
        == 50
    )
    _touch(db_session, agent, [])
    db_session.expire_all()
    assert (
        crud_managed_agent.get(db_session, id=row.id).control_reachable_session_ids
        == []
    )
