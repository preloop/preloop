"""An account's own model row wins over a system row with the same alias.

Regression for a flow bound to the account's own key being served (and
metered) by an operator-paid system row that carried the same gateway alias.
"""

import logging
import uuid

from preloop.models import models
from preloop.models.crud import crud_ai_model, crud_api_key
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService

ALIAS = "acme/flash-model"


def _payload(name: str, *, alias: str = ALIAS, is_default: bool = True) -> dict:
    return {
        "name": name,
        "provider_name": "openai",
        "model_identifier": "flash-model",
        "api_key": "test-key",
        "is_default": is_default,
        "meta_data": {
            "gateway": {
                "enabled": True,
                "model_alias": alias,
                "provider_adapter": "preloop",
            }
        },
    }


def _system_row(db_session, **kwargs) -> models.AIModel:
    row = crud_ai_model.create_with_account(
        db=db_session, obj_in=_payload("System hosted", **kwargs), account_id=None
    )
    # A system default must not lose its flag to the account's own default.
    row.is_default = True
    db_session.flush()
    return row


def _own_row(db_session, account_id, name="Own key", **kwargs) -> models.AIModel:
    return crud_ai_model.create_with_account(
        db=db_session, obj_in=_payload(name, **kwargs), account_id=account_id
    )


def _service(db_session, test_user, context_data=None) -> OpenAIGatewayService:
    api_key = None
    if context_data is not None:
        api_key, _token = crud_api_key.create_runtime_key(
            db_session,
            name=f"Runtime {uuid.uuid4()}",
            account_id=test_user.account_id,
            user_id=test_user.id,
            context_data=context_data,
            commit=False,
        )
    return OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user, api_key=api_key)
    )


def _flow(db_session, account_id, ai_model_id=None) -> models.Flow:
    flow = models.Flow(
        account_id=account_id,
        name="Bound flow",
        prompt_template="Example",
        agent_config={},
        ai_model_id=ai_model_id,
    )
    db_session.add(flow)
    db_session.flush()
    return flow


def test_execution_key_resolves_alias_to_flow_bound_own_row(db_session, test_user):
    """Execution keys minted before this change carry only ``flow_id``."""
    _system_row(db_session)
    own = _own_row(db_session, test_user.account_id)
    flow = _flow(db_session, test_user.account_id, ai_model_id=own.id)

    service = _service(db_session, test_user, {"flow_id": str(flow.id)})
    resolved = service._resolve_requested_model(ALIAS, provider="openai")

    assert resolved.id == own.id
    assert service.flow_model_warning is None


def test_execution_key_without_flow_binding_prefers_own_row(db_session, test_user):
    _system_row(db_session)
    own = _own_row(db_session, test_user.account_id)
    flow = _flow(db_session, test_user.account_id)

    service = _service(db_session, test_user, {"flow_id": str(flow.id)})

    assert service._resolve_requested_model(ALIAS, provider="openai").id == own.id


def test_flow_bound_row_wins_alias_tie_even_when_it_is_the_system_row(
    db_session, test_user
):
    system = _system_row(db_session)
    _own_row(db_session, test_user.account_id)

    service = _service(db_session, test_user, {"ai_model_id": str(system.id)})

    assert service._resolve_requested_model(ALIAS, provider="openai").id == system.id


def test_agent_credential_resolves_alias_to_own_row(db_session, test_user):
    _system_row(db_session)
    own = _own_row(db_session, test_user.account_id)

    service = _service(db_session, test_user, {"managed_agent_id": str(uuid.uuid4())})
    resolved = service._resolve_requested_model(ALIAS, provider="openai")

    assert resolved.id == own.id
    assert service.alias_collision_warning is not None
    assert str(own.id) in service.alias_collision_warning


def test_user_token_resolves_bare_suffix_to_own_row(db_session, test_user):
    _system_row(db_session)
    own = _own_row(db_session, test_user.account_id)

    service = _service(db_session, test_user)

    assert service._resolve_requested_model("flash-model", provider="openai").id == (
        own.id
    )


def test_default_model_prefers_own_default_over_system_default(db_session, test_user):
    _system_row(db_session)
    own = _own_row(db_session, test_user.account_id)

    service = _service(db_session, test_user)

    assert service._resolve_requested_model(None, provider="openai").id == own.id


def test_flow_without_requested_model_routes_to_its_bound_row(db_session, test_user):
    _system_row(db_session)
    _own_row(db_session, test_user.account_id)
    other = _own_row(
        db_session,
        test_user.account_id,
        name="Other",
        alias="acme/other-model",
        is_default=False,
    )
    service = _service(db_session, test_user, {"ai_model_id": str(other.id)})

    assert service._resolve_requested_model(None, provider="openai").id == other.id


def test_flow_served_by_another_row_is_warned(db_session, test_user, caplog):
    _system_row(db_session)
    own = _own_row(db_session, test_user.account_id)
    other = _own_row(
        db_session,
        test_user.account_id,
        name="Other",
        alias="acme/other-model",
        is_default=False,
    )
    service = _service(db_session, test_user, {"ai_model_id": str(own.id)})

    with caplog.at_level(logging.WARNING):
        resolved = service._resolve_requested_model(
            "acme/other-model", provider="openai"
        )

    assert resolved.id == other.id
    assert service.flow_model_warning is not None
    assert str(own.id) in service.flow_model_warning
    assert "flow model mismatch" in (service.response_warning or "")
    assert any("gateway_flow_model_mismatch" in r.message for r in caplog.records)


def test_flow_from_another_account_is_ignored(db_session, test_user):
    other_account = models.Account(organization_name="Other account")
    db_session.add(other_account)
    db_session.flush()
    _system_row(db_session)
    own = _own_row(db_session, test_user.account_id)
    foreign_flow = _flow(db_session, other_account.id)

    service = _service(db_session, test_user, {"flow_id": str(foreign_flow.id)})

    assert service._flow_bound_model_id() is None
    assert service._resolve_requested_model(ALIAS, provider="openai").id == own.id


def test_runtime_token_records_flow_model_binding(db_session, test_user):
    from preloop.services.flow_runtime_token import create_flow_runtime_token

    own = _own_row(db_session, test_user.account_id)
    flow = _flow(db_session, test_user.account_id, ai_model_id=own.id)

    _token, api_key_id = create_flow_runtime_token(
        db_session, flow=flow, execution_id=uuid.uuid4()
    )

    api_key = crud_api_key.get(db_session, id=api_key_id)
    assert api_key.context_data["ai_model_id"] == str(own.id)


def test_system_row_taking_an_account_alias_is_logged(db_session, test_user, caplog):
    _own_row(db_session, test_user.account_id)

    with caplog.at_level(logging.WARNING):
        _system_row(db_session)

    assert any("gateway_system_alias_shadowed" in r.message for r in caplog.records)


def test_imported_own_row_still_beats_system_row(db_session, test_user):
    """Ownership outranks the user-created-over-import preference."""
    _system_row(db_session)
    payload = _payload("Imported own key")
    payload["meta_data"]["managed_by"] = "preloop agents onboard"
    own = crud_ai_model.create_with_account(
        db=db_session, obj_in=payload, account_id=test_user.account_id
    )

    service = _service(db_session, test_user)

    assert service._resolve_requested_model(ALIAS, provider="openai").id == own.id
