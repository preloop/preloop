"""Grant configuration and account-aware import validation use synthetic data."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from preloop.models.crud import crud_approval_workflow, crud_mcp_server
from preloop.models.schemas.grant_introspection import IntrospectionConfig
from preloop.models.schemas.mcp_server import (
    MCPServerCreate,
    merge_auth_config,
    redact_auth_config,
    redact_snapshot_credentials,
)
from preloop.services.policy import PolicyDocument
from preloop.services.policy.loader import PolicyApplier
from preloop.services.policy.schema import references_grant
from preloop.utils.redaction import REDACTED_STRING


def introspection() -> dict:
    return {
        "endpoint": "https://as.example.com/introspect",
        "client_id": "firewall",
        "client_secret": "synthetic-secret",
    }


@pytest.mark.parametrize(
    "override",
    [
        {"endpoint": "http://as.example.com/introspect"},
        {"endpoint": "https://user:password@as.example.com/introspect"},
        {"timeout_seconds": 31},
        {"max_cache_ttl_seconds": 301},
        {"negative_cache_ttl_seconds": 61},
        {"required_scopes": ["read write"]},
        {"client_auth": "none"},
        {"client_secret": ""},
    ],
)
def test_invalid_introspection_configuration_rejected(override: dict) -> None:
    with pytest.raises(ValidationError):
        IntrospectionConfig.model_validate({**introspection(), **override})


def test_nested_secret_redaction_and_write_only_merge() -> None:
    auth = {"token": "synthetic-bearer", "introspection": introspection()}
    public = redact_auth_config(auth)
    assert public["introspection"]["client_secret"] == REDACTED_STRING
    assert public["introspection"]["client_id"] == "firewall"
    assert public["introspection"]["endpoint"] == "https://as.example.com/introspect"
    assert merge_auth_config(public, auth) == auth
    assert "client_secret" not in merge_auth_config(public, None)["introspection"]
    snapshot = redact_snapshot_credentials({"mcp_servers": [{"auth_config": auth}]})
    assert snapshot["mcp_servers"][0]["auth_config"] == public
    assert auth["introspection"]["client_secret"] == "synthetic-secret"
    MCPServerCreate(
        name="protected",
        url="https://mcp.example.com/mcp",
        auth_type="bearer",
        auth_config=auth,
    )
    with pytest.raises(ValidationError, match="bearer or oauth"):
        MCPServerCreate(
            name="protected",
            url="https://mcp.example.com/mcp",
            auth_type="none",
            auth_config=auth,
        )


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("grant.active == true", True),
        ("grant['active'] == true", True),
        ("has(grant.scope)", True),
        ('args.label == "grant.active"', False),
        ("args.grant.active == true", False),
        ("subject.grant.consent == 'x'", False),
    ],
)
def test_binding_reference_ignores_quoted_literals(
    expression: str, expected: bool
) -> None:
    assert references_grant(expression) is expected


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("declared", [False, True])
def test_grant_rule_import_requires_configured_referenced_server(
    monkeypatch, configured: bool, declared: bool
) -> None:
    auth = (
        {"token": "synthetic-bearer", "introspection": introspection()}
        if configured
        else {"token": "synthetic-bearer"}
    )
    monkeypatch.setattr(
        crud_mcp_server,
        "get_active_by_account",
        lambda *a, **k: []
        if declared
        else [SimpleNamespace(name="protected", auth_config=auth)],
    )
    monkeypatch.setattr(
        crud_approval_workflow, "get_names_by_account", lambda *a, **k: set()
    )
    policy = PolicyDocument.model_validate(
        {
            "version": "1.0",
            "metadata": {"name": "Synthetic grant policy"},
            "mcp_servers": [
                {
                    "name": "protected",
                    "url": "https://mcp.example.com/mcp",
                    "auth_type": "bearer",
                    "auth_config": auth,
                }
            ]
            if declared
            else [],
            "tools": [
                {
                    "name": "read_record",
                    "source": "protected",
                    "conditions": [
                        {"expression": "grant.active == true", "action": "allow"}
                    ],
                }
            ],
        }
    )
    errors = PolicyApplier(MagicMock(), uuid4())._validate_references(policy)
    assert bool(errors) is not configured
    if errors:
        assert "introspection configuration" in errors[0]
        assert "synthetic-secret" not in errors[0]


@pytest.mark.parametrize(
    "condition_type,expression",
    [
        ("simple", "grant.active == true"),
        (
            "cel",
            'grant.active && "read" in grant.scope && grant.sub == "subject-example"',
        ),
    ],
)
def test_existing_evaluators_resolve_grant_bindings(
    condition_type: str, expression: str
) -> None:
    from preloop.services.policy_evaluator import (
        EXTRA_BINDINGS_KEY,
        _evaluate_rule_condition,
    )

    assert _evaluate_rule_condition(
        expression=expression,
        condition_type=condition_type,
        tool_args={},
        context={
            EXTRA_BINDINGS_KEY: {
                "grant": {"active": True, "scope": ["read"], "sub": "subject-example"}
            }
        },
    )


def test_documented_grant_policy_validates_and_imports(monkeypatch) -> None:
    """The published example configures the same grant provider its rule uses."""
    page = Path(__file__).resolve().parents[3] / "docs/guide/grant-introspection.md"
    text = page.read_text()
    example = text.split("```yaml\n", 1)[1].split("```", 1)[0]
    policy = PolicyDocument.model_validate(yaml.safe_load(example))
    monkeypatch.setattr(crud_mcp_server, "get_active_by_account", lambda *a, **k: [])
    monkeypatch.setattr(
        crud_approval_workflow, "get_names_by_account", lambda *a, **k: set()
    )
    assert PolicyApplier(MagicMock(), uuid4())._validate_references(policy) == []
    config = policy.mcp_servers[0].auth_config["introspection"]
    assert config["fail_open"] is False
    assert config["required_scopes"] == ["records:read"]
    assert policy.tools[0].conditions[0].condition_type == "cel"
