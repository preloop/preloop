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

import logging
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
from preloop.services import openai_gateway as gateway_module
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.secret_service import (
    ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
)

PINNED_ALIAS = "anthropic/claude-fable-5"


@pytest.fixture(autouse=True)
def _claude_family_verification_is_opt_in(monkeypatch):
    """Keep the suite on the pre-verification contract by default.

    The existing tests were written against registration without an upstream
    probe. This leaves them on that path (and out of the network), while the
    verification tests below turn the flag on explicitly. The process-level
    verification cache is cleared around every test so one test's answer never
    leaks into the next.
    """
    monkeypatch.setattr(
        settings, "model_gateway_claude_family_autoregister_verify_upstream", False
    )
    with gateway_module._CLAUDE_FAMILY_VERIFY_CACHE_LOCK:
        gateway_module._CLAUDE_FAMILY_VERIFY_CACHE.clear()
    yield
    with gateway_module._CLAUDE_FAMILY_VERIFY_CACHE_LOCK:
        gateway_module._CLAUDE_FAMILY_VERIFY_CACHE.clear()


def _enable_verification(monkeypatch, *, outcome: str) -> list[str]:
    """Turn on verification and stub the upstream probe, recording its calls."""
    calls: list[str] = []

    def _probe(*, identifier: str, access_token: str) -> str:
        # Record only the identifier: the token must never reach a test
        # failure message or a log assertion.
        calls.append(identifier)
        return outcome

    monkeypatch.setattr(
        settings, "model_gateway_claude_family_autoregister_verify_upstream", True
    )
    monkeypatch.setattr(gateway_module, "_probe_anthropic_model_identifier", _probe)
    return calls


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


def test_failed_autoregistration_preserves_unrelated_pending_state(
    db_session, test_user, monkeypatch
):
    """A registration failure rolls back ONLY the savepoint, not the session.

    The gateway request pipeline may hold unrelated pending writes when the
    lazy registration runs; a session-level rollback would silently discard
    them. The registration writes live under ``begin_nested`` so a failure
    releases just the savepoint.
    """
    agent, _pinned, service = _enrolled_service(db_session, test_user)

    # Unrelated pending (uncommitted) state from "earlier in the pipeline".
    agent.display_name = "Renamed Before Autoregister"
    db_session.flush()

    def _boom(*args, **kwargs):
        raise ValueError("simulated registration failure")

    monkeypatch.setattr(
        "preloop.services.openai_gateway.crud_managed_agent_ai_model_binding.create",
        _boom,
    )

    with pytest.raises(ModelGatewayAPIError) as err:
        service._resolve_requested_model("claude-sonnet-4-9", provider="anthropic")
    assert err.value.status_code == 404

    # The unrelated pending write survived the failed registration.
    assert agent.display_name == "Renamed Before Autoregister"
    db_session.commit()
    db_session.refresh(agent)
    assert agent.display_name == "Renamed Before Autoregister"
    # And no half-registered model row leaked.
    leaked = [
        model
        for model in crud_ai_model.get_by_account(
            db_session, account_id=test_user.account_id
        )
        if model.model_identifier == "claude-sonnet-4-9"
    ]
    assert leaked == []


def test_user_token_never_autoregisters(db_session, test_user):
    """Without a managed agent to bind, no row is created (fail closed)."""
    _make_subscription_model(db_session, test_user)
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="user-jwt", user=test_user)
    )

    with pytest.raises(ModelGatewayAPIError) as err:
        service._resolve_requested_model("claude-sonnet-4-9", provider="anthropic")
    assert err.value.status_code == 404


def test_upstream_rejection_blocks_autoregistration(db_session, test_user, monkeypatch):
    """An identifier Anthropic 404s never becomes a catalog row."""
    _agent, _pinned, service = _enrolled_service(db_session, test_user)
    calls = _enable_verification(monkeypatch, outcome="rejected")

    with pytest.raises(ModelGatewayAPIError) as err:
        service._resolve_requested_model(
            "anthropic/claude-made-up-9", provider="anthropic"
        )
    assert err.value.status_code == 404
    assert calls == ["claude-made-up-9"]

    leaked = [
        model
        for model in crud_ai_model.get_by_account(
            db_session, account_id=test_user.account_id
        )
        if model.model_identifier == "claude-made-up-9"
    ]
    assert leaked == []


def test_upstream_rejection_is_negative_cached(db_session, test_user, monkeypatch):
    """A rejected id is not re-probed within the negative-cache window."""
    _agent, _pinned, service = _enrolled_service(db_session, test_user)
    calls = _enable_verification(monkeypatch, outcome="rejected")

    for _ in range(2):
        with pytest.raises(ModelGatewayAPIError):
            service._resolve_requested_model(
                "anthropic/claude-made-up-9", provider="anthropic"
            )
    assert calls == ["claude-made-up-9"]


