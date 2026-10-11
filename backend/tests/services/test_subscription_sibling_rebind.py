"""Re-onboarding must not strand gateway-autoregistered subscription siblings.

Scenario reproduced from a real account: Codex CLI was onboarded on
``openai/gpt-6-astra``; the user then picked ``gpt-6.1-sol`` in Codex's own
``/model`` picker and the gateway auto-registered a sibling row sharing the
same ChatGPT-OAuth secret, bound to the agent. A later re-onboard replaced the
agent's binding set with the single onboarded model, which deactivated the
sibling bindings. The next ``gpt-6.1-sol`` request then matched a
principal-bound row with no active binding and was refused with a 400
("bound to another agent's subscription credentials") before the autoregister
path — which knows how to bind an existing same-secret row — ever ran.

Two fixes are pinned here:

- ``replace_for_agent`` keeps autoregistered sibling bindings whose model
  shares the credential secret with a model in the new binding set, and still
  deactivates siblings on a different secret (a credential lineage the agent
  no longer holds).
- ``_resolve_requested_model`` re-binds a same-secret sibling instead of
  refusing, reactivating the inactive slot rather than minting a duplicate row
  or a duplicate binding.
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
from preloop.models.models.managed_agent_ai_model_binding import (
    AUTOREGISTER_CONFIG_KEY_PREFIX,
)
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.secret_service import OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE

PRIMARY_ALIAS = "openai/gpt-6-astra"
SIBLING_ALIAS = "openai/gpt-6.1-sol"
SIBLING_IDENTIFIER = "gpt-6.1-sol"


def _make_agent(db_session, test_user, *, label: str = "Codex CLI") -> ManagedAgent:
    now = datetime.now(UTC).replace(tzinfo=None)
    agent = ManagedAgent(
        id=uuid4(),
        account_id=test_user.account_id,
        runtime_session_id=None,
        agent_kind="codex",
        session_source_type="codex",
        session_source_id=f"codex-{uuid4().hex[:8]}",
        display_name=label,
        enrolled_via="runtime_session_token",
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=now,
    )
    db_session.add(agent)
    db_session.commit()
    return agent


def _make_codex_oauth_model(db_session, test_user, *, alias: str, managed_by: str):
    identifier = alias.partition("/")[2]
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"Codex CLI {alias} {uuid4()}",
            "provider_name": "openai-codex",
            "model_identifier": identifier,
            "credential_type": OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
            "credential_payload": {
                "access": f"oauth-access-{uuid4().hex[:6]}",
                "account_id": "chatgpt-account",
            },
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": alias,
                    "provider_adapter": "preloop",
                },
                "managed_by": managed_by,
            },
        },
        account_id=test_user.account_id,
    )


def _make_same_secret_sibling(db_session, test_user, template, *, alias: str):
    """Create a sibling row on the template's secret, as autoregister does."""
    identifier = alias.partition("/")[2]
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"Codex CLI {alias}",
            "provider_name": "openai-codex",
            "model_identifier": identifier,
            "api_endpoint": template.api_endpoint,
            "credentials_secret_id": template.credentials_secret_id,
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": alias,
                    "provider_adapter": "preloop",
                },
                "managed_by": "model-gateway codex-family autoregister",
            },
        },
        account_id=test_user.account_id,
    )


def _onboard_binding(ai_model, *, alias: str, is_primary: bool = True) -> dict:
    return {
        "config_key": "model",
        "gateway_alias": alias,
        "ai_model_id": str(ai_model.id),
        "is_primary": is_primary,
        "status": "gateway_ready",
    }


def _autoregister_binding(ai_model, *, alias: str) -> dict:
    identifier = alias.partition("/")[2]
    return {
        "config_key": f"{AUTOREGISTER_CONFIG_KEY_PREFIX}{identifier}",
        "gateway_alias": alias,
        "ai_model_id": str(ai_model.id),
        "is_primary": False,
        "status": "gateway_ready",
    }


def _replace(db_session, test_user, agent, bindings):
    return crud_managed_agent_ai_model_binding.replace_for_agent(
        db_session,
        account_id=str(test_user.account_id),
        agent_id=str(agent.id),
        bindings=bindings,
    )


