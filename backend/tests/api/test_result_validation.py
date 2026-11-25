"""The wrapper receives the controller's verdict for every accepted schema."""

import inspect
import json
import os
import subprocess
import sys
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.agents.codex import CodexAgent
from preloop.api.endpoints.result_validation import MAX_REQUEST_BYTES, router
from preloop.config import settings
from preloop.models.schemas.flow import FlowCreate, FlowUpdate
from preloop.services import native_answer, native_result_repair
from preloop.services.flow_artifacts import sanitize_captured_result
from preloop.services.flow_result_schema import validate_result_document
from preloop.services.result_validation_capability import (
    AUDIENCE,
    VALIDATION_PATH,
    result_validation_environment,
    schema_digest,
)
from tests.agents.result_validation_server import validation_server
from tests.agents.test_native_result_repair import CONDITIONAL_SCHEMA, review

FEATURES = [
    ("required", {"required": ["x"]}, {"x": 1}, {}),
    ("enum", {"properties": {"x": {"enum": [1]}}}, {"x": 1}, {"x": True}),
    ("type", {"properties": {"x": {"type": "integer"}}}, {"x": 1}, {"x": True}),
    (
        "const",
        {"properties": {"x": {"const": {"y": True}}}},
        {"x": {"y": True}},
        {"x": {"y": 1}},
    ),
    (
        "allOf",
        {"allOf": [{"required": ["x"]}, {"required": ["y"]}]},
        {"x": 1, "y": 2},
        {"x": 1},
    ),
    ("anyOf", {"anyOf": [{"required": ["x"]}, {"required": ["y"]}]}, {"x": 1}, {}),
    (
        "oneOf",
        {"oneOf": [{"required": ["x"]}, {"required": ["y"]}]},
        {"x": 1},
        {"x": 1, "y": 2},
    ),
    ("not", {"not": {"required": ["x"]}}, {}, {"x": 1}),
    ("if-then-else", CONDITIONAL_SCHEMA, review(success=False), review(success=True)),
    (
        "then",
        CONDITIONAL_SCHEMA,
        review(verdict="pass", success=True),
        review(verdict="pass", success=False),
    ),
    ("additionalProperties", {"additionalProperties": False}, {}, {"x": 1}),
    (
        "nested-items",
        {"properties": {"x": {"items": {"items": {"type": "integer"}}}}},
        {"x": [[1]]},
        {"x": [["a"]]},
    ),
    ("boolean-schema", {"properties": {"x": False}}, {}, {"x": 1}),
    (
        "dependentRequired",
        {"dependentRequired": {"x": ["y"]}},
        {"x": 1, "y": 2},
        {"x": 1},
    ),
    (
        "dependentSchemas",
        {"dependentSchemas": {"x": {"required": ["y"]}}},
        {"x": 1, "y": 2},
        {"x": 1},
    ),
    (
        "unevaluatedProperties",
        {"allOf": [{"properties": {"x": {}}}], "unevaluatedProperties": False},
        {"x": 1},
        {"y": 1},
    ),
    (
        "patternProperties",
        {
            "patternProperties": {"^x": {"type": "integer"}},
            "additionalProperties": False,
        },
        {"xyz": 1},
        {"xyz": "a"},
    ),
    ("propertyNames", {"propertyNames": {"minLength": 2}}, {"xx": 1}, {"x": 1}),
    (
        "prefixItems",
        {"properties": {"x": {"prefixItems": [{"type": "string"}], "items": False}}},
        {"x": ["a"]},
        {"x": ["a", "b"]},
    ),
    (
        "contains",
        {
            "properties": {
                "x": {
                    "contains": {"type": "integer"},
                    "minContains": 2,
                    "maxContains": 2,
                }
            }
        },
        {"x": [1, 2, "a"]},
        {"x": [1, "a"]},
    ),
    (
        "unevaluatedItems",
        {
            "properties": {
                "x": {"prefixItems": [{"type": "string"}], "unevaluatedItems": False}
            }
        },
        {"x": ["a"]},
        {"x": ["a", 1]},
    ),
    (
        "ref-anchor",
        {
            "$defs": {"value": {"$anchor": "value", "type": "integer"}},
            "properties": {"x": {"$ref": "#value"}},
        },
        {"x": 1},
        {"x": "a"},
    ),
    (
        "numeric",
        {"properties": {"x": {"minimum": 1, "exclusiveMaximum": 3, "multipleOf": 0.5}}},
        {"x": 1.5},
        {"x": 3},
    ),
    (
        "string",
        {"properties": {"x": {"minLength": 2, "maxLength": 4, "pattern": "^a"}}},
        {"x": "abc"},
        {"x": "b"},
    ),
    (
        "array",
        {"properties": {"x": {"minItems": 1, "maxItems": 2, "uniqueItems": True}}},
        {"x": [True, 1]},
        {"x": [1, 1]},
    ),
    ("object", {"minProperties": 1, "maxProperties": 2}, {"x": 1}, {}),
]


