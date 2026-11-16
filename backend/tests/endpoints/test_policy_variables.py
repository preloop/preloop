"""Policy variable references remain literal through API operations."""

import json
import os
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_audit_log,
    crud_mcp_server,
    crud_tool_configuration,
)
from preloop.services.policy.loader import PolicyApplier
from preloop.services.policy.schema import PolicyDocument
from preloop.services.policy_version_service import PolicyVersionService

REFERENCES = "${PATH}/${PRELOOP_POLICY_TEST_VALUE}"
VALUES = ("test-process-path-value", "test-process-setting-value")


@pytest.fixture(autouse=True)
def process_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use synthetic process values for both variable references."""
    monkeypatch.setenv("PATH", VALUES[0])
    monkeypatch.setenv("PRELOOP_POLICY_TEST_VALUE", VALUES[1])


def policy_data() -> dict[str, Any]:
    """Build a policy with references in readable fields and nested values."""
    return {
        "version": "1.0",
        "metadata": {"name": REFERENCES},
        "mcp_servers": [
            {
                "name": REFERENCES,
                "url": "https://example.com/" + REFERENCES,
                "auth_type": "bearer",
                "auth_config": {"token": REFERENCES},
            }
        ],
        "tools": [
            {
                "name": REFERENCES,
                "source": REFERENCES,
                "conditions": [
                    {
                        "expression": 'args.value == "' + REFERENCES + '"',
                        "action": "deny",
                    }
                ],
            }
        ],
    }


def assert_literal(value: str) -> None:
    """Require both references and exclude both process values."""
    assert REFERENCES in value
    for resolved in VALUES:
        assert resolved not in value


@pytest.mark.parametrize("resolve_env", [None, "true", "false"])
@pytest.mark.parametrize("file_format", ["yaml", "json"])
def test_upload_read_paths(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    resolve_env: str | None,
    file_format: str,
    mocker: MockerFixture,
) -> None:
    """Upload, read, diff, audit and restore preserve the submitted references."""
    content = (
        json.dumps(policy_data())
        if file_format == "json"
        else yaml.safe_dump(policy_data())
    )
    fields = {} if resolve_env is None else {"resolve_env": resolve_env}
    env_get = mocker.spy(os.environ, "get")
    upload = client.post(
        "/api/v1/policies/upload",
        data=fields,
        files={"file": ("policy." + file_format, content)},
    )
    assert upload.status_code == 200, upload.text
    assert not {"PATH", "PRELOOP_POLICY_TEST_VALUE"} & {
        call.args[0] for call in env_get.call_args_list
    }
    assert_literal(upload.text)

    for export_format in ("json", "yaml"):
        exported = client.get(
            "/api/v1/policies/export", params={"format": export_format}
        )
        assert exported.status_code == 200, exported.text
        assert_literal(exported.text)

    servers = crud_mcp_server.get_multi_by_account(
        db_session, account_id=str(test_user.account_id)
    )
    assert len(servers) == 1
    assert_literal(json.dumps(servers[0].auth_config))
    server = client.get(f"/api/v1/mcp-servers/{servers[0].id}")
    assert server.status_code == 200, server.text
    assert_literal(server.text)

    configs = crud_tool_configuration.get_multi_by_account(
        db_session, account_id=str(test_user.account_id)
    )
    assert len(configs) == 1
    tool = client.get(f"/api/v1/tool-configurations/{configs[0].id}")
    assert tool.status_code == 200, tool.text
    assert_literal(tool.text)

    rows = crud_audit_log.get_by_account(
        db_session,
        account_id=test_user.account_id,
        action="configuration_change",
        resource_id="policy",
    )
    assert len(rows) == 1
    assert_literal(json.dumps(rows[0].details))

    changed = policy_data()
    changed["mcp_servers"][0]["url"] = "https://example.com/updated"
    diff = client.post(
        "/api/v1/policies/diff",
        files={"file": ("changed.json", json.dumps(changed))},
    )
    assert diff.status_code == 200, diff.text
    assert_literal(diff.text)

    service = PolicyVersionService(db_session, str(test_user.account_id))
    snapshot = service.create_snapshot()
    assert_literal(json.dumps(snapshot.snapshot_data))
    _, success, error = service.rollback_to_snapshot(snapshot.id)
    assert success, error
    assert_literal(client.get("/api/v1/policies/export").text)
    assert any("unresolved" in warning.lower() for warning in upload.json()["warnings"])


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("resolve_env", [None, False, True])
def test_applier_keeps_references(
    mocker: MockerFixture, dry_run: bool, resolve_env: bool | None
) -> None:
    """The shared applier never reads process variables, including dry runs."""
    applier = PolicyApplier(MagicMock(), "11111111-1111-4111-8111-111111111111")
    mocker.patch.object(applier, "_validate_references", return_value=[])
    mocker.patch.object(applier, "_prepare_approval_workflows")
    apply_servers = mocker.patch.object(applier, "_apply_mcp_servers")
    mocker.patch.object(applier, "_apply_tools")
    options = {} if resolve_env is None else {"resolve_env": resolve_env}
    result = applier.apply(
        PolicyDocument.model_validate(policy_data()),
        dry_run=dry_run,
        **options,
    )
    assert result.success, result.errors
    server = apply_servers.call_args.args[0][0]
    assert_literal(server.model_dump_json())


@pytest.mark.parametrize("dry_run", ["true", "false"])
def test_upload_reference_error(client: TestClient, dry_run: str) -> None:
    """Reference-validation diagnostics use the submitted names."""
    data = policy_data()
    data.pop("mcp_servers")
    response = client.post(
        "/api/v1/policies/upload",
        data={"dry_run": dry_run, "resolve_env": "true"},
        files={"file": ("policy.json", json.dumps(data))},
    )
    assert response.status_code == 422, response.text
    assert_literal(response.text)
    assert response.json()["detail"]["warnings"]


@pytest.mark.parametrize("dry_run", ["true", "false"])
def test_upload_schema_error(client: TestClient, dry_run: str) -> None:
    """Schema diagnostics do not transform variable references."""
    data = policy_data()
    data["mcp_servers"][0]["transport"] = REFERENCES
    response = client.post(
        "/api/v1/policies/upload",
        data={"dry_run": dry_run, "resolve_env": "true"},
        files={"file": ("policy.json", json.dumps(data))},
    )
    assert response.status_code == 400, response.text
    for value in VALUES:
        assert value not in response.text
    assert response.json()["detail"]["warnings"]


def test_upload_dry_run(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """Dry run reports references and warnings without persisting objects."""
    response = client.post(
        "/api/v1/policies/upload",
        data={"dry_run": "true", "resolve_env": "true"},
        files={"file": ("policy.json", json.dumps(policy_data()))},
    )
    assert response.status_code == 200, response.text
    assert_literal(response.text)
    assert response.json()["warnings"]
    assert not crud_mcp_server.get_multi_by_account(
        db_session, account_id=str(test_user.account_id)
    )


@pytest.mark.parametrize("endpoint", ["upload", "validate", "diff"])
@pytest.mark.parametrize("invalid_field", ["auth_config", "metadata"])
def test_validation_diagnostics_omit_submitted_values(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    endpoint: str,
    invalid_field: str,
) -> None:
    """Diagnostics report the field and problem without copying input values."""
    data = policy_data()
    value = "operator-auth-value"
    if invalid_field == "auth_config":
        data["mcp_servers"][0]["auth_config"] = value
    else:
        data["mcp_servers"][0]["auth_config"] = {"token": value}
        data.pop("metadata")
    response = client.post(
        "/api/v1/policies/" + endpoint,
        files={"file": ("policy.json", json.dumps(data))},
    )
    assert response.status_code == (200 if endpoint == "validate" else 400)
    assert value not in response.text
    assert value not in caplog.text
    errors = response.json().get("detail", response.json())["errors"]
    assert errors
    assert all(error["value"] is None for error in errors)