def _all_bindings(db_session, test_user, agent):
    return crud_managed_agent_ai_model_binding.list_for_agent(
        db_session,
        account_id=str(test_user.account_id),
        agent_id=str(agent.id),
        include_inactive=True,
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


def _service(db_session, test_user, agent) -> OpenAIGatewayService:
    service = OpenAIGatewayService(
        db_session, _agent_context(db_session, test_user, agent)
    )
    original = service._get_account_models

    def _account_only():
        return [
            model
            for model in original()
            if str(getattr(model, "account_id", "")) == str(test_user.account_id)
        ]

    service._get_account_models = _account_only  # type: ignore[method-assign]
    return service


def _count_rows(db_session, test_user, identifier: str) -> int:
    return sum(
        1
        for model in crud_ai_model.get_by_account(
            db_session, account_id=test_user.account_id
        )
        if model.model_identifier == identifier
    )


def _enrolled_with_sibling(db_session, test_user):
    """Agent onboarded on PRIMARY_ALIAS with an autoregistered SIBLING_ALIAS."""
    agent = _make_agent(db_session, test_user)
    primary = _make_codex_oauth_model(
        db_session, test_user, alias=PRIMARY_ALIAS, managed_by="preloop agents onboard"
    )
    sibling = _make_same_secret_sibling(
        db_session, test_user, primary, alias=SIBLING_ALIAS
    )
    _replace(
        db_session,
        test_user,
        agent,
        [
            _onboard_binding(primary, alias=PRIMARY_ALIAS),
            _autoregister_binding(sibling, alias=SIBLING_ALIAS),
        ],
    )
    return agent, primary, sibling


# --- replace_for_agent -------------------------------------------------------


def test_reonboard_keeps_same_secret_autoregistered_siblings(db_session, test_user):
    """A re-onboard payload naming only the primary keeps the sibling active."""
    agent, primary, sibling = _enrolled_with_sibling(db_session, test_user)

    active = _replace(
        db_session, test_user, agent, [_onboard_binding(primary, alias=PRIMARY_ALIAS)]
    )

    by_alias = {binding.gateway_alias: binding for binding in active}
    assert set(by_alias) == {PRIMARY_ALIAS, SIBLING_ALIAS}
    assert by_alias[PRIMARY_ALIAS].is_primary is True
    assert by_alias[SIBLING_ALIAS].is_primary is False
    assert by_alias[SIBLING_ALIAS].status == "gateway_ready"
    assert str(by_alias[SIBLING_ALIAS].ai_model_id) == str(sibling.id)


def test_reonboard_drops_autoregistered_siblings_on_another_secret(
    db_session, test_user
):
    """A sibling on a credential the agent no longer holds is deactivated."""
    agent, primary, _sibling = _enrolled_with_sibling(db_session, test_user)
    # Re-onboard onto a freshly imported model with its own secret (a new
    # ChatGPT login): the old lineage's siblings must not stay authorized.
    fresh_primary = _make_codex_oauth_model(
        db_session,
        test_user,
        alias="openai/gpt-6-astra-fresh",
        managed_by="preloop agents onboard",
    )
    assert fresh_primary.credentials_secret_id != primary.credentials_secret_id

    active = _replace(
        db_session,
        test_user,
        agent,
        [_onboard_binding(fresh_primary, alias="openai/gpt-6-astra-fresh")],
    )

    assert [binding.gateway_alias for binding in active] == ["openai/gpt-6-astra-fresh"]
    inactive = {
        binding.gateway_alias
        for binding in _all_bindings(db_session, test_user, agent)
        if binding.status == "inactive"
    }
    assert inactive == {PRIMARY_ALIAS, SIBLING_ALIAS}


def test_reonboard_still_deactivates_explicit_bindings_not_in_payload(
    db_session, test_user
):
    """Only gateway-autoregistered slots get the carve-out; CLI slots are replaced."""
    agent, primary, sibling = _enrolled_with_sibling(db_session, test_user)
    # Promote the sibling to an explicit (CLI-owned) slot on the same secret.
    _replace(
        db_session,
        test_user,
        agent,
        [
            _onboard_binding(primary, alias=PRIMARY_ALIAS),
            {
                "config_key": "model.fallback",
                "gateway_alias": SIBLING_ALIAS,
                "ai_model_id": str(sibling.id),
                "is_primary": False,
            },
        ],
    )

    active = _replace(
        db_session, test_user, agent, [_onboard_binding(primary, alias=PRIMARY_ALIAS)]
    )

    active_keys = {(b.config_key, b.gateway_alias) for b in active}
    assert ("model.fallback", SIBLING_ALIAS) not in active_keys
    # The autoregistered slot for the same model is still kept.
    assert (
        f"{AUTOREGISTER_CONFIG_KEY_PREFIX}{SIBLING_IDENTIFIER}",
        SIBLING_ALIAS,
    ) in active_keys


# --- gateway resolution ------------------------------------------------------


def _strand_sibling(db_session, test_user, agent, sibling):
    """Force the pre-fix state: the sibling's binding row exists but is inactive."""
    for binding in _all_bindings(db_session, test_user, agent):
        if str(binding.ai_model_id) == str(sibling.id):
            binding.status = "inactive"
            db_session.add(binding)
    db_session.commit()
    assert all(
        b.status == "inactive"
        for b in _all_bindings(db_session, test_user, agent)
        if str(b.ai_model_id) == str(sibling.id)
    )


def test_stranded_same_secret_sibling_is_rebound_not_refused(db_session, test_user):
    """The exact production failure: resolve succeeds and the slot is reactivated."""
    agent, _primary, sibling = _enrolled_with_sibling(db_session, test_user)
    _strand_sibling(db_session, test_user, agent, sibling)
    service = _service(db_session, test_user, agent)

    resolved = service._resolve_requested_model(SIBLING_IDENTIFIER, provider="openai")

    assert resolved.id == sibling.id
    assert _count_rows(db_session, test_user, SIBLING_IDENTIFIER) == 1
    sibling_bindings = [
        b
        for b in _all_bindings(db_session, test_user, agent)
        if str(b.ai_model_id) == str(sibling.id)
    ]
    assert len(sibling_bindings) == 1
    assert sibling_bindings[0].status == "gateway_ready"

    # A fresh service (no cached authorization) now resolves on the fast path.
    again = _service(db_session, test_user, agent)
    assert again._resolve_requested_model(SIBLING_ALIAS, provider="openai").id == (
        sibling.id
    )
    assert SIBLING_ALIAS in [item["id"] for item in again.list_models()["data"]]


def test_rebind_works_for_every_codex_spelling(db_session, test_user):
    """bare, openai/ and openai-codex/ spellings all rebind the same row."""
    agent, _primary, sibling = _enrolled_with_sibling(db_session, test_user)
    _strand_sibling(db_session, test_user, agent, sibling)

    for spelling in (
        f"openai-codex/{SIBLING_IDENTIFIER}",
        SIBLING_ALIAS,
        SIBLING_IDENTIFIER,
    ):
        service = _service(db_session, test_user, agent)
        assert service._resolve_requested_model(spelling, provider="openai").id == (
            sibling.id
        )
    assert _count_rows(db_session, test_user, SIBLING_IDENTIFIER) == 1


def test_sibling_on_another_lineage_is_still_refused(db_session, test_user):
    """A bound row on a different secret stays another agent's; the 400 holds."""
    agent_a, _primary_a, sibling_a = _enrolled_with_sibling(db_session, test_user)
    agent_b = _make_agent(db_session, test_user, label="Codex (other laptop)")
    # Agent B holds a BYOK model only: no subscription lineage to re-bind on.
    byok = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"BYOK {uuid4()}",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "sk-byok-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/gpt-5",
                    "provider_adapter": "preloop",
                }
            },
        },
        account_id=test_user.account_id,
    )
    _replace(
        db_session, test_user, agent_b, [_onboard_binding(byok, alias="openai/gpt-5")]
    )
    service = _service(db_session, test_user, agent_b)

    with pytest.raises(ModelGatewayAPIError) as excinfo:
        service._resolve_requested_model(SIBLING_IDENTIFIER, provider="openai")

    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "model_not_authorized"
    # Agent A's binding is untouched.
    a_bindings = _all_bindings(db_session, test_user, agent_a)
    assert any(
        str(b.ai_model_id) == str(sibling_a.id) and b.status == "gateway_ready"
        for b in a_bindings
    )
    assert not any(
        str(b.ai_model_id) == str(sibling_a.id)
        for b in _all_bindings(db_session, test_user, agent_b)
    )


