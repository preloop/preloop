"""Codex requires Responses and rejects chat-only bindings before launch."""

from tests.credential_fixtures import ai_model_with_secret

from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.services.model_api_protocol import codex_model_configuration_error
from preloop.services.model_routing import model_usable_for_agent


@pytest.mark.parametrize("gateway", [None, {}, {"enabled": False}])
def test_chat_only_custom_model_requires_gateway(gateway: Any) -> None:
    model = ai_model_with_secret(
        provider_name="anthropic",
        model_identifier="claude-sonnet",
        api_key="test-key",
        api_endpoint="https://api.example.com/v1",
        meta_data={"gateway": gateway} if gateway is not None else {},
    )
    assert "Enable meta_data.gateway.enabled=true" in codex_model_configuration_error(
        model
    )
    assert not model_usable_for_agent(model, "codex")
    assert model_usable_for_agent(model, "claude-code")


@pytest.mark.parametrize(
    "provider, metadata",
    [
        ("openai", {}),
        ("anthropic", {"gateway": {"enabled": True}}),
        ("custom", {"gateway": {"responses_api": "native"}}),
    ],
)
def test_responses_capable_models_remain_supported(
    provider: str, metadata: dict[str, Any]
) -> None:
    model = ai_model_with_secret(
        provider_name=provider,
        model_identifier="example-model",
        api_key="test-key",
        api_endpoint="https://api.example.com/v1",
        meta_data=metadata,
    )
    assert codex_model_configuration_error(model) is None
    assert model_usable_for_agent(model, "codex")


def test_save_rejects_chat_only_codex_with_remediation() -> None:
    from preloop.api.endpoints.flows import _validate_flow_model_binding

    model = models.AIModel(provider_name="anthropic", account_id=None, meta_data={})
    with patch("preloop.api.endpoints.flows.crud_ai_model.get", return_value=model):
        with pytest.raises(HTTPException) as caught:
            _validate_flow_model_binding(MagicMock(), "codex", "model-id", "account-id")
    assert caught.value.status_code == 422
    assert "Responses" in caught.value.detail
    assert "gateway.enabled=true" in caught.value.detail


@pytest.mark.parametrize("gateway_enabled", [None, False, True])
def test_create_and_update_validate_default_model(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    gateway_enabled: bool | None,
) -> None:
    model = ai_model_with_secret(
        name="Custom model",
        provider_name="anthropic",
        model_identifier="claude-sonnet",
        api_key="test-key",
        api_endpoint="https://api.example.com/v1",
        account_id=test_user.account_id,
        meta_data={}
        if gateway_enabled is None
        else {"gateway": {"enabled": gateway_enabled}},
    )
    db_session.add(model)
    db_session.flush()
    payload = {
        "name": f"Custom Codex {uuid4().hex}",
        "prompt_template": "Review",
        "agent_type": "codex",
        "ai_model_id": str(model.id),
        "agent_config": {},
    }
    response = client.post("/api/v1/flows", json=payload)
    if gateway_enabled:
        assert response.status_code == 200, response.text
    else:
        assert response.status_code == 422, response.text
        assert "gateway.enabled=true" in response.json()["detail"]
        payload.pop("ai_model_id")
        created = client.post("/api/v1/flows", json=payload)
        assert created.status_code == 200, created.text
        response = client.put(
            f"/api/v1/flows/{created.json()['id']}",
            json={"ai_model_id": str(model.id)},
        )
        assert response.status_code == 422, response.text
        assert "gateway.enabled=true" in response.json()["detail"]


def test_direct_responses_protocol_reaches_execution_context() -> None:
    from preloop.services.model_runtime_resolver import resolve_ai_model_runtime

    model = models.AIModel(
        provider_name="custom",
        model_identifier="example-model",
        api_endpoint="https://api.example.com/v1",
        meta_data={"gateway": {"enabled": False, "responses_api": "native"}},
    )
    with patch("preloop.services.model_runtime_resolver.get_secret_service") as secret:
        secret.return_value.resolve_ai_model_credentials.return_value = None
        context = resolve_ai_model_runtime(model).to_execution_context()
    assert not context["model_gateway_enabled"]
    assert context["model_api_protocol"] == "responses"
    assert context["model_endpoint"] == "https://api.example.com/v1"


def test_update_rejects_explicit_null_harness(client: TestClient) -> None:
    created = client.post(
        "/api/v1/flows",
        json={
            "name": f"Codex null harness {uuid4().hex}",
            "prompt_template": "Review",
            "agent_type": "codex",
            "agent_config": {},
        },
    )
    assert created.status_code == 200
    updated = client.put(
        f"/api/v1/flows/{created.json()['id']}", json={"agent_type": None}
    )
    assert updated.status_code == 422
    assert updated.json()["detail"] == "agent_type cannot be null"
    stored = client.get(f"/api/v1/flows/{created.json()['id']}")
    assert stored.status_code == 200
    assert stored.json()["agent_type"] == "codex"
