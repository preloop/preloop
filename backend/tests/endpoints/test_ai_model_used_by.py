"""Model-consumer reads use the model gate before consulting configuration."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.endpoints.ai_models import get_ai_model_used_by
from preloop.models import crud, models
from preloop.schemas.ai_model import AIModelUsedByResponse
from tests.conftest import maybe_await


@pytest.mark.asyncio
async def test_model_visibility_is_checked_before_consumers_are_read() -> None:
    with (
        patch(
            "preloop.api.endpoints.ai_models._get_readable_ai_model",
            side_effect=HTTPException(404),
        ),
        patch(
            "preloop.services.ai_model_usage_links.configured_model_consumers"
        ) as consumers,
    ):
        with pytest.raises(HTTPException) as error:
            await maybe_await(
                get_ai_model_used_by(
                    model_id=uuid4(), db=MagicMock(), current_user=models.User()
                )
            )
        assert error.value.status_code == 404
        consumers.assert_not_called()


@pytest.mark.asyncio
async def test_consumers_are_scoped_to_the_authenticated_membership() -> None:
    db = MagicMock()
    model_id = uuid4()
    user = models.User(id=uuid4(), account_id=uuid4())
    with (
        patch("preloop.api.endpoints.ai_models._get_readable_ai_model") as readable,
        patch(
            "preloop.services.ai_model_usage_links.configured_model_consumers",
            return_value=AIModelUsedByResponse(),
        ) as consumers,
    ):
        response = await maybe_await(
            get_ai_model_used_by(model_id=model_id, db=db, current_user=user)
        )
        assert response.agents == response.flows == []
        readable.assert_called_once_with(db=db, model_id=model_id, current_user=user)
        consumers.assert_called_once_with(
            db, user=user, model_id=str(model_id), credentials_secret_id=None
        )


def test_real_model_consumer_endpoint_reads_configuration_without_traffic(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """A configured agent appears before its first request; traffic slots do not."""
    crud.crud_user.update(db_session, db_obj=test_user, obj_in={"is_superuser": True})
    model = crud.crud_ai_model.create(
        db_session,
        obj_in=dict(
            account_id=test_user.account_id,
            name="Example model",
            provider_name="openai",
            model_identifier="example-model",
        ),
    )
    configured_id = None
    for name, key in (
        ("Configured agent", "model.primary"),
        ("Observed agent", "gateway.autoregister.example-model"),
    ):
        agent = crud.crud_managed_agent.create_custom_agent(
            db_session,
            account_id=test_user.account_id,
            owner_user_id=test_user.id,
            display_name=name,
            agent_kind="codex",
        )
        crud.crud_managed_agent_ai_model_binding.replace_for_agent(
            db_session,
            account_id=str(test_user.account_id),
            agent_id=str(agent.id),
            bindings=[
                {
                    "config_key": key,
                    "gateway_alias": "example-model",
                    "ai_model_id": str(model.id),
                }
            ],
        )
        if name == "Configured agent":
            configured_id = str(agent.id)
    response = client.get(f"/api/v1/ai-models/{model.id}/used-by")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "agents": [
            {"id": configured_id, "name": "Configured agent", "agent_kind": "codex"}
        ],
        "flows": [],
        "ci_identities": [],
        "shared_credential_models": [],
        "truncated": False,
    }