def test_other_agent_with_own_codex_lineage_gets_own_sibling(db_session, test_user):
    """Two Codex logins in one account: the second agent autoregisters, not 400."""
    _agent_a, _primary_a, sibling_a = _enrolled_with_sibling(db_session, test_user)
    agent_b = _make_agent(db_session, test_user, label="Codex (colleague)")
    primary_b = _make_codex_oauth_model(
        db_session,
        test_user,
        alias="openai/gpt-6-astra-b",
        managed_by="preloop agents onboard",
    )
    assert primary_b.credentials_secret_id != sibling_a.credentials_secret_id
    _replace(
        db_session,
        test_user,
        agent_b,
        [_onboard_binding(primary_b, alias="openai/gpt-6-astra-b")],
    )
    service = _service(db_session, test_user, agent_b)

    resolved = service._resolve_requested_model(SIBLING_IDENTIFIER, provider="openai")

    assert resolved.id != sibling_a.id
    assert resolved.model_identifier == SIBLING_IDENTIFIER
    assert resolved.credentials_secret_id == primary_b.credentials_secret_id
    assert any(
        str(b.ai_model_id) == str(resolved.id) and b.status == "gateway_ready"
        for b in _all_bindings(db_session, test_user, agent_b)
    )


def test_user_token_is_unaffected(db_session, test_user):
    """Console tokens already see everything; no binding is written for them."""
    agent, _primary, sibling = _enrolled_with_sibling(db_session, test_user)
    _strand_sibling(db_session, test_user, agent, sibling)
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="user-jwt", user=test_user)
    )

    resolved = service._resolve_requested_model(SIBLING_ALIAS, provider="openai")

    assert resolved.id == sibling.id
    assert all(
        b.status == "inactive"
        for b in _all_bindings(db_session, test_user, agent)
        if str(b.ai_model_id) == str(sibling.id)
    )