def test_negative_cache_expires(db_session, test_user, monkeypatch):
    """After the negative TTL the identifier is probed again."""
    _agent, _pinned, service = _enrolled_service(db_session, test_user)
    calls = _enable_verification(monkeypatch, outcome="rejected")
    monkeypatch.setattr(
        gateway_module, "_CLAUDE_FAMILY_VERIFY_NEGATIVE_TTL_SECONDS", 0
    )

    for _ in range(2):
        with pytest.raises(ModelGatewayAPIError):
            service._resolve_requested_model(
                "anthropic/claude-made-up-9", provider="anthropic"
            )
    assert calls == ["claude-made-up-9", "claude-made-up-9"]


def test_upstream_acceptance_registers_marked_verified(
    db_session, test_user, monkeypatch
):
    """A 200 still creates exactly today's row, now marked verified."""
    agent, pinned, service = _enrolled_service(db_session, test_user)
    _enable_verification(monkeypatch, outcome="verified")

    resolved = service._resolve_requested_model(
        "anthropic/claude-sonnet-4-9", provider="anthropic"
    )

    assert resolved.model_identifier == "claude-sonnet-4-9"
    assert resolved.credentials_secret_id == pinned.credentials_secret_id
    assert resolved.meta_data["managed_by"] == (
        "model-gateway claude-family autoregister"
    )
    assert resolved.meta_data["upstream_verification"] == "verified"
    bindings = crud_managed_agent_ai_model_binding.list_for_agent(
        db_session,
        account_id=str(test_user.account_id),
        agent_id=str(agent.id),
    )
    assert "anthropic/claude-sonnet-4-9" in {
        binding.gateway_alias for binding in bindings
    }


def test_inconclusive_probe_falls_back_marked_unverified(
    db_session, test_user, monkeypatch, caplog
):
    """A 5xx/transport answer registers as before and is marked unverified."""
    _agent, _pinned, service = _enrolled_service(db_session, test_user)
    _enable_verification(monkeypatch, outcome="unknown")

    with caplog.at_level("WARNING", logger="preloop.services.openai_gateway"):
        resolved = service._resolve_requested_model(
            "anthropic/claude-sonnet-4-9", provider="anthropic"
        )

    assert resolved.meta_data["upstream_verification"] == "unverified"
    inconclusive = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "inconclusive" in record.getMessage()
    ]
    assert len(inconclusive) == 1
    # The access token is never part of a log line.
    assert "oauth-access-token" not in caplog.text


def test_verify_disabled_skips_probe_and_metadata(
    db_session, test_user, monkeypatch
):
    """Flag off: no probe, and no new metadata beyond today's row."""
    _agent, _pinned, service = _enrolled_service(db_session, test_user)
    # The autouse fixture disabled verification; a probe would be a bug.
    monkeypatch.setattr(
        gateway_module,
        "_probe_anthropic_model_identifier",
        lambda **kwargs: pytest.fail("probe must not run when verification is off"),
    )

    resolved = service._resolve_requested_model(
        "anthropic/claude-sonnet-4-9", provider="anthropic"
    )

    assert resolved.meta_data["managed_by"] == (
        "model-gateway claude-family autoregister"
    )
    assert "upstream_verification" not in resolved.meta_data


@pytest.mark.parametrize(
    ("status", "expected"),
    [(200, "verified"), (404, "rejected"), (500, "unknown"), (401, "unknown")],
)
def test_probe_anthropic_model_identifier_classifies(monkeypatch, status, expected):
    """The probe is a body-less OAuth GET whose status maps to one outcome."""
    import httpx

    seen = {}

    def _get(url, *, headers, timeout):
        # A GET with no body argument: the client's request body can never
        # reach the verification call.
        seen["url"] = url
        seen["headers"] = headers
        seen["timeout"] = timeout
        return httpx.Response(status)

    monkeypatch.setattr(gateway_module.httpx, "get", _get)

    outcome = gateway_module._probe_anthropic_model_identifier(
        identifier="claude-made-up-9", access_token="secret-token"
    )

    assert outcome == expected
    assert seen["url"] == "https://api.anthropic.com/v1/models/claude-made-up-9"
    assert seen["headers"]["Authorization"] == "Bearer secret-token"
    assert seen["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["timeout"] == gateway_module._ANTHROPIC_MODEL_VERIFY_TIMEOUT_SECONDS


def test_probe_transport_error_is_unknown(monkeypatch):
    """A connection failure degrades to the register-anyway fallback."""
    import httpx

    def _get(url, *, headers, timeout):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(gateway_module.httpx, "get", _get)

    outcome = gateway_module._probe_anthropic_model_identifier(
        identifier="claude-made-up-9", access_token="secret-token"
    )

    assert outcome == "unknown"
