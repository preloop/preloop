"""Lazy auto-registration of unknown Codex models for ChatGPT OAuth.

Codex CLI updates ship new built-in identifiers (``gpt-6-astra``, dated
gpt-5.6 snapshots, o-series). ``preloop models sync`` cannot discover
against principal-bound ChatGPT OAuth, so a registry snapshotted at onboard
time would 404 those requests until the user re-onboards. ``_maybe_autoregister_codex_family_model``
closes the gap: when the requesting principal already holds an authorized
openai-codex subscription-OAuth model, an unknown Codex-shaped request
creates a sibling ``AIModel`` sharing the SAME credential secret plus a
managed-agent binding, then serves the request.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from preloop.config import settings
from preloop.models.crud import (
    crud_ai_model,
    crud_api_key,
    crud_managed_agent_ai_model_binding,
)
from preloop.models.models import ManagedAgent
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.secret_service import OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE

PINNED_ALIAS = "openai/gpt-5.4"


def _make_agent(db_session, test_user) -> ManagedAgent:
    now = datetime.now(UTC).replace(tzinfo=None)
    agent = ManagedAgent(
        id=uuid4(),
        account_id=test_user.account_id,
        runtime_session_id=None,
        agent_kind="codex",
        session_source_type="codex",
        session_source_id=f"codex-{uuid4().hex[:8]}",
        display_name="Codex CLI",
        enrolled_via="runtime_session_token",
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=now,
    )
    db_session.add(agent)
    db_session.commit()
    return agent


def _make_subscription_model(db_session, test_user, *, alias: str = PINNED_ALIAS):
    identifier = alias.partition("/")[2]
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"Codex CLI {alias} {uuid4()}",
            "provider_name": "openai-codex",
            "model_identifier": identifier,
            "credential_type": OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
            "credential_payload": {
                "access": "oauth-access-token",
                "account_id": "chatgpt-account",
            },
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


def _bind(db_session, test_user, agent, ai_model, *, alias: str):
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


def _restrict_to_account(service: OpenAIGatewayService, account_id) -> None:
    """Ignore system-default rows so a shared test DB cannot decrypt-fail the loop.

    ``_get_account_models`` also returns ``account_id IS NULL`` rows. Those are
    unrelated to this path; a seeded secret encrypted under another key would
    raise before autoregister runs.
    """
    original = service._get_account_models

    def _account_only():
        return [
            model
            for model in original()
            if str(getattr(model, "account_id", "")) == str(account_id)
        ]

    service._get_account_models = _account_only  # type: ignore[method-assign]


def _enrolled_service(db_session, test_user):
    agent = _make_agent(db_session, test_user)
    pinned = _make_subscription_model(db_session, test_user)
    _bind(db_session, test_user, agent, pinned, alias=PINNED_ALIAS)
    service = OpenAIGatewayService(
        db_session, _agent_context(db_session, test_user, agent)
    )
    _restrict_to_account(service, test_user.account_id)
    return agent, pinned, service


def test_unknown_astra_model_autoregisters(db_session, test_user):
    """A new Codex-built-in id resolves by lazily creating a sibling model."""
    agent, pinned, service = _enrolled_service(db_session, test_user)

    resolved = service._resolve_requested_model("gpt-6-astra", provider="openai")

    assert resolved.model_identifier == "gpt-6-astra"
    assert resolved.provider_name == "openai-codex"
    assert resolved.credentials_secret_id == pinned.credentials_secret_id
    bindings = crud_managed_agent_ai_model_binding.list_for_agent(
        db_session,
        account_id=str(test_user.account_id),
        agent_id=str(agent.id),
    )
    aliases = {binding.gateway_alias for binding in bindings}
    assert "openai/gpt-6-astra" in aliases


def test_openai_prefix_autoregisters(db_session, test_user):
    """openai/<id> is the alias spelling Codex onboarding already uses."""
    _agent, pinned, service = _enrolled_service(db_session, test_user)

    resolved = service._resolve_requested_model("openai/gpt-6-astra", provider="openai")

    assert resolved.model_identifier == "gpt-6-astra"
    assert resolved.credentials_secret_id == pinned.credentials_secret_id


def test_openai_codex_prefix_autoregisters(db_session, test_user):
    """openai-codex/<id> also maps onto the ChatGPT-OAuth credential."""
    _agent, _pinned, service = _enrolled_service(db_session, test_user)

    resolved = service._resolve_requested_model(
        "openai-codex/gpt-6-astra-pro", provider="openai"
    )

    assert resolved.model_identifier == "gpt-6-astra-pro"


def test_autoregistered_model_resolves_again_without_new_rows(db_session, test_user):
    """Second request finds the registered row; no duplicate is created."""
    _agent, _pinned, service = _enrolled_service(db_session, test_user)

    first = service._resolve_requested_model("gpt-6-astra", provider="openai")
    fresh_service = OpenAIGatewayService(db_session, service.auth_context)
    _restrict_to_account(fresh_service, test_user.account_id)
    second = fresh_service._resolve_requested_model("gpt-6-astra", provider="openai")

    assert first.id == second.id
    count = sum(
        1
        for model in crud_ai_model.get_by_account(
            db_session, account_id=test_user.account_id
        )
        if model.model_identifier == "gpt-6-astra"
    )
    assert count == 1


def test_o_series_autoregisters(db_session, test_user):
    """o-series ids (o4-mini) are Codex-selectable too."""
    _agent, _pinned, service = _enrolled_service(db_session, test_user)

    resolved = service._resolve_requested_model("o4-mini", provider="openai")

    assert resolved.model_identifier == "o4-mini"


def test_claude_model_still_404s(db_session, test_user):
    """Auto-registration is scoped to Codex/OpenAI identifiers only."""
    _agent, _pinned, service = _enrolled_service(db_session, test_user)

    with pytest.raises(ModelGatewayAPIError) as err:
        service._resolve_requested_model("claude-sonnet-4-9", provider="openai")
    assert err.value.status_code == 404


def test_anthropic_protocol_never_autoregisters(db_session, test_user):
    """Only the OpenAI protocol path may relax the Codex registry check."""
    _agent, _pinned, service = _enrolled_service(db_session, test_user)

    with pytest.raises(ModelGatewayAPIError) as err:
        service._resolve_requested_model("gpt-6-astra", provider="anthropic")
    assert err.value.status_code == 404


def test_other_provider_prefix_never_autoregisters(db_session, test_user):
    """azure/... identifiers are unreachable via the ChatGPT OAuth key."""
    _agent, _pinned, service = _enrolled_service(db_session, test_user)

    with pytest.raises(ModelGatewayAPIError) as err:
        service._resolve_requested_model("azure/gpt-6-astra", provider="openai")
    assert err.value.status_code == 404


def test_no_subscription_template_never_autoregisters(db_session, test_user):
    """BYOK-only accounts keep strict registry semantics."""
    agent = _make_agent(db_session, test_user)
    byok = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"BYOK {uuid4()}",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_key": "sk-byok",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": PINNED_ALIAS,
                    "provider_adapter": "preloop",
                }
            },
        },
        account_id=test_user.account_id,
    )
    _bind(db_session, test_user, agent, byok, alias=PINNED_ALIAS)
    service = OpenAIGatewayService(
        db_session, _agent_context(db_session, test_user, agent)
    )
    _restrict_to_account(service, test_user.account_id)

    with pytest.raises(ModelGatewayAPIError) as err:
        service._resolve_requested_model("gpt-6-astra", provider="openai")
    assert err.value.status_code == 404


def test_disabled_flag_never_autoregisters(db_session, test_user, monkeypatch):
    """The feature flag hard-disables the relaxation."""
    _agent, _pinned, service = _enrolled_service(db_session, test_user)
    monkeypatch.setattr(
        settings, "model_gateway_codex_family_autoregister_enabled", False
    )

    with pytest.raises(ModelGatewayAPIError) as err:
        service._resolve_requested_model("gpt-6-astra", provider="openai")
    assert err.value.status_code == 404


def test_failed_autoregistration_preserves_unrelated_pending_state(
    db_session, test_user, monkeypatch
):
    """A registration failure rolls back ONLY the savepoint, not the session."""
    agent, _pinned, service = _enrolled_service(db_session, test_user)

    agent.display_name = "Renamed Before Autoregister"
    db_session.flush()

    def _boom(*args, **kwargs):
        raise ValueError("simulated registration failure")

    monkeypatch.setattr(
        "preloop.services.openai_gateway.crud_managed_agent_ai_model_binding.create",
        _boom,
    )

    with pytest.raises(ModelGatewayAPIError) as err:
        service._resolve_requested_model("gpt-6-astra", provider="openai")
    assert err.value.status_code == 404

    assert agent.display_name == "Renamed Before Autoregister"
    db_session.commit()
    db_session.refresh(agent)
    assert agent.display_name == "Renamed Before Autoregister"
    leaked = [
        model
        for model in crud_ai_model.get_by_account(
            db_session, account_id=test_user.account_id
        )
        if model.model_identifier == "gpt-6-astra"
    ]
    assert leaked == []


def test_user_token_never_autoregisters(db_session, test_user):
    """Without a managed agent to bind, no row is created (fail closed)."""
    _make_subscription_model(db_session, test_user)
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="user-jwt", user=test_user)
    )
    _restrict_to_account(service, test_user.account_id)

    with pytest.raises(ModelGatewayAPIError) as err:
        service._resolve_requested_model("gpt-6-astra", provider="openai")
    assert err.value.status_code == 404


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("gpt-6-astra", "gpt-6-astra"),
        ("openai/gpt-6-astra", "gpt-6-astra"),
        ("openai-codex/gpt-5.6-sol", "gpt-5.6-sol"),
        ("chatgpt-4o", "chatgpt-4o"),
        ("o3", "o3"),
        ("o4-mini", "o4-mini"),
        ("claude-sonnet-4-9", None),
        ("azure/gpt-6-astra", None),
        ("bedrock/gpt-6-astra", None),
        ("openrouter/gpt-6-astra", None),
        ("", None),
        ("openai/", None),
    ],
)
def test_codex_autoregister_identifier(raw, expected):
    assert OpenAIGatewayService._codex_autoregister_identifier(raw) == expected
