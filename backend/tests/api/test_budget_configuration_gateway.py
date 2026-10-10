"""A policy created through the OSS API must stop an actual gateway request."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.app import create_app
from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import budget
from preloop.api.endpoints.openai_gateway import get_model_gateway_auth_context
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_ai_model,
    crud_api_key,
    crud_managed_agent,
)
from preloop.models.db.session import get_db_session
from preloop.plugins.base import PluginManager
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_runtime_resolver import resolve_ai_model_runtime


@pytest.mark.parametrize(
    "scope", ["account", "api_key", "managed_agent", "ai_model", "user"]
)
@pytest.mark.parametrize("explicit_alias", [True, False])
def test_api_created_budget_blocks_priced_gateway_dispatch(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    explicit_alias: bool,
) -> None:
    monkeypatch.setenv("PRELOOP_SERVICE_ROLE", "gateway")
    if scope == "user":
        monkeypatch.setattr(
            "preloop.services.configuration_gating._authorizer",
            lambda db, account_id, capability: None,
        )
    monkeypatch.setattr("preloop.plugins.get_plugin_manager", PluginManager)
    account = crud_account.get(db_session, id=test_user.account_id)
    crud_account.update(
        db_session, db_obj=account, obj_in={"primary_user_id": test_user.id}
    )
    agent = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=test_user.account_id,
        display_name="Synthetic agent",
        owner_user_id=test_user.id,
    )
    agent_id = agent.id
    key, token = crud_api_key.create_runtime_key(
        db_session,
        name="Synthetic gateway key",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={"managed_agent_id": str(agent_id)},
    )
    key_id = key.id
    model = crud_ai_model.create_with_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={
            "name": "Synthetic priced model",
            "provider_name": "openai",
            "model_identifier": "synthetic-priced-model",
            "api_key": "synthetic-unused-provider-key",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    **({"model_alias": "synthetic-priced"} if explicit_alias else {}),
                },
                "pricing": {"input_price_per_1k": 1, "output_price_per_1k": 1},
            },
        },
    )
    model_id = model.id
    alias = resolve_ai_model_runtime(model).model_gateway_model_alias
    subject_ids = {
        "account": None,
        "api_key": key_id,
        "managed_agent": agent_id,
        "ai_model": model_id,
        "user": test_user.id,
    }
    app = create_app()
    app.include_router(budget.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_user
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(
            token=token,
            user=test_user,
            api_key=key,
        )
    )
    with (
        patch("preloop.services.openai_gateway.litellm.completion") as provider,
        TestClient(app) as client,
    ):
        policy = client.post(
            "/api/v1/budget/policies",
            json={
                "subject_type": scope,
                "subject_id": str(subject_ids[scope]) if subject_ids[scope] else None,
                "model_alias": "",
                "period": "monthly",
                "hard_limit_usd": 0,
            },
        )
        assert policy.status_code == 200, policy.text
        if scope == "ai_model":
            assert policy.json()["subject_type"] == "account"
            assert policy.json()["model_alias"] == alias
        response = client.post(
            "/openai/v1/chat/completions",
            json={
                "model": alias,
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 10,
            },
        )
    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "insufficient_quota"
    assert response.json()["error"]["preloop_code"] == "budget_limit_exceeded"
    assert response.headers["x-should-retry"] == "false"
    assert int(response.headers["retry-after"]) > 0
    provider.assert_not_called()
