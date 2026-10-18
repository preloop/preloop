"""PostgreSQL tenant boundaries and constant-query graph source windows."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.models.crud.fleet_map import list_resources


def test_source_window_is_owned_ordered_bounded_and_one_query(
    db_session: Session,
) -> None:
    accounts = [
        crud.crud_account.create(
            db_session, obj_in={"organization_name": f"Example {i}"}
        )
        for i in range(2)
    ]
    now = datetime.now(UTC)
    owned = []
    for index, identifier in enumerate((9, 3, 8, 2)):
        agent = models.ManagedAgent(
            id=UUID(int=identifier),
            account_id=accounts[index % 2].id,
            agent_kind="custom",
            session_source_type="test",
            session_source_id=str(uuid4()),
            display_name="Example agent",
            lifecycle_updated_at=now,
            last_seen_at=now,
        )
        db_session.add(agent)
        if index % 2 == 0:
            owned.append(str(agent.id))
    db_session.flush()
    account_id = str(accounts[0].id)
    queries: list[str] = []

    def record(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        queries.append(statement)

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        rows = list_resources(
            db_session, source="agents", account_id=account_id, limit=1
        )
        assert [str(row.id) for row in rows] == [sorted(owned)[0]]
        assert len(queries) == 1
        queries.clear()
        rows = list_resources(
            db_session, source="agents", account_id=account_id, limit=1000
        )
        assert [str(row.id) for row in rows] == sorted(owned)
        assert len(queries) == 1
    finally:
        event.remove(bind, "before_cursor_execute", record)


def test_binding_and_latest_enrollment_windows_stay_in_tenant(
    db_session: Session,
) -> None:
    now = datetime.now(UTC)
    accounts = [
        crud.crud_account.create(
            db_session, obj_in={"organization_name": f"Example {i}"}
        )
        for i in range(2)
    ]
    agents = []
    for account in accounts:
        agent = models.ManagedAgent(
            account_id=account.id,
            agent_kind="custom",
            session_source_type="test",
            session_source_id=str(uuid4()),
            display_name="Example agent",
            lifecycle_updated_at=now,
            last_seen_at=now,
        )
        model = models.AIModel(
            account_id=account.id,
            name="Example model",
            provider_name="openai",
            model_identifier="example",
        )
        db_session.add_all([agent, model])
        db_session.flush()
        agents.append(agent)
        db_session.add(
            models.ManagedAgentAIModelBinding(
                account_id=account.id,
                managed_agent_id=agent.id,
                ai_model_id=model.id,
                config_key="model",
                gateway_alias="example",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        for days in (2, 1):
            db_session.add(
                models.ManagedAgentEnrollment(
                    account_id=account.id,
                    managed_agent_id=agent.id,
                    enrollment_type="cli_managed_config",
                    status="validated",
                    managed_config={"model": f"example-{days}"},
                    created_at=now - timedelta(days=days),
                )
            )
    db_session.flush()
    ids = [str(agent.id) for agent in agents]
    account_id = str(accounts[0].id)
    bindings = crud.crud_managed_agent_ai_model_binding.list_for_fleet_map(
        db_session, account_id=account_id, agent_ids=ids, limit=100
    )
    assert len(bindings) == 1 and bindings[0].managed_agent_id == agents[0].id
    enrollments = crud.crud_managed_agent_enrollment.list_latest_for_fleet_map(
        db_session, account_id=account_id, agent_ids=ids, limit=100
    )
    assert len(enrollments) == 1 and enrollments[0].managed_agent_id == agents[0].id
    assert enrollments[0].managed_config == {"model": "example-1"}


def test_model_window_includes_only_available_built_ins_when_enabled(
    db_session: Session,
) -> None:
    from preloop.services.built_in_models import BUILT_IN_MODEL_ALIAS

    accounts = [
        crud.crud_account.create(
            db_session, obj_in={"organization_name": f"Example models {i}"}
        )
        for i in range(2)
    ]
    own = models.AIModel(
        account_id=accounts[0].id,
        name="Own",
        provider_name="openai",
        model_identifier="example",
    )
    other = models.AIModel(
        account_id=accounts[1].id,
        name="Other",
        provider_name="openai",
        model_identifier="example",
    )
    globals_ = [
        models.AIModel(
            account_id=None,
            name=f"System {i}",
            provider_name="openai",
            model_identifier="example",
            meta_data=metadata,
        )
        for i, metadata in enumerate(
            [
                {"gateway": {"enabled": True, "model_alias": BUILT_IN_MODEL_ALIAS}},
                {"gateway": {"enabled": False, "model_alias": BUILT_IN_MODEL_ALIAS}},
                {
                    "gateway": {"enabled": True, "model_alias": BUILT_IN_MODEL_ALIAS},
                    "retired": True,
                },
                {"gateway": {"enabled": True, "model_alias": "legacy"}},
            ]
        )
    ]
    db_session.add_all([own, other, *globals_])
    db_session.flush()
    account_id = str(accounts[0].id)
    assert [
        r.id
        for r in list_resources(
            db_session, source="models", account_id=account_id, limit=1000
        )
    ] == [own.id]
    rows = list_resources(
        db_session,
        source="models",
        account_id=account_id,
        limit=1000,
        include_built_in=True,
    )
    # The synthetic local stack can also contain unrelated active system rows.
    ids = {r.id for r in rows}
    assert {own.id, globals_[0].id} <= ids
    assert not ids.intersection({other.id, *(r.id for r in globals_[1:])})
    assert [r.id for r in rows] == sorted(r.id for r in rows)
    bounded = list_resources(
        db_session,
        source="models",
        account_id=account_id,
        limit=1,
        include_built_in=True,
    )
    assert [r.id for r in bounded] == [rows[0].id]