# --- ensure_active_binding ---------------------------------------------------


def test_ensure_active_binding_reactivates_existing_slot(db_session, test_user):
    agent, _primary, sibling = _enrolled_with_sibling(db_session, test_user)
    _strand_sibling(db_session, test_user, agent, sibling)

    row = crud_managed_agent_ai_model_binding.ensure_active_binding(
        db_session,
        account_id=str(test_user.account_id),
        agent_id=str(agent.id),
        ai_model_id=str(sibling.id),
        gateway_alias=SIBLING_ALIAS,
        config_key=f"{AUTOREGISTER_CONFIG_KEY_PREFIX}{SIBLING_IDENTIFIER}",
    )

    assert row.status == "gateway_ready"
    assert row.is_primary is False
    rows = [
        b
        for b in _all_bindings(db_session, test_user, agent)
        if str(b.ai_model_id) == str(sibling.id)
    ]
    assert len(rows) == 1


def test_ensure_active_binding_creates_when_missing(db_session, test_user):
    agent = _make_agent(db_session, test_user)
    model = _make_codex_oauth_model(
        db_session, test_user, alias=PRIMARY_ALIAS, managed_by="preloop agents onboard"
    )

    row = crud_managed_agent_ai_model_binding.ensure_active_binding(
        db_session,
        account_id=str(test_user.account_id),
        agent_id=str(agent.id),
        ai_model_id=str(model.id),
        gateway_alias=PRIMARY_ALIAS,
        config_key=f"{AUTOREGISTER_CONFIG_KEY_PREFIX}gpt-6-astra",
    )

    assert row.status == "gateway_ready"
    assert str(row.ai_model_id) == str(model.id)
    assert len(_all_bindings(db_session, test_user, agent)) == 1


def test_ensure_active_binding_is_idempotent(db_session, test_user):
    agent, _primary, sibling = _enrolled_with_sibling(db_session, test_user)
    kwargs = dict(
        account_id=str(test_user.account_id),
        agent_id=str(agent.id),
        ai_model_id=str(sibling.id),
        gateway_alias=SIBLING_ALIAS,
        config_key=f"{AUTOREGISTER_CONFIG_KEY_PREFIX}{SIBLING_IDENTIFIER}",
    )

    first = crud_managed_agent_ai_model_binding.ensure_active_binding(
        db_session, **kwargs
    )
    second = crud_managed_agent_ai_model_binding.ensure_active_binding(
        db_session, **kwargs
    )

    assert first.id == second.id
    assert (
        len(
            [
                b
                for b in _all_bindings(db_session, test_user, agent)
                if str(b.ai_model_id) == str(sibling.id)
            ]
        )
        == 1
    )
