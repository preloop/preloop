"""Result schemas a strict structured-output route refuses are refused at save.

A native-result flow routed to the Codex backend of a ChatGPT plan
(``openai-codex``) reached the provider with a ``oneOf`` schema and failed
at run time. The save-time check now covers that route, its 422 names the
path, the keyword and what to use instead, and a run that still reaches the
provider with such a schema fails with the same words.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from preloop.services.flow_result_schema import (
    STRICT_OUTPUT_UNSUPPORTED,
    ResultSchemaError,
    check_native_result_schema,
    explain_provider_schema_rejection,
    uses_openai_strict_results,
)

ACCEPTED = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "detail"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "detail": {
            "anyOf": [
                {"type": "string"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["line"],
                    "properties": {"line": {"type": "integer"}},
                },
            ]
        },
    },
}
REFUSED = deepcopy(ACCEPTED)
REFUSED["properties"]["detail"] = {"oneOf": ACCEPTED["properties"]["detail"]["anyOf"]}
PROVIDER_400 = (
    'unexpected status 400 Bad Request: {"error": {"message": "Invalid '
    "schema for response_format 'codex_output_schema': In context=(), 'oneOf' "
    'is not permitted.", "type": "invalid_request_error"}}'
)


@pytest.mark.parametrize(
    ("provider", "identifier", "endpoint", "strict"),
    [
        ("openai-codex", "gpt-5-codex", None, True),
        ("openai-codex", "gpt-5.4", "https://chatgpt.com/backend-api/codex", True),
        ("OpenAI-Codex", "codex-mini", None, True),
        ("openai", "gpt-5", None, True),
        # Routes whose limits we do not know keep every schema.
        ("anthropic", "claude-sonnet", None, False),
        ("mistral", "mistral-large", None, False),
        ("openai-compatible", "gpt-5", "https://models.example.com", False),
    ],
)
def test_strict_routes_include_the_codex_backend(
    provider: str, identifier: str, endpoint: str | None, strict: bool
) -> None:
    model = SimpleNamespace(
        provider_name=provider, model_identifier=identifier, api_endpoint=endpoint
    )
    assert uses_openai_strict_results(model) is strict


@pytest.mark.parametrize("keyword", sorted(STRICT_OUTPUT_UNSUPPORTED))
def test_every_refused_keyword_names_the_path_and_the_replacement(
    keyword: str,
) -> None:
    schema = deepcopy(ACCEPTED)
    schema["properties"]["verdict"][keyword] = (
        [{"type": "string"}] if keyword in {"oneOf", "allOf", "prefixItems"} else {}
    )
    with pytest.raises(ResultSchemaError) as caught:
        check_native_result_schema(schema)
    message = str(caught.value)
    assert message.startswith("result_schema at $.properties.verdict: ")
    assert f"{keyword} is unsupported" in message
    assert message.endswith(STRICT_OUTPUT_UNSUPPORTED[keyword])


def test_the_reported_schema_is_refused_with_anyof_as_the_fix() -> None:
    with pytest.raises(ResultSchemaError) as caught:
        check_native_result_schema(REFUSED)
    assert str(caught.value) == (
        "result_schema at $.properties.detail: oneOf is unsupported in native "
        "results; use anyOf with the same branches"
    )
    check_native_result_schema(ACCEPTED)


def _model(db: Session, user: models.User, provider: str, identifier: str) -> Any:
    return crud.crud_ai_model.create_with_account(
        db,
        obj_in={
            "name": f"{provider} model",
            "provider_name": provider,
            "model_identifier": identifier,
            "api_key": "synthetic-key",
            # Codex reaches these providers through the Responses gateway.
            "meta_data": {"gateway": {"enabled": True}},
        },
        account_id=user.account_id,
    )


def _body(model: Any, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "Native review",
        "prompt_template": "Review the change",
        "agent_type": "codex",
        "ai_model_id": str(model.id),
        "agent_config": {"native_result": True},
        "result_schema": schema,
    }


def test_codex_route_refuses_on_create_and_update_and_accepts_the_fix(
    db_session: Session, test_user: models.User, client: TestClient
) -> None:
    model = _model(db_session, test_user, "openai-codex", "gpt-5-codex")
    refused = client.post("/api/v1/flows", json=_body(model, REFUSED))
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    assert "$.properties.detail" in detail and "oneOf" in detail
    assert "use anyOf" in detail
    created = client.post("/api/v1/flows", json=_body(model, ACCEPTED))
    assert created.status_code == 200, created.text
    flow_id = created.json()["id"]
    updated = client.put(f"/api/v1/flows/{flow_id}", json={"result_schema": REFUSED})
    assert updated.status_code == 422, updated.text
    assert "use anyOf" in updated.json()["detail"]


@pytest.mark.parametrize(
    ("provider", "identifier"),
    [("anthropic", "claude-sonnet"), ("mistral", "mistral-large")],
)
def test_route_with_unknown_limits_accepts_the_schema(
    db_session: Session,
    test_user: models.User,
    client: TestClient,
    provider: str,
    identifier: str,
) -> None:
    model = _model(db_session, test_user, provider, identifier)
    created = client.post("/api/v1/flows", json=_body(model, REFUSED))
    assert created.status_code == 200, created.text


def test_stored_flow_with_a_refused_schema_still_loads(
    db_session: Session, test_user: models.User, client: TestClient
) -> None:
    model = _model(db_session, test_user, "openai-codex", "gpt-5-codex")
    # Saved before the route was checked: written past the save-time check.
    stored = CRUDBase(models.Flow).create(
        db_session,
        obj_in={
            "name": "Stored native review",
            "prompt_template": "Review the change",
            "agent_type": "codex",
            "ai_model_id": model.id,
            "agent_config": {"native_result": True},
            "account_id": test_user.account_id,
            "result_schema": REFUSED,
            "is_enabled": True,
        },
    )
    assert crud.crud_flow.get(db_session, id=str(stored.id)) is not None
    read = client.get(f"/api/v1/flows/{stored.id}")
    assert read.status_code == 200, read.text
    assert read.json()["result_schema"] == REFUSED
    listed = client.get("/api/v1/flows")
    assert listed.status_code == 200, listed.text


def test_run_time_refusal_says_what_the_save_says() -> None:
    with pytest.raises(ResultSchemaError) as saved:
        check_native_result_schema(REFUSED)
    explained = explain_provider_schema_rejection(PROVIDER_400, REFUSED)
    assert explained is not None
    assert str(saved.value) in explained
    assert "Provider response: " in explained and "'oneOf'" in explained
    # Other failures, and schemas the check accepts, keep their own text.
    assert explain_provider_schema_rejection("rate limited", REFUSED) is None
    assert explain_provider_schema_rejection(PROVIDER_400, ACCEPTED) is None


def test_orchestrator_stores_the_save_time_wording_on_the_run() -> None:
    from preloop.services.flow_failure_category import derive_failure_category
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    orchestrator = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
    orchestrator.flow = SimpleNamespace(result_schema=REFUSED)
    result = orchestrator._explain_schema_rejection(
        {"status": "FAILED", "error_message": PROVIDER_400}
    )
    assert result["error_message"].startswith(
        "The model provider refused the result schema: result_schema at "
        "$.properties.detail: oneOf is unsupported in native results; "
        "use anyOf with the same branches."
    )
    assert (
        derive_failure_category(status="FAILED", error_message=result["error_message"])
        == "model_config"
    )
