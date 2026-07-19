"""Per-principal model authorization on the model gateway.

Subscription-OAuth (Claude Code / Codex) models are bound to one managed
agent's credentials. They must only be surfaced to — and resolvable by — the
gateway principal whose ``managed_agent_id`` has an active row in
``managed_agent_ai_model_binding`` for that model. BYOK/API-key models stay
visible to every principal, and console user tokens keep full visibility.

The rules are pinned at the single authorized-set chokepoint
(``compute_authorized_model_ids``) via the service surfaces that consume it:
``list_models`` and ``_resolve_requested_model``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from preloop.models.crud import (
    crud_ai_model,
    crud_api_key,
    crud_managed_agent_ai_model_binding,
)
from preloop.models.models import ManagedAgent
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.secret_service import (
    ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
)

BYOK_ALIAS = "openai/gpt-5"
BOUND_ALIAS_A = "anthropic/claude-sonnet-4-5-agent-a"
BOUND_ALIAS_B = "anthropic/claude-sonnet-4-5-agent-b"


def _make_agent(db_session, test_user, *, source_id: str) -> ManagedAgent:
    now = datetime.now(UTC).replace(tzinfo=None)
    agent = ManagedAgent(
        id=uuid4(),
        account_id=test_user.account_id,
        runtime_session_id=None,
        agent_kind="hermes",
        session_source_type="hermes",
        session_source_id=source_id,
        display_name=f"Hermes {source_id}",
        enrolled_via="runtime_session_token",
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=now,
    )
    db_session.add(agent)
    db_session.commit()
    return agent


def _make_byok_gateway_model(db_session, test_user, *, alias: str = BYOK_ALIAS):
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"BYOK {uuid4()}",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "sk-byok-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": alias,
                    "provider_adapter": "preloop",
                }
            },
        },
        account_id=test_user.account_id,
    )


def _make_bound_gateway_model(
    db_session, test_user, *, alias: str, is_default: bool = False
):
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"Subscription {uuid4()}",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "credential_type": ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
            "credential_payload": {"access": "oauth-access-token"},
            "is_default": is_default,
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": alias,
                    "provider_adapter": "preloop",
                }
            },
        },
        account_id=test_user.account_id,
    )


def _bind_model_to_agent(db_session, test_user, agent, ai_model, *, alias: str):
    crud_managed_agent_ai_model_binding.replace_for_agent(
        db_session,
        account_id=str(test_user.account_id),
        agent_id=str(agent.id),
        bindings=[
            {
                "config_key": "primary",
                "gateway_alias": alias,
                "ai_model_id": str(ai_model.id),
            }
        ],
    )


def _agent_context(db_session, test_user, agent) -> ModelGatewayAuthContext:
    api_key, token = crud_api_key.create_runtime_key(
        db_session,
        name=f"Managed Agent Credential {uuid4()}",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={
            "managed_agent_id": str(agent.id),
            "runtime_principal": {
                "type": agent.session_source_type,
                "id": agent.session_source_id,
                "name": agent.display_name,
            },
        },
    )
    return ModelGatewayAuthContext(token=token, user=test_user, api_key=api_key)


def _legacy_context(db_session, test_user) -> ModelGatewayAuthContext:
    """Gateway credential minted before managed_agent_id existed."""
    api_key, token = crud_api_key.create_runtime_key(
        db_session,
        name=f"Legacy Gateway Credential {uuid4()}",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={},
    )
    return ModelGatewayAuthContext(token=token, user=test_user, api_key=api_key)


def _user_context(test_user) -> ModelGatewayAuthContext:
    """Console-originated user token (no API key, no OAuth MCP token)."""
    return ModelGatewayAuthContext(token="user-jwt", user=test_user)


def _listed_ids(service: OpenAIGatewayService) -> list[str]:
    return [item["id"] for item in service.list_models()["data"]]


def test_bound_model_invisible_to_other_agent_listing(db_session, test_user):
    """Another agent's credential must not see a principal-bound model."""
    agent_a = _make_agent(db_session, test_user, source_id="hermes-a")
    agent_b = _make_agent(db_session, test_user, source_id="hermes-b")
    _make_byok_gateway_model(db_session, test_user)
    bound = _make_bound_gateway_model(db_session, test_user, alias=BOUND_ALIAS_A)
    _bind_model_to_agent(db_session, test_user, agent_a, bound, alias=BOUND_ALIAS_A)

    service = OpenAIGatewayService(
        db_session, _agent_context(db_session, test_user, agent_b)
    )

    assert _listed_ids(service) == [BYOK_ALIAS]


def test_own_bound_model_visible_to_its_principal(db_session, test_user):
    """The bound agent's own credential keeps seeing its subscription model."""
    agent_a = _make_agent(db_session, test_user, source_id="hermes-a")
    _make_byok_gateway_model(db_session, test_user)
    bound = _make_bound_gateway_model(db_session, test_user, alias=BOUND_ALIAS_A)
    _bind_model_to_agent(db_session, test_user, agent_a, bound, alias=BOUND_ALIAS_A)

    service = OpenAIGatewayService(
        db_session, _agent_context(db_session, test_user, agent_a)
    )

    assert sorted(_listed_ids(service)) == sorted([BYOK_ALIAS, BOUND_ALIAS_A])


