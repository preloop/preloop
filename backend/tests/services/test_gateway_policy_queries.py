"""Bound policy lookup cost without bypassing commercial extension checks."""

from typing import Any
import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.budget import crud_budget_policy
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_budget_enforcer import ModelGatewayBudgetEnforcer


@pytest.mark.parametrize("scope", [None, "account", "global"])
def test_one_policy_query_without_optional_subject_resolution(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    scope: str | None,
) -> None:
    if scope is not None:
        crud_budget_policy.create(
            db_session,
            obj_in={
                "account_id": test_user.account_id,
                "subject_type": scope,
                "period": models.BudgetPeriod.monthly,
                "hard_limit_usd": 100,
                "soft_limit_usd": 0.00001,
                "notify_on_soft": True,
            },
        )
    model = models.AIModel(
        id=uuid.uuid4(),
        provider_name="openai",
        model_identifier="synthetic-priced",
        meta_data={
            "gateway": {"enabled": True},
            "pricing": {"input_price_per_1k": 1, "output_price_per_1k": 1},
        },
    )
    auth = ModelGatewayAuthContext(
        token="synthetic",
        user=test_user,
        api_key=models.ApiKey(id=uuid.uuid4(), account_id=test_user.account_id),
    )
    calls: list[str] = []

    class ExtendedEnforcer(ModelGatewayBudgetEnforcer):
        def _enforce_additional_constraints(self, *args: Any, **kwargs: Any) -> None:
            assert kwargs["estimated_cost"] > 0
            calls.append("additional")

        def _notify_budget_limit(self, **kwargs: Any) -> None:
            assert kwargs["limit_type"] == "soft"
            calls.append("notification")

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("No configured policy needs agent or owner resolution")

    for helper in ("_resolve_managed_agent_id", "_resolve_owner_user_id"):
        monkeypatch.setattr(
            "preloop.services.model_gateway_budget_enforcer." + helper, forbidden
        )
    statements: list[str] = []

    def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "budget_policies" in statement and statement.lstrip().upper().startswith(
            "SELECT"
        ):
            statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        ExtendedEnforcer().enforce_or_raise(
            db_session,
            auth,
            model,
            {"model": "openai/synthetic-priced", "max_tokens": 10},
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 1
    assert calls == ["additional"] + (["notification"] if scope else [])
