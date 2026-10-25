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


def test_identity_recency_preserves_session_attribution_without_totals(
    client, db_session, test_user
) -> None:
    """Per-run traffic counts; another known agent's traffic stays its own."""
    from datetime import timedelta
    from unittest.mock import patch
    from uuid import uuid4

    from preloop.models import models

    agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="identity-agent"
    )
    other, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="other-agent"
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    owned_at = now - timedelta(seconds=10)
    for principal, timestamp in [
        ("identity-agent:run", owned_at),
        (other.session_source_id, now),
    ]:
        db_session.add(
            models.ApiUsage(
                account_id=test_user.account_id,
                runtime_session_id=agent.runtime_session_id,
                endpoint="/v1/responses",
                method="POST",
                status_code=200,
                duration=0.1,
                action_type="model_gateway",
                runtime_principal_type="openclaw",
                runtime_principal_id=principal,
                timestamp=timestamp,
                model_alias="synthetic/model",
            )
        )
    db_session.flush()
    with patch(
        "preloop.models.crud.managed_agent._usage_aggregate_for_principal",
        side_effect=AssertionError("identity must not aggregate usage"),
    ):
        identity = crud_managed_agent.get_summary_for_account(
            db_session,
            account_id=str(test_user.account_id),
            agent_id=str(agent.id),
            include_usage=False,
        )
        assert identity is not None
        assert identity["last_request_at"] == owned_at
        assert identity["activity_status"] == "active_now"
        assert identity["usage_computed"] is False
        assert identity["total_requests"] == 0
        assert (
            crud_managed_agent.get_summary_for_account(
                db_session,
                account_id=str(uuid4()),
                agent_id=str(agent.id),
                include_usage=False,
            )
            is None
        )