def test_byok_models_listed_for_every_principal(db_session, test_user):
    """CRITICAL regression: BYOK models stay visible to all principals."""
    agent_a = _make_agent(db_session, test_user, source_id="hermes-a")
    agent_b = _make_agent(db_session, test_user, source_id="hermes-b")
    _make_byok_gateway_model(db_session, test_user)
    bound = _make_bound_gateway_model(db_session, test_user, alias=BOUND_ALIAS_A)
    _bind_model_to_agent(db_session, test_user, agent_a, bound, alias=BOUND_ALIAS_A)

    contexts = [
        _agent_context(db_session, test_user, agent_a),
        _agent_context(db_session, test_user, agent_b),
        _legacy_context(db_session, test_user),
        _user_context(test_user),
    ]
    for context in contexts:
        service = OpenAIGatewayService(db_session, context)
        assert BYOK_ALIAS in _listed_ids(service)


def test_user_token_listing_unchanged(db_session, test_user):
    """Console user tokens keep full inventory visibility."""
    agent_a = _make_agent(db_session, test_user, source_id="hermes-a")
    _make_byok_gateway_model(db_session, test_user)
    bound = _make_bound_gateway_model(db_session, test_user, alias=BOUND_ALIAS_A)
    _bind_model_to_agent(db_session, test_user, agent_a, bound, alias=BOUND_ALIAS_A)

    service = OpenAIGatewayService(db_session, _user_context(test_user))

    assert sorted(_listed_ids(service)) == sorted([BYOK_ALIAS, BOUND_ALIAS_A])


def test_legacy_credential_excludes_bound_models_but_keeps_byok(db_session, test_user):
    """Fail closed: no managed_agent_id means no principal-bound models."""
    agent_a = _make_agent(db_session, test_user, source_id="hermes-a")
    byok = _make_byok_gateway_model(db_session, test_user)
    bound = _make_bound_gateway_model(db_session, test_user, alias=BOUND_ALIAS_A)
    _bind_model_to_agent(db_session, test_user, agent_a, bound, alias=BOUND_ALIAS_A)

    service = OpenAIGatewayService(db_session, _legacy_context(db_session, test_user))

    assert _listed_ids(service) == [BYOK_ALIAS]
    resolved = service._resolve_requested_model(BYOK_ALIAS, provider="openai")
    assert resolved.id == byok.id
    with pytest.raises(ModelGatewayAPIError) as excinfo:
        service._resolve_requested_model(BOUND_ALIAS_A, provider="openai")
    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "model_not_authorized"


def test_bound_model_without_binding_row_hidden_from_agents(db_session, test_user):
    """Fail closed: an OAuth model with no binding row serves no agent."""
    agent_a = _make_agent(db_session, test_user, source_id="hermes-a")
    _make_byok_gateway_model(db_session, test_user)
    _make_bound_gateway_model(db_session, test_user, alias=BOUND_ALIAS_A)

    service = OpenAIGatewayService(
        db_session, _agent_context(db_session, test_user, agent_a)
    )

    assert _listed_ids(service) == [BYOK_ALIAS]
    with pytest.raises(ModelGatewayAPIError) as excinfo:
        service._resolve_requested_model(BOUND_ALIAS_A, provider="openai")
    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "model_not_authorized"


def test_agents_sharing_runtime_only_see_their_own_bound_models(db_session, test_user):
    """Each agent's credential authorizes only its own bound model."""
    agent_a = _make_agent(db_session, test_user, source_id="hermes-shared")
    agent_b = _make_agent(db_session, test_user, source_id="hermes-shared-2")
    _make_byok_gateway_model(db_session, test_user)
    bound_a = _make_bound_gateway_model(db_session, test_user, alias=BOUND_ALIAS_A)
    bound_b = _make_bound_gateway_model(db_session, test_user, alias=BOUND_ALIAS_B)
    _bind_model_to_agent(db_session, test_user, agent_a, bound_a, alias=BOUND_ALIAS_A)
    _bind_model_to_agent(db_session, test_user, agent_b, bound_b, alias=BOUND_ALIAS_B)

    service_a = OpenAIGatewayService(
        db_session, _agent_context(db_session, test_user, agent_a)
    )
    service_b = OpenAIGatewayService(
        db_session, _agent_context(db_session, test_user, agent_b)
    )

    assert sorted(_listed_ids(service_a)) == sorted([BYOK_ALIAS, BOUND_ALIAS_A])
    assert sorted(_listed_ids(service_b)) == sorted([BYOK_ALIAS, BOUND_ALIAS_B])
    assert (
        service_a._resolve_requested_model(BOUND_ALIAS_A, provider="openai").id
        == bound_a.id
    )
    assert (
        service_b._resolve_requested_model(BOUND_ALIAS_B, provider="openai").id
        == bound_b.id
    )