@pytest.mark.parametrize(
    "feature,schema,valid,invalid", FEATURES, ids=[f[0] for f in FEATURES]
)
def test_standalone_wrapper_matches_controller(
    feature: str, schema: dict[str, Any], valid: dict[str, Any], invalid: dict[str, Any]
) -> None:
    """Exercise the stdlib client with site-packages disabled, over loopback."""
    source = inspect.getsource(native_answer) + inspect.getsource(native_result_repair)
    source += "\nimport sys\nrequest = json.load(sys.stdin)\n"
    source += "print(json.dumps(controller_validation(**request)))\n"
    with validation_server(schema) as env:
        for document, expected in ((valid, True), (invalid, False)):
            controller = validate_result_document(document, schema)
            assert controller["valid"] is expected, feature
            proc = subprocess.run(
                [sys.executable, "-S", "-c", source],
                input=json.dumps({"document": document, "schema": schema}),
                text=True,
                capture_output=True,
                check=True,
                env={**os.environ, **env, "PRELOOP_DISABLE_TELEMETRY": "true"},
            )
            assert json.loads(proc.stdout) == controller


def test_flow_save_retains_keywords_previously_skipped_by_repair() -> None:
    """Accepted Draft 2020-12 constraints use the controller at repair time too."""
    schema = {"type": "object", "dependentRequired": {"x": ["y"]}}
    created = FlowCreate(
        name="Review",
        prompt_template="Review",
        agent_type="codex",
        agent_config={},
        result_schema=schema,
    )
    assert created.result_schema == schema
    assert FlowUpdate(result_schema=schema).result_schema == schema


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _headers(schema: dict[str, Any]) -> dict[str, str]:
    return {
        "X-Preloop-Result-Capability": result_validation_environment(schema)[
            "PRELOOP_RESULT_VALIDATION_CAPABILITY"
        ]
    }


@pytest.mark.parametrize("kind", ["missing", "tampered", "expired", "wrong-audience"])
def test_validation_requires_the_narrow_capability(
    client: TestClient, kind: str
) -> None:
    claims = {"aud": AUDIENCE, "exp": 4_000_000_000, "schema_digest": schema_digest({})}
    if kind == "expired":
        claims["exp"] = 1
    if kind == "wrong-audience":
        claims["aud"] = "other"
    token = jwt.encode(
        claims,
        "wrong-key" * 4 if kind == "tampered" else settings.security.secret_key,
        algorithm="HS256",
    )
    response = client.post(
        VALIDATION_PATH,
        json={"schema": {}, "document": {}},
        headers={"X-Preloop-Result-Capability": "" if kind == "missing" else token},
    )
    assert response.status_code == 401


def test_capability_cannot_validate_another_schema(client: TestClient) -> None:
    response = client.post(
        VALIDATION_PATH,
        json={"schema": {"required": ["x"]}, "document": {}},
        headers=_headers({}),
    )
    assert response.status_code == 403


def test_validation_is_bounded_and_not_cached(client: TestClient) -> None:
    headers = _headers({})
    assert (
        client.post(
            VALIDATION_PATH, content=b"x" * (MAX_REQUEST_BYTES + 1), headers=headers
        ).status_code
        == 413
    )
    assert (
        client.post(VALIDATION_PATH, content=b"not JSON", headers=headers).status_code
        == 422
    )
    response = client.post(
        VALIDATION_PATH, json={"schema": {}, "document": {}}, headers=headers
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"valid": True, "errors": [], "error_count": 0}


def test_repair_uses_capture_sanitization(client: TestClient) -> None:
    schema = {"required": ["trusted_publication"]}
    document = {"trusted_publication": {"success": True}}
    response = client.post(
        VALIDATION_PATH,
        json={"schema": schema, "document": document},
        headers=_headers(schema),
    )
    assert response.json() == validate_result_document(
        sanitize_captured_result(document), schema
    )
    assert response.json()["valid"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kubernetes", [False, True])
async def test_validation_transport_is_set_for_native_runs_without_mcp_tools(
    monkeypatch: pytest.MonkeyPatch, kubernetes: bool
) -> None:
    monkeypatch.setenv("PRELOOP_EXECUTION_API_URL", "http://controller.example.com")
    agent = CodexAgent({})
    agent.use_kubernetes = kubernetes
    env = await agent._prepare_environment(
        {"agent_config": {"native_result": True}, "result_schema": CONDITIONAL_SCHEMA}
    )
    assert env["PRELOOP_RESULT_VALIDATION_URL"] == (
        "http://controller.example.com" + VALIDATION_PATH
    )
    claims = jwt.decode(
        env["PRELOOP_RESULT_VALIDATION_CAPABILITY"],
        settings.security.secret_key,
        algorithms=["HS256"],
        audience=AUDIENCE,
    )
    assert claims["schema_digest"] == schema_digest(CONDITIONAL_SCHEMA)
