"""Regression coverage for credential reuse and structured-result failures."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.models.schemas.flow import FlowCreate, FlowUpdate
from preloop.services.flow_failure_category import derive_failure_category
from preloop.services.flow_result_schema import (
    ResultSchemaError,
    check_native_result_schema,
    uses_openai_strict_results,
)
from tests.services.test_flow_execution_limits import (
    _create_execution,
    _create_flow,
    _log_failure,
)


@pytest.mark.parametrize(
    "ownership",
    ["legacy", "owned", "foreign", "mixed", "unreferenced", "global", "owned_mixed"],
)
@pytest.mark.parametrize("update", [False, True])
def test_reuse_requires_unambiguous_account_ownership(
    db_session: Session, test_user: models.User, ownership: str, update: bool
) -> None:
    """Legacy reuse needs positive evidence and refuses mixed/global references."""
    account_id = test_user.account_id
    source = crud.crud_ai_model.create_with_account(
        db_session,
        obj_in={
            "name": "Source",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "synthetic-key",
        },
        account_id=account_id,
    )
    secret = source.credentials_secret
    if ownership not in {"owned", "owned_mixed"}:
        secret.account_id = None
    if ownership == "foreign":
        secret.account_id = crud.crud_account.create(
            db_session, obj_in={"organization_name": "Other", "is_active": True}
        ).id
    if ownership == "unreferenced":
        source.credentials_secret_id = None
    if ownership in {"mixed", "global", "owned_mixed"}:
        # Reproduce historical inconsistent rows without using the guarded CRUD write.
        other = models.AIModel(
            name="Other",
            provider_name="openai",
            model_identifier="gpt-5",
            account_id=None
            if ownership == "global"
            else crud.crud_account.create(
                db_session, obj_in={"organization_name": "Example", "is_active": True}
            ).id,
            credentials_secret_id=secret.id,
        )
        db_session.add(other)
    db_session.flush()
    with db_session.no_autoflush:
        if ownership in {"legacy", "owned"}:
            target = crud.crud_ai_model.create_with_account(
                db_session,
                obj_in={
                    "name": "Target",
                    "provider_name": "openai",
                    "model_identifier": "gpt-5-mini",
                    **(
                        {"api_key": "synthetic-previous"}
                        if update
                        else {"credentials_secret_id": secret.id}
                    ),
                },
                account_id=account_id,
                commit=False,
            )
            if update:
                target = crud.crud_ai_model.update(
                    db_session,
                    db_obj=target,
                    obj_in={"credentials_secret_id": secret.id},
                )
            assert target.credentials_secret_id == secret.id
        else:
            with pytest.raises(
                ValueError,
                match="no owning account"
                if ownership == "unreferenced"
                else "different account",
            ):
                crud.crud_ai_model._apply_secret_reference_fields(
                    db_session,
                    obj_data={"credentials_secret_id": secret.id},
                    account_id=account_id,
                    secret_name="Target",
                )


STRICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            },
        }
    },
}


@pytest.mark.parametrize("native", [True, False])
def test_schema_constraints_checked_on_create_and_partial_update(
    db_session: Session, test_user: models.User, native: bool
) -> None:
    """Native saves reject nested open objects; capture-only saves remain valid."""
    schema = deepcopy(STRICT_SCHEMA)
    del schema["properties"]["findings"]["items"]["additionalProperties"]
    model = crud.crud_ai_model.create_with_account(
        db_session,
        obj_in={
            "name": "Schema model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "synthetic-key",
        },
        account_id=test_user.account_id,
    )
    flow_in = FlowCreate(
        ai_model_id=model.id,
        name="Structured flow",
        prompt_template="Review",
        agent_type="codex",
        agent_config={"native_result": native},
        result_schema=schema,
        account_id=test_user.account_id,
    )
    if native:
        with pytest.raises(
            ResultSchemaError, match=r"findings.items.*additionalProperties"
        ):
            crud.crud_flow.create(
                db_session, flow_in=flow_in, account_id=test_user.account_id
            )
    else:
        flow = crud.crud_flow.create(
            db_session, flow_in=flow_in, account_id=test_user.account_id
        )
        with pytest.raises(ResultSchemaError, match="additionalProperties"):
            crud.crud_flow.update(
                db_session,
                db_obj=flow,
                flow_in=FlowUpdate(agent_config={"native_result": True}),
                account_id=test_user.account_id,
            )
        assert flow.agent_config == {"native_result": False}
    flow_in.result_schema = deepcopy(STRICT_SCHEMA)
    flow = crud.crud_flow.create(
        db_session, flow_in=flow_in, account_id=test_user.account_id
    )
    assert flow.result_schema == STRICT_SCHEMA
    renamed = crud.crud_flow.update(
        db_session,
        db_obj=flow,
        flow_in=FlowUpdate(name="Renamed"),
        account_id=test_user.account_id,
    )
    assert renamed.result_schema == STRICT_SCHEMA


@pytest.mark.parametrize(
    "message",
    [
        "Invalid schema for response_format 'result': In context=('properties', 'findings', 'items'), 'additionalProperties' is required to be supplied and to be false.",
        "Invalid schema for text.format 'result': required must contain every property",
    ],
)
def test_provider_schema_rejection_is_configuration(message: str) -> None:
    """A strict structured-output provider rejection never becomes unknown."""
    assert (
        derive_failure_category(status="FAILED", error_message=message)
        == "model_config"
    )
    assert (
        derive_failure_category(
            status="FAILED", error_message=message, failure_analysis={"transient": True}
        )
        == "model_config"
    )


def test_authentication_breaker_preserves_key_rejection(
    db_session: Session, test_user: models.User
) -> None:
    """The first failing run identifies the key rejection despite SDK retries."""
    from preloop.services.flow_execution_limits import (
        enforce_failure_streak_for_id,
        RepeatedUpstreamFailureError,
    )

    flow = _create_flow(db_session, test_user)
    execution = _create_execution(db_session, flow)
    for _ in range(2):
        _log_failure(
            db_session,
            test_user,
            flow,
            execution,
            status_code=401,
            error_class="upstream_auth",
        )
    db_session.flush()
    with pytest.raises(RepeatedUpstreamFailureError) as exc:
        enforce_failure_streak_for_id(db_session, execution_id=execution.id)
    db_session.refresh(execution)
    assert execution.failure_category == "model_auth"
    assert "key" in execution.error_message.lower()
    assert "rejected" in execution.error_message.lower()
    assert exc.value.category == "model_auth"
    assert (
        derive_failure_category(status="FAILED", error_message=execution.error_message)
        == "model_auth"
    )


@pytest.mark.parametrize("location", ["items", "$defs", "anyOf"])
def test_native_schema_checks_nested_schema_locations(location: str) -> None:
    """Objects hidden behind definitions or nullable unions need strict closure."""
    schema = deepcopy(STRICT_SCHEMA)
    child = schema["properties"]["findings"]["items"]
    del child["additionalProperties"]
    if location == "$defs":
        schema["$defs"] = {"finding": child}
        schema["properties"]["findings"]["items"] = {"$ref": "#/$defs/finding"}
    elif location == "anyOf":
        schema["properties"]["findings"]["items"] = {"anyOf": [child, {"type": "null"}]}
    with pytest.raises(ResultSchemaError, match="additionalProperties"):
        check_native_result_schema(schema)


@pytest.mark.parametrize(
    "change", ["required", "root_anyOf", "allOf", "external_ref", "missing_ref"]
)
def test_native_schema_rejects_strict_contract_violations(change: str) -> None:
    """Reject schemas the strict output API cannot accept before starting a run."""
    schema = deepcopy(STRICT_SCHEMA)
    if change == "required":
        schema["properties"]["findings"]["items"]["required"] = []
    elif change == "root_anyOf":
        schema["anyOf"] = [{"type": "object"}]
    elif change == "allOf":
        schema["allOf"] = [{"type": "object"}]
    else:
        schema["properties"]["findings"]["items"] = {
            "$ref": "https://example.com/schema"
            if change == "external_ref"
            else "#/$defs/missing"
        }
    with pytest.raises(ResultSchemaError):
        check_native_result_schema(schema)


def test_valid_recursive_schema_and_data_annotations_are_preserved() -> None:
    """Strict recursive schemas and arbitrary default data keep their semantics."""
    schema = {
        "$ref": "#/$defs/node",
        "$defs": {
            "node": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "child": {"anyOf": [{"$ref": "#/$defs/node"}, {"type": "null"}]}
                },
                "required": ["child"],
                "default": {"allOf": "ordinary data"},
            }
        },
    }
    original = deepcopy(schema)
    check_native_result_schema(schema)
    assert schema == original


@pytest.mark.parametrize(
    "provider,identifier,endpoint,strict",
    [
        ("openai", "gpt-5", None, True),
        ("azure", "gpt-5", "https://example.openai.azure.com", True),
        ("openrouter", "openai/gpt-5", None, True),
        ("deepseek", "deepseek-chat", None, False),
        ("openai", "deepseek-chat", "https://api.deepseek.com", False),
        ("google", "gemini-2.5-pro", None, False),
        ("openai", "local-model", "https://models.example.com", False),
    ],
)
def test_strict_schema_validation_follows_model_family(
    provider: str, identifier: str, endpoint: str | None, strict: bool
) -> None:
    """Other providers and JSON-mode fallbacks keep accepting their existing schemas."""
    assert (
        uses_openai_strict_results(
            SimpleNamespace(
                provider_name=provider,
                model_identifier=identifier,
                api_endpoint=endpoint,
            )
        )
        is strict
    )


def test_schema_save_http_errors_are_actionable(
    db_session: Session, test_user: models.User, client: Any
) -> None:
    """Create and merged partial-update errors reach clients as 422, not 500."""
    model = crud.crud_ai_model.create_with_account(
        db_session,
        obj_in={
            "name": "HTTP schema model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "synthetic-key",
        },
        account_id=test_user.account_id,
    )
    schema = deepcopy(STRICT_SCHEMA)
    del schema["properties"]["findings"]["items"]["additionalProperties"]
    body = {
        "name": "HTTP schema flow",
        "prompt_template": "Review",
        "agent_type": "codex",
        "ai_model_id": str(model.id),
        "agent_config": {"native_result": True},
        "result_schema": schema,
    }
    response = client.post("/api/v1/flows", json=body)
    assert response.status_code == 422, response.text
    assert "findings.items" in response.json()["detail"]
    body["agent_config"] = {"native_result": False}
    response = client.post("/api/v1/flows", json=body)
    assert response.status_code == 200, response.text
    flow_id = response.json()["id"]
    response = client.put(
        f"/api/v1/flows/{flow_id}", json={"agent_config": {"native_result": True}}
    )
    assert response.status_code == 422, response.text
    assert "additionalProperties" in response.json()["detail"]


@pytest.mark.parametrize("default_on", [False, True])
def test_native_validation_preserves_other_families_and_checks_model_changes(
    db_session: Session, test_user: models.User, default_on: bool
) -> None:
    """JSON mode works until a save selects an OpenAI strict-output model."""
    fallback = crud.crud_ai_model.create_with_account(
        db_session,
        obj_in={
            "name": "Fallback",
            "provider_name": "deepseek",
            "model_identifier": "deepseek-chat",
            "api_key": "synthetic-key",
        },
        account_id=test_user.account_id,
    )
    strict = crud.crud_ai_model.create_with_account(
        db_session,
        obj_in={
            "name": "Strict",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "synthetic-key",
        },
        account_id=test_user.account_id,
    )
    flow = crud.crud_flow.create(
        db_session,
        flow_in=FlowCreate(
            name="Family flow",
            prompt_template="Review",
            agent_type="codex",
            ai_model_id=fallback.id,
            agent_config={} if default_on else {"native_result": True},
            git_clone_config={
                "enabled": True,
                "publication_mode": "read_only",
                "repositories": [
                    {
                        "project_id": "00000000-0000-4000-8000-000000000001",
                        "tracker_id": "00000000-0000-4000-8000-000000000002",
                    }
                ],
            }
            if default_on
            else None,
            result_schema={"type": "object"},
        ),
        account_id=test_user.account_id,
    )
    with pytest.raises(ResultSchemaError, match="additionalProperties"):
        crud.crud_flow.update(
            db_session,
            db_obj=flow,
            flow_in=FlowUpdate(ai_model_id=strict.id),
            account_id=test_user.account_id,
        )
    assert flow.ai_model_id == fallback.id


@pytest.mark.parametrize(
    "selection", ["model_routing", "model_by_label", "retry_on_no_progress"]
)
def test_schema_checks_routed_and_retry_models(
    db_session: Session, test_user: models.User, selection: str
) -> None:
    """A strict model selected after the default has the same save-time checks."""
    model = crud.crud_ai_model.create_with_account(
        db_session,
        obj_in={
            "name": "Routed",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "synthetic-key",
        },
        account_id=test_user.account_id,
    )
    model_id = str(model.id)
    selections = {
        "model_routing": {
            "rules": [
                {
                    "id": "review",
                    "labels": {"any": ["bug"]},
                    "agent_type": "codex",
                    "ai_model_id": model_id,
                }
            ]
        },
        "model_by_label": [{"label": "bug", "ai_model_id": model_id}],
        "retry_on_no_progress": {"enabled": True, "ai_model_id": model_id},
    }
    with pytest.raises(ResultSchemaError, match="additionalProperties"):
        crud.crud_flow.create(
            db_session,
            flow_in=FlowCreate(
                name="Routed flow",
                prompt_template="Review",
                agent_type="codex",
                agent_config={"native_result": True, selection: selections[selection]},
                result_schema={"type": "object"},
            ),
            account_id=test_user.account_id,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"is_enabled": False},
        {"name": "Renamed legacy"},
        {"agent_config": {"native_result": True, "limits": {"max_turns": 10}}},
    ],
)
def test_legacy_native_flow_allows_unrelated_remediation(
    db_session: Session, test_user: models.User, change: dict[str, Any]
) -> None:
    """A previously saved schema cannot prevent disabling or maintaining a flow."""
    from preloop.models.crud.base import CRUDBase

    model = crud.crud_ai_model.create_with_account(
        db_session,
        obj_in={
            "name": "Legacy",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "synthetic-key",
        },
        account_id=test_user.account_id,
    )
    flow = crud.crud_flow.create(
        db_session,
        flow_in=FlowCreate(
            name="Legacy",
            prompt_template="Review",
            agent_type="codex",
            ai_model_id=model.id,
            agent_config={"native_result": False},
            result_schema={"type": "object"},
        ),
        account_id=test_user.account_id,
    )
    CRUDBase(models.Flow).update(
        db_session, db_obj=flow, obj_in={"agent_config": {"native_result": True}}
    )
    updated = crud.crud_flow.update(
        db_session,
        db_obj=flow,
        flow_in=FlowUpdate(**change),
        account_id=test_user.account_id,
    )
    assert updated.result_schema == {"type": "object"}
    for name, value in change.items():
        assert getattr(updated, name) == value
    if change.get("is_enabled") is False:
        with pytest.raises(ResultSchemaError):
            crud.crud_flow.update(
                db_session,
                db_obj=flow,
                flow_in=FlowUpdate(is_enabled=True),
                account_id=test_user.account_id,
            )


@pytest.mark.parametrize(
    "provider,identifier,endpoint",
    [
        ("custom", "gpt-4o", "https://models.example.com"),
        ("openai-compatible", "o3-mini", "https://models.example.com"),
        ("openai", "gpt-4o", "https://models.example.com"),
        ("custom", "openai/gpt-oss-120b", "https://models.example.com"),
        ("azure", "phi-4", "https://example.services.ai.azure.com"),
    ],
)
def test_custom_routes_do_not_inherit_openai_schema_constraints(
    provider: str, identifier: str, endpoint: str
) -> None:
    """OpenAI-shaped model aliases are insufficient evidence of a strict API."""
    assert not uses_openai_strict_results(
        SimpleNamespace(
            provider_name=provider, model_identifier=identifier, api_endpoint=endpoint
        )
    )


@pytest.mark.parametrize(
    "keyword", ["prefixItems", "contains", "unevaluatedProperties", "unevaluatedItems"]
)
def test_strict_schema_rejects_unsupported_schema_locations(keyword: str) -> None:
    """Unsupported schema keywords must not bypass validation of nested objects."""
    schema = deepcopy(STRICT_SCHEMA)
    child = {"type": "object"}
    schema["properties"]["findings"][keyword] = (
        [child] if keyword == "prefixItems" else child
    )
    with pytest.raises(ResultSchemaError, match=keyword):
        check_native_result_schema(schema)


def test_schema_reference_can_address_an_array_branch() -> None:
    """JSON Pointer array indices resolve just like mapping keys."""
    schema = deepcopy(STRICT_SCHEMA)
    schema["properties"]["findings"]["items"] = {
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }
    schema["properties"]["tag"] = {"$ref": "#/properties/findings/items/anyOf/0"}
    schema["required"].append("tag")
    check_native_result_schema(schema)


def test_source_review_preset_enables_strict_validation(
    db_session: Session, test_user: models.User
) -> None:
    """A cloned review preset defaults native output on without read-only config."""
    from preloop.flow_presets import PRESET_SLUGS

    preset = crud.crud_flow.create(
        db_session,
        flow_in=FlowCreate(
            name=PRESET_SLUGS["pull-request-reviewer"],
            prompt_template="Review",
            agent_type="codex",
            agent_config={},
            is_preset=True,
        ),
        account_id=test_user.account_id,
    )
    model = crud.crud_ai_model.create_with_account(
        db_session,
        obj_in={
            "name": "Preset model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "synthetic-key",
        },
        account_id=test_user.account_id,
    )
    with pytest.raises(ResultSchemaError, match="additionalProperties"):
        crud.crud_flow.create(
            db_session,
            flow_in=FlowCreate(
                name="Cloned",
                prompt_template="Review",
                agent_type="codex",
                agent_config={},
                source_preset_id=preset.id,
                ai_model_id=model.id,
                result_schema={"type": "object"},
            ),
            account_id=test_user.account_id,
        )


def test_overlong_array_reference_returns_schema_validation_error() -> None:
    """An index beyond Python's integer limit stays an actionable schema error."""
    schema = deepcopy(STRICT_SCHEMA)
    schema["properties"]["findings"]["items"] = {
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }
    schema["properties"]["tag"] = {
        "$ref": "#/properties/findings/items/anyOf/" + "9" * 5000
    }
    schema["required"].append("tag")
    with pytest.raises(ResultSchemaError, match="unresolved reference"):
        check_native_result_schema(schema)
