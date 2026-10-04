"""PolicyApplier and export handle the ``sensitive_data`` block (#1121)."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

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
