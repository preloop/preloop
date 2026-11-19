"""A schema-bound flow on an unpinned OpenRouter model saves with a warning."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import crud, models

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict"],
    "properties": {"verdict": {"type": "string"}},
}
CODE = "openrouter_unpinned_structured_result"


def _model(
    db: Session,
    user: models.User,
    provider: str,
    routing: dict[str, Any] | None = None,
    identifier: str = "example/model-flash",
) -> Any:
    return crud.crud_ai_model.create_with_account(
        db,
        obj_in={
            "name": f"{provider} model",
            "provider_name": provider,
            "model_identifier": identifier,
            "api_key": "synthetic-key",
            "meta_data": {"gateway": {"enabled": True}},
            "model_parameters": {"provider": routing} if routing else None,
        },
        account_id=user.account_id,
    )


def _body(model: Any, native: bool = True) -> dict[str, Any]:
    return {
        "name": "Native review",
        "prompt_template": "Review the change",
        "agent_type": "codex",
        "ai_model_id": str(model.id),
        "agent_config": {"native_result": native},
        "result_schema": SCHEMA,
    }


def test_unpinned_openrouter_native_flow_saves_with_a_warning(
    db_session: Session, test_user: models.User, client: TestClient
) -> None:
    model = _model(db_session, test_user, "openrouter")
    created = client.post("/api/v1/flows", json=_body(model))
    assert created.status_code == 200, created.text
    warnings = created.json()["config_warnings"]
    assert [warning["code"] for warning in warnings] == [CODE]
    assert warnings[0]["ai_model_id"] == str(model.id)
    assert "provider preference" in warnings[0]["message"]
    assert "require_parameters" not in warnings[0]["message"]
    assert warnings[0]["docs_url"].endswith(
        "#structured-results-and-upstream-providers"
    )
    flow_id = created.json()["id"]
    read = client.get(f"/api/v1/flows/{flow_id}")
    assert [warning["code"] for warning in read.json()["config_warnings"]] == [CODE]
    updated = client.put(f"/api/v1/flows/{flow_id}", json={"name": "Renamed review"})
    assert updated.status_code == 200, updated.text
    assert [w["code"] for w in updated.json()["config_warnings"]] == [CODE]


@pytest.mark.parametrize(
    ("provider", "routing", "native"),
    [
        ("openrouter", {"only": ["provider-a"]}, True),
        ("openrouter", {"order": ["provider-a"], "allow_fallbacks": False}, True),
        ("openrouter", None, False),
        ("openai", None, True),
    ],
)
def test_no_warning_when_pinned_not_native_or_not_openrouter(
    db_session: Session,
    test_user: models.User,
    client: TestClient,
    provider: str,
    routing: dict[str, Any] | None,
    native: bool,
) -> None:
    model = _model(db_session, test_user, provider, routing)
    created = client.post("/api/v1/flows", json=_body(model, native))
    assert created.status_code == 200, created.text
    assert created.json()["config_warnings"] == []


def test_order_with_fallbacks_still_warns(
    db_session: Session, test_user: models.User, client: TestClient
) -> None:
    model = _model(db_session, test_user, "openrouter", {"order": ["provider-a"]})
    created = client.post("/api/v1/flows", json=_body(model))
    assert created.status_code == 200, created.text
    assert [w["code"] for w in created.json()["config_warnings"]] == [CODE]


@pytest.mark.parametrize("kind", ["routing", "label"])
def test_routing_rule_model_is_checked_too(
    db_session: Session, test_user: models.User, client: TestClient, kind: str
) -> None:
    pinned = _model(db_session, test_user, "openrouter", {"only": ["provider-a"]})
    routed = _model(db_session, test_user, "openrouter", identifier="example/other")
    body = _body(pinned)
    if kind == "routing":
        body["agent_config"]["model_routing"] = {
            "rules": [
                {
                    "id": "review",
                    "labels": {"any": ["review"]},
                    "ai_model_id": str(routed.id),
                    "agent_type": "codex",
                }
            ]
        }
    else:
        body["agent_config"]["model_by_label"] = [
            {"label": "review", "ai_model_id": str(routed.id)}
        ]
    created = client.post("/api/v1/flows", json=body)
    assert created.status_code == 200, created.text
    warnings = created.json()["config_warnings"]
    assert [w["ai_model_id"] for w in warnings] == [str(routed.id)]


def test_get_by_ids_loads_models_in_one_lookup(
    db_session: Session, test_user: models.User
) -> None:
    first = _model(db_session, test_user, "openrouter")
    second = _model(db_session, test_user, "openai")
    found = crud.crud_ai_model.get_by_ids(
        db_session, [str(first.id), second.id, "not-a-uuid"]
    )
    assert set(found) == {str(first.id), str(second.id)}