def test_unauthorized_bound_model_resolution_returns_400_enumerating_usable(
    db_session, test_user
):
    """Requesting another agent's bound model yields the exact 400 body."""
    agent_a = _make_agent(db_session, test_user, source_id="hermes-a")
    agent_b = _make_agent(db_session, test_user, source_id="hermes-b")
    _make_byok_gateway_model(db_session, test_user)
    bound = _make_bound_gateway_model(db_session, test_user, alias=BOUND_ALIAS_A)
    _bind_model_to_agent(db_session, test_user, agent_a, bound, alias=BOUND_ALIAS_A)

    service = OpenAIGatewayService(
        db_session, _agent_context(db_session, test_user, agent_b)
    )

    with pytest.raises(ModelGatewayAPIError) as excinfo:
        service._resolve_requested_model(BOUND_ALIAS_A, provider="openai")

    error = excinfo.value
    assert error.status_code == 400
    assert error.code == "model_not_authorized"
    assert error.message == (
        f"Model '{BOUND_ALIAS_A}' is bound to another agent's subscription "
        "credentials and can't serve this credential. Models available to "
        f"this credential: {BYOK_ALIAS}."
    )
    payload = error.to_payload()
    assert payload == {
        "error": {
            "message": error.message,
            "type": "invalid_request_error",
            "param": None,
            "code": "model_not_authorized",
        }
    }


def test_unauthorized_bound_model_anthropic_surface_stays_consistent(
    db_session, test_user
):
    """The Anthropic-compat surface renders the same refusal in its envelope."""
    agent_a = _make_agent(db_session, test_user, source_id="hermes-a")
    agent_b = _make_agent(db_session, test_user, source_id="hermes-b")
    _make_byok_gateway_model(db_session, test_user)
    bound = _make_bound_gateway_model(db_session, test_user, alias=BOUND_ALIAS_A)
    _bind_model_to_agent(db_session, test_user, agent_a, bound, alias=BOUND_ALIAS_A)

    service = OpenAIGatewayService(
        db_session, _agent_context(db_session, test_user, agent_b)
    )

    with pytest.raises(ModelGatewayAPIError) as excinfo:
        service._resolve_requested_model(BOUND_ALIAS_A, provider="anthropic")

    error = excinfo.value
    assert error.status_code == 400
    assert error.code == "model_not_authorized"
    assert error.to_payload() == {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": error.message,
        },
    }


def test_hidden_suffix_alias_is_also_refused(db_session, test_user):
    """A bare-suffix request for a bound alias must not stay callable."""
    agent_a = _make_agent(db_session, test_user, source_id="hermes-a")
    agent_b = _make_agent(db_session, test_user, source_id="hermes-b")
    bound = _make_bound_gateway_model(db_session, test_user, alias=BOUND_ALIAS_A)
    _bind_model_to_agent(db_session, test_user, agent_a, bound, alias=BOUND_ALIAS_A)

    service = OpenAIGatewayService(
        db_session, _agent_context(db_session, test_user, agent_b)
    )

    with pytest.raises(ModelGatewayAPIError) as excinfo:
        service._resolve_requested_model("claude-sonnet-4-5-agent-a", provider="openai")

    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "model_not_authorized"
    assert "Models available to this credential: none." in excinfo.value.message


def test_unknown_model_still_404s_with_bound_models_present(db_session, test_user):
    """Unknown models keep the loud 404, not the authorization 400."""
    agent_a = _make_agent(db_session, test_user, source_id="hermes-a")
    agent_b = _make_agent(db_session, test_user, source_id="hermes-b")
    _make_byok_gateway_model(db_session, test_user)
    bound = _make_bound_gateway_model(db_session, test_user, alias=BOUND_ALIAS_A)
    _bind_model_to_agent(db_session, test_user, agent_a, bound, alias=BOUND_ALIAS_A)

    service = OpenAIGatewayService(
        db_session, _agent_context(db_session, test_user, agent_b)
    )

    with pytest.raises(ModelGatewayAPIError) as excinfo:
        service._resolve_requested_model("gpt-nonexistent", provider="openai")

    assert excinfo.value.status_code == 404


def test_default_selection_never_lands_on_unauthorized_model(db_session, test_user):
    """An unauthorized bound default must not be silently selected."""
    agent_a = _make_agent(db_session, test_user, source_id="hermes-a")
    agent_b = _make_agent(db_session, test_user, source_id="hermes-b")
    bound = _make_bound_gateway_model(
        db_session, test_user, alias=BOUND_ALIAS_A, is_default=True
    )
    _bind_model_to_agent(db_session, test_user, agent_a, bound, alias=BOUND_ALIAS_A)

    unauthorized_service = OpenAIGatewayService(
        db_session, _agent_context(db_session, test_user, agent_b)
    )
    with pytest.raises(ModelGatewayAPIError) as excinfo:
        unauthorized_service._resolve_requested_model(None, provider="openai")
    assert excinfo.value.status_code == 404
    assert "No gateway-enabled default model configured" in excinfo.value.message

    authorized_service = OpenAIGatewayService(
        db_session, _agent_context(db_session, test_user, agent_a)
    )
    resolved = authorized_service._resolve_requested_model(None, provider="openai")
    assert resolved.id == bound.id
