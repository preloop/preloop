"""PolicyApplier and export handle the ``sensitive_data`` block (#1121)."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from preloop.services.policy import PolicyDocument, export_current_policy
from preloop.services.policy.loader import PolicyApplier

BLOCK = {
    "detectors": {
        "locales": ["de"],
        "custom_patterns": [{"name": "employee_id", "regex": r"EMP-\d{6}"}],
    }
}


def test_apply_persists_sensitive_data_block(mocker) -> None:
    replace = mocker.patch(
        "preloop.services.sensitive_data.policy_store.replace_sensitive_data_config"
    )
    applier = PolicyApplier(MagicMock(), uuid.uuid4())
    mocker.patch.object(applier, "_validate_references", return_value=[])
    policy = PolicyDocument.model_validate(
        {"version": "1.0", "metadata": {"name": "p"}, "sensitive_data": BLOCK}
    )
    result = applier.apply(policy, dry_run=False)
    assert result.success, result.errors
    assert result.sensitive_data_applied is True
    replace.assert_called_once()
    assert replace.call_args.args[2].detectors.locales == ["de"]


def test_dry_run_does_not_persist(mocker) -> None:
    replace = mocker.patch(
        "preloop.services.sensitive_data.policy_store.replace_sensitive_data_config"
    )
    applier = PolicyApplier(MagicMock(), uuid.uuid4())
    mocker.patch.object(applier, "_validate_references", return_value=[])
    policy = PolicyDocument.model_validate(
        {"version": "1.0", "metadata": {"name": "p"}, "sensitive_data": BLOCK}
    )
    result = applier.apply(policy, dry_run=True)
    assert result.success
    assert result.sensitive_data_applied is True
    replace.assert_not_called()


def test_export_includes_stored_block(mocker) -> None:
    account = MagicMock()
    account.meta_data = {"sensitive_data": BLOCK}
    mocker.patch("preloop.models.crud.crud_account.get", return_value=account)
    mocker.patch(
        "preloop.models.crud.crud_mcp_server.get_multi_by_account", return_value=[]
    )
    mocker.patch(
        "preloop.models.crud.crud_approval_workflow.get_multi_by_account",
        return_value=[],
    )
    mocker.patch(
        "preloop.models.crud.crud_tool_configuration.get_multi_by_account",
        return_value=[],
    )
    exported = export_current_policy(MagicMock(), str(uuid.uuid4()))
    assert exported.sensitive_data is not None
    assert exported.sensitive_data.detectors.custom_patterns[0].name == "employee_id"


def _stored_rule(types):
    from preloop.services.policy.schema import ModelIORule

    return ModelIORule.model_validate(
        {
            "id": "uses-custom",
            "target": "model.request",
            "detectors": {"pii": {"types": types}},
            "conditions": [{"expression": "pii.found == true", "action": "deny"}],
        }
    )


def test_apply_rejects_a_block_that_drops_a_type_a_stored_rule_uses(mocker) -> None:
    mocker.patch(
        "preloop.models.crud.crud_mcp_server.get_active_by_account", return_value=[]
    )
    mocker.patch(
        "preloop.models.crud.crud_approval_workflow.get_names_by_account",
        return_value=set(),
    )
    mocker.patch(
        "preloop.services.model_content_policy.load_model_io_rules",
        return_value=[_stored_rule(["employee_id"])],
    )
    replace = mocker.patch(
        "preloop.services.sensitive_data.policy_store.replace_sensitive_data_config"
    )
    applier = PolicyApplier(MagicMock(), uuid.uuid4())
    policy = PolicyDocument.model_validate(
        {
            "version": "1.0",
            "metadata": {"name": "p"},
            "sensitive_data": {"detectors": {}},
        }
    )
    result = applier.apply(policy, dry_run=False)
    assert result.success is False
    assert any(
        "uses-custom" in error and "employee_id" in error for error in result.errors
    )
    replace.assert_not_called()
    # Declaring the type again (or importing the rules too) is accepted.
    ok = PolicyApplier(MagicMock(), uuid.uuid4()).apply(
        PolicyDocument.model_validate(
            {"version": "1.0", "metadata": {"name": "p"}, "sensitive_data": BLOCK}
        ),
        dry_run=True,
    )
    assert ok.success, ok.errors


def test_export_tolerates_a_stored_mismatch(mocker) -> None:
    account = MagicMock()
    account.meta_data = {
        "sensitive_data": {"detectors": {"locales": ["de"]}},
        "model_io_rules": [_stored_rule(["employee_id"]).model_dump(mode="json")],
    }
    mocker.patch("preloop.models.crud.crud_account.get", return_value=account)
    mocker.patch(
        "preloop.models.crud.crud_mcp_server.get_multi_by_account", return_value=[]
    )
    mocker.patch(
        "preloop.models.crud.crud_approval_workflow.get_multi_by_account",
        return_value=[],
    )
    mocker.patch(
        "preloop.models.crud.crud_tool_configuration.get_multi_by_account",
        return_value=[],
    )
    exported = export_current_policy(MagicMock(), str(uuid.uuid4()))
    assert exported.model_io[0].id == "uses-custom"
    assert exported.sensitive_data.detectors.locales == ["de"]


def test_export_omits_block_when_absent(mocker) -> None:
    account = MagicMock()
    account.meta_data = {}
    mocker.patch("preloop.models.crud.crud_account.get", return_value=account)
    mocker.patch(
        "preloop.models.crud.crud_mcp_server.get_multi_by_account", return_value=[]
    )
    mocker.patch(
        "preloop.models.crud.crud_approval_workflow.get_multi_by_account",
        return_value=[],
    )
    mocker.patch(
        "preloop.models.crud.crud_tool_configuration.get_multi_by_account",
        return_value=[],
    )
    exported = export_current_policy(MagicMock(), str(uuid.uuid4()))
    assert exported.sensitive_data is None


# ---------------------------------------------------------------------------
# scope.servers: a server the account does not have never matches
# ---------------------------------------------------------------------------

ALLOWLIST_RULE = {
    "id": "allowlist",
    "on": ["tool.args"],
    "condition": "!(tool.name in ['get_issue'])",
    "action": "deny",
}


def _configured_servers(mocker: MockerFixture, *names: str) -> None:
    servers = []
    for name in names:
        server = MagicMock()
        server.name = name
        server.auth_config = None
        servers.append(server)
    mocker.patch(
        "preloop.models.crud.crud_mcp_server.get_active_by_account",
        return_value=servers,
    )
    mocker.patch(
        "preloop.models.crud.crud_approval_workflow.get_names_by_account",
        return_value=set(),
    )
    mocker.patch(
        "preloop.services.model_content_policy.load_model_io_rules", return_value=[]
    )


def _policy(sensitive_data: dict[str, Any], **sections: Any) -> PolicyDocument:
    return PolicyDocument.model_validate(
        {
            "version": "1.0",
            "metadata": {"name": "p"},
            "sensitive_data": sensitive_data,
            **sections,
        }
    )


@pytest.mark.parametrize(
    ("section", "entry"),
    [
        ("rules", {**ALLOWLIST_RULE, "scope": {"servers": ["githb"]}}),
        ("reference_only", {"id": "refs", "scope": {"servers": ["githb"]}}),
    ],
)
def test_apply_rejects_a_scope_server_the_account_lacks(
    mocker: MockerFixture, section: str, entry: dict[str, Any]
) -> None:
    _configured_servers(mocker, "github")
    replace = mocker.patch(
        "preloop.services.sensitive_data.policy_store.replace_sensitive_data_config"
    )
    policy = _policy({section: [entry]})

    result = PolicyApplier(MagicMock(), uuid.uuid4()).apply(policy)
    assert result.success is False
    [error] = result.errors
    assert f"'{entry['id']}'" in error and "'githb'" in error
    assert "github" in error  # the servers it could have meant
    replace.assert_not_called()

    # As for tools, skip_missing_servers turns it into a warning. The entry
    # is kept and matches once a server with that name is configured.
    result = PolicyApplier(MagicMock(), uuid.uuid4()).apply(
        policy, skip_missing_servers=True
    )
    assert result.success, result.errors
    [warning] = result.warnings
    assert "'githb'" in warning
    replace.assert_called_once()


@pytest.mark.parametrize("server", ["github", "GitHub", "in-file", "preloop-mcp"])
def test_scope_servers_the_policy_can_match_pass(
    mocker: MockerFixture, server: str
) -> None:
    _configured_servers(mocker, "github")
    policy = _policy(
        {"rules": [{**ALLOWLIST_RULE, "scope": {"servers": [server]}}]},
        mcp_servers=[{"name": "in-file", "url": "https://mcp.example.com"}],
    )
    assert PolicyApplier(MagicMock(), uuid.uuid4())._validate_references(policy) == []


def test_scope_server_shared_with_the_account_passes(mocker: MockerFixture) -> None:
    # Tool calls also reach servers another account shares here (hook H3),
    # so a scope can name one that the account does not own.
    from preloop.plugins.account_hooks import VISIBLE_MCP_SERVER, VisibilityProvider

    _configured_servers(mocker, "github")
    shared = MagicMock()
    shared.id = uuid.uuid4()
    shared.name = "shared-records"

    class SharedServer(VisibilityProvider):
        def extra_visible_ids(
            self, db: Any, account_id: Any, resource_type: str
        ) -> list[Any]:
            return [shared.id] if resource_type == VISIBLE_MCP_SERVER else []

    mocker.patch("preloop.plugins.account_hooks._visibility_provider", SharedServer())
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
        shared
    ]
    applier = PolicyApplier(db, uuid.uuid4())

    def scoped_to(server: str) -> PolicyDocument:
        return _policy(
            {"reference_only": [{"id": "refs", "scope": {"servers": [server]}}]}
        )

    assert applier._validate_references(scoped_to("shared-records")) == []
    [error] = applier._validate_references(scoped_to("githb"))
    assert "'githb'" in error


def test_model_targets_do_not_check_scope_servers(mocker: MockerFixture) -> None:
    # Model targets ignore scope.servers, so the name cannot stop a match.
    _configured_servers(mocker)
    rule = {
        "id": "prompts",
        "on": ["model.request"],
        "types": ["email"],
        "action": "deny",
        "scope": {"servers": ["githb"]},
    }
    policy = _policy({"rules": [rule]})
    assert PolicyApplier(MagicMock(), uuid.uuid4())._validate_references(policy) == []
