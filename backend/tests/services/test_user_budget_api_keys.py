"""Per-user budgets count calls made with API keys the user owns (#1174)."""

import uuid
from typing import Any

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_api_key,
    crud_api_usage,
    crud_managed_agent,
    crud_user,
)
from preloop.models.crud.budget import crud_budget_policy, crud_budget_spend
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_budget_enforcer import (
    ModelGatewayBudgetEnforcer,
    budget_user_ids,
)
from preloop.services.model_gateway_errors import ModelGatewayAPIError

PAYLOAD = {"model": "synthetic-priced", "messages": [{"role": "user", "content": "x"}]}


def _model() -> models.AIModel:
    return models.AIModel(
        id=uuid.uuid4(),
        provider_name="openai",
        model_identifier="synthetic-priced",
        meta_data={
            "gateway": {"enabled": True},
            "pricing": {"input_price_per_1k": 1, "output_price_per_1k": 1},
        },
    )


def _other_user(db: Session, account_id: Any) -> models.User:
    unique = uuid.uuid4().hex[:8]
    return crud_user.create(
        db,
        obj_in={
            "account_id": account_id,
            "username": f"jane{unique}",
            "email": f"jane{unique}@example.com",
            "hashed_password": "x",
            "is_active": True,
        },
    )


def _key(db: Session, user: models.User, **context: Any) -> models.ApiKey:
    key, _token = crud_api_key.create_runtime_key(
        db,
        name="Synthetic key " + uuid.uuid4().hex[:6],
        account_id=user.account_id,
        user_id=user.id,
        context_data=context or {},
    )
    return key


def _usage(db: Session, key: models.ApiKey, **extra: Any) -> None:
    crud_api_usage.log_gateway_request(
        db,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(key.user_id),
        account_id=str(key.account_id),
        api_key_id=str(key.id),
        auth_subject_type="api_key",
        model_alias="synthetic-priced",
        estimated_cost=2.0,
        **extra,
    )


def _user_spend(db: Session, account_id: Any, user_id: Any) -> float:
    return crud_budget_spend.get_spend(
        db,
        account_id=account_id,
        subject_type="user",
        subject_id=user_id,
        model_alias=None,
        period=models.BudgetPeriod.all_time,
        period_start=None,
    )


def _user_policy(db: Session, user: models.User, hard: float) -> None:
    crud_budget_policy.create(
        db,
        obj_in={
            "account_id": user.account_id,
            "subject_type": "user",
            "subject_id": user.id,
            "period": models.BudgetPeriod.monthly,
            "hard_limit_usd": hard,
        },
    )


def _blocked(db: Session, user: models.User, key: models.ApiKey) -> bool:
    auth = ModelGatewayAuthContext(token="t", user=user, api_key=key)
    try:
        ModelGatewayBudgetEnforcer().enforce_or_raise(db, auth, _model(), PAYLOAD)
    except ModelGatewayAPIError as exc:
        assert exc.status_code == 429
        return True
    return False


def test_api_key_spend_appears_in_the_owners_user_budget(
    db_session: Session, test_user: models.User
) -> None:
    key = _key(db_session, test_user)
    _usage(db_session, key)
    assert _user_spend(db_session, test_user.account_id, test_user.id) == 2.0


def test_agent_traffic_counts_once_against_the_agent_owner(
    db_session: Session, test_user: models.User
) -> None:
    owner = _other_user(db_session, test_user.account_id)
    agent = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=test_user.account_id,
        display_name="Synthetic agent",
        owner_user_id=owner.id,
    )
    # The key was minted by test_user, but the traffic belongs to the agent.
    key = _key(db_session, test_user, managed_agent_id=str(agent.id))
    _usage(db_session, key, managed_agent_id=str(agent.id))
    assert _user_spend(db_session, test_user.account_id, owner.id) == 2.0
    assert _user_spend(db_session, test_user.account_id, test_user.id) == 0.0


def test_hard_user_limit_blocks_the_next_call_with_an_owned_key(
    db_session: Session, test_user: models.User
) -> None:
    key = _key(db_session, test_user)
    _user_policy(db_session, test_user, hard=2.5)
    assert not _blocked(db_session, test_user, key)
    _usage(db_session, key)
    assert _blocked(db_session, test_user, key)


def test_another_users_limit_does_not_block_my_key(
    db_session: Session, test_user: models.User
) -> None:
    other = _other_user(db_session, test_user.account_id)
    _user_policy(db_session, other, hard=0)
    key = _key(db_session, test_user)
    assert not _blocked(db_session, test_user, key)


def test_key_creator_limit_does_not_block_agent_traffic(
    db_session: Session, test_user: models.User
) -> None:
    owner = _other_user(db_session, test_user.account_id)
    agent = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=test_user.account_id,
        display_name="Synthetic agent",
        owner_user_id=owner.id,
    )
    key = _key(db_session, test_user, managed_agent_id=str(agent.id))
    _user_policy(db_session, test_user, hard=0)
    assert not _blocked(db_session, test_user, key)
    _user_policy(db_session, owner, hard=0)
    assert _blocked(db_session, test_user, key)


@pytest.mark.parametrize("with_agent", [False, True])
def test_budget_user_ids_names_the_users_spend_is_recorded_against(
    db_session: Session, test_user: models.User, with_agent: bool
) -> None:
    owner = _other_user(db_session, test_user.account_id)
    context: dict[str, Any] = {}
    if with_agent:
        agent = crud_managed_agent.create_custom_agent(
            db_session,
            account_id=test_user.account_id,
            display_name="Synthetic agent",
            owner_user_id=owner.id,
        )
        context["managed_agent_id"] = str(agent.id)
    key = _key(db_session, test_user, **context)
    auth = ModelGatewayAuthContext(token="t", user=test_user, api_key=key)
    expected = {owner.id} if with_agent else {test_user.id}
    assert budget_user_ids(db_session, auth) == expected


def test_user_token_without_key_has_no_budget_user(
    db_session: Session, test_user: models.User
) -> None:
    auth = ModelGatewayAuthContext(token="t", user=test_user)
    assert budget_user_ids(db_session, auth) == set()


def test_a_known_key_owner_spares_the_api_key_lookup(
    db_session: Session, test_user: models.User
) -> None:
    """The gateway passes the owner it authenticated, so recording the user
    scope adds no statement to the request."""
    from sqlalchemy import event

    key = _key(db_session, test_user)
    statements: list[str] = []

    def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "FROM api_key" in statement or "FROM api_keys" in statement:
            statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        _usage(db_session, key, api_key_user_id=key.user_id)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert statements == []
    assert _user_spend(db_session, test_user.account_id, test_user.id) == 2.0
