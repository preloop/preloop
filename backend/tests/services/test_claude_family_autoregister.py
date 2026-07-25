"""Lazy auto-registration of unknown Claude models for subscription OAuth.

Claude Code updates ship new dated ``claude-*`` identifiers, and family env
pins may reference a family the onboarding import missed. A registry
snapshotted at onboard time would 404 those requests until the user
re-onboards, even though Anthropic itself authorizes whatever the
subscription may use. ``_maybe_autoregister_claude_family_model`` closes the
gap: when the requesting principal already holds an authorized Anthropic
subscription-OAuth model, an unknown ``claude-*`` request creates a sibling
``AIModel`` sharing the SAME credential secret (one live OAuth token lineage)
plus a managed-agent binding, then serves the request.
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
from preloop.services.secret_service import (
    ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
)

PINNED_ALIAS = "anthropic/claude-fable-5"


def _make_agent(db_session, test_user) -> ManagedAgent:
    now = datetime.now(UTC).replace(tzinfo=None)
    agent = ManagedAgent(
        id=uuid4(),
        account_id=test_user.account_id,
        runtime_session_id=None,
        agent_kind="claude_code",
        session_source_type="claude_code",
        session_source_id=f"claude-code-{uuid4().hex[:8]}",
        display_name="Claude Code",
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
            "name": f"Claude Code {alias} {uuid4()}",
            "provider_name": "anthropic",
            "model_identifier": identifier,
            "credential_type": ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
            "credential_payload": {"access": "oauth-access-token"},
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


def _enrolled_service(db_session, test_user):
    agent = _make_agent(db_session, test_user)
    pinned = _make_subscription_model(db_session, test_user)
    _bind(db_session, test_user, agent, pinned, alias=PINNED_ALIAS)
    service = OpenAIGatewayService(
        db_session, _agent_context(db_session, test_user, agent)
    )
    return agent, pinned, service


def test_unknown_claude_model_autoregisters(db_session, test_user):
    """A new dated claude-* id resolves by lazily creating a sibling model."""
    agent, pinned, service = _enrolled_service(db_session, test_user)

    resolved = service._resolve_requested_model(
        "anthropic/claude-sonnet-4-9", provider="anthropic"
    )

    assert resolved.model_identifier == "claude-sonnet-4-9"
    # One live OAuth token lineage: the sibling must SHARE the secret row,
    # never copy token material.
    assert resolved.credentials_secret_id == pinned.credentials_secret_id
    # The binding admits the new row on the next request's authorized set.
    bindings = crud_managed_agent_ai_model_binding.list_for_agent(
        db_session,
        account_id=str(test_user.account_id),
        agent_id=str(agent.id),
    )
    aliases = {binding.gateway_alias for binding in bindings}
    assert "anthropic/claude-sonnet-4-9" in aliases


def test_autoregistered_model_resolves_again_without_new_rows(db_session, test_user):
    """Second request finds the registered row; no duplicate is created."""
    _agent, _pinned, service = _enrolled_service(db_session, test_user)

    first = service._resolve_requested_model("claude-haiku-4-9", provider="anthropic")
    fresh_service = OpenAIGatewayService(db_session, service.auth_context)
    second = fresh_service._resolve_requested_model(
        "claude-haiku-4-9", provider="anthropic"
    )

    assert first.id == second.id
    count = sum(
        1
        for model in crud_ai_model.get_by_account(
            db_session, account_id=test_user.account_id
        )
        if model.model_identifier == "claude-haiku-4-9"
    )
    assert count == 1


def test_variant_marker_resolves_to_base_model(db_session, test_user):
    """claude-fable-5[1m] addresses the registered claude-fable-5 row."""
    _agent, pinned, service = _enrolled_service(db_session, test_user)

    resolved = service._resolve_requested_model(
        "claude-fable-5[1m]", provider="anthropic"
    )

    assert resolved.id == pinned.id


def test_variant_marker_preserved_on_passthrough_upstream_ref(db_session, test_user):
    """The 1M selector is forwarded verbatim upstream, not silently dropped."""
    _agent, pinned, service = _enrolled_service(db_session, test_user)

    assert (
        service._passthrough_upstream_model_ref(pinned, "claude-fable-5[1m]")
        == "claude-fable-5[1m]"
    )
    # Exact/base requests keep the canonical identifier.
    assert (
        service._passthrough_upstream_model_ref(pinned, "claude-fable-5")
        == "claude-fable-5"
    )
    assert (
        service._passthrough_upstream_model_ref(pinned, PINNED_ALIAS)
        == "claude-fable-5"
    )
    # A DIFFERENT model's variant must not leak through.
    assert (
        service._passthrough_upstream_model_ref(pinned, "claude-opus-4-6[1m]")
        == "claude-fable-5"
    )


def test_non_claude_model_still_404s(db_session, test_user):
    """Auto-registration is scoped to claude-* identifiers only."""
    _agent, _pinned, service = _enrolled_service(db_session, test_user)

    with pytest.raises(ModelGatewayAPIError) as err:
        service._resolve_requested_model("gpt-nonexistent", provider="anthropic")
    assert err.value.status_code == 404


def test_openai_protocol_never_autoregisters(db_session, test_user):
    """Only the Anthropic protocol path may relax the registry check."""
    _agent, _pinned, service = _enrolled_service(db_session, test_user)

    with pytest.raises(ModelGatewayAPIError) as err:
        service._resolve_requested_model(
            "anthropic/claude-sonnet-4-9", provider="openai"
        )
    assert err.value.status_code == 404


def test_other_provider_prefix_never_autoregisters(db_session, test_user):
    """bedrock/... identifiers are unreachable via the Anthropic OAuth key."""
    _agent, _pinned, service = _enrolled_service(db_session, test_user)

    with pytest.raises(ModelGatewayAPIError) as err:
        service._resolve_requested_model(
            "bedrock/claude-sonnet-4-9", provider="anthropic"
        )
    assert err.value.status_code == 404


def test_no_subscription_template_never_autoregisters(db_session, test_user):
    """BYOK-only accounts keep strict registry semantics."""
    agent = _make_agent(db_session, test_user)
    byok = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"BYOK {uuid4()}",
            "provider_name": "anthropic",
            "model_identifier": "claude-fable-5",
            "api_key": "sk-ant-byok",
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

    with pytest.raises(ModelGatewayAPIError) as err:
        service._resolve_requested_model("claude-sonnet-4-9", provider="anthropic")
    assert err.value.status_code == 404


def test_disabled_flag_never_autoregisters(db_session, test_user, monkeypatch):
    """The feature flag hard-disables the relaxation."""
    _agent, _pinned, service = _enrolled_service(db_session, test_user)
    monkeypatch.setattr(
        settings, "model_gateway_claude_family_autoregister_enabled", False
    )

    with pytest.raises(ModelGatewayAPIError) as err:
        service._resolve_requested_model("claude-sonnet-4-9", provider="anthropic")
    assert err.value.status_code == 404


def test_user_token_never_autoregisters(db_session, test_user):
    """Without a managed agent to bind, no row is created (fail closed)."""
    _make_subscription_model(db_session, test_user)
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="user-jwt", user=test_user)
    )

    with pytest.raises(ModelGatewayAPIError) as err:
        service._resolve_requested_model("claude-sonnet-4-9", provider="anthropic")
    assert err.value.status_code == 404
