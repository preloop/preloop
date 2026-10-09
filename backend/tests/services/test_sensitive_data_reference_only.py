"""Reference-only logging for selected tools, servers and agents (#1124)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.tools import Tool
from pydantic import ValidationError

from preloop.api.endpoints import policies
from preloop.config import settings
from preloop.models.models.audit_log import AuditLog
from preloop.services import audit_chain, policy_evaluator
from preloop.services.dynamic_fastmcp import DynamicFastMCP
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.policy.schema import SensitiveDataConfig
from preloop.services.sensitive_data import reference, storage
from preloop.services.sensitive_data.reference import (
    REFERENCE_MARKER,
    SEALED_ARGS_KEY,
    build_reference_record,
    extract_keep_fields,
    is_reference_record,
    reference_rule_for,
    rotate_salt,
    seal_original,
    strip_sealed_original,
    tool_args_for_replay,
    unseal_original,
    verify_hmac,
)
from preloop.services.sensitive_data.storage import (
    StorageScope,
    apply_storage_redaction,
)
from preloop.services.sensitive_data.tool_policy import result_text
from preloop.utils.redaction import redact_dict

EMAIL = "alice@example.com"
RECORD_TEXT = f"patient Jane Doe, {EMAIL}, dx: hypertension"
ARGS = {
    "patient_id": "P-77",
    "consent_id": "consent-9",
    "call": {"id": "c-1"},
    "note": EMAIL,
}


def _config(**overrides) -> SensitiveDataConfig:
    rule = {
        "id": "patient-tools",
        "scope": {"tools": ["get_patient_record"], "servers": ["ehr"]},
        "keep_fields": ["$.consent_id", "$.call.id"],
        "approver_view": "redacted",
    }
    rule.update(overrides)
    return SensitiveDataConfig.model_validate({"reference_only": [rule]})


def _fake_account():
    account = MagicMock()
    account.meta_data = {}
    return account


@pytest.fixture
def salts(mocker):
    """In-memory account rows per account id so salts persist across calls."""
    accounts: dict = {}

    def get(db, id):  # noqa: A002 - mirrors crud signature
        return accounts.setdefault(str(id), _fake_account())

    mocker.patch.object(reference.crud_account, "get", side_effect=get)
    mocker.patch(
        "preloop.models.db.session.get_session_factory",
        return_value=lambda: MagicMock(),
    )
    mocker.patch.object(reference, "flag_modified")
    reference.invalidate_salt_cache()
    yield accounts
    reference.invalidate_salt_cache()


@pytest.fixture
def reference_policy(mocker, salts):
    storage.invalidate_cache()
    config = _config()
    mocker.patch.object(storage, "resolve_config", return_value=config)
    yield config
    storage.invalidate_cache()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_reference_only_yaml_from_the_issue_loads() -> None:
    config = SensitiveDataConfig.model_validate(
        {
            "reference_only": [
                {
                    "id": "patient-tools",
                    "scope": {
                        "agents": [],
                        "tools": ["get_patient_record"],
                        "servers": ["ehr"],
                    },
                    "keep_fields": ["$.consent_id", "$.call.id"],
                    "approver_view": "redacted",
                }
            ]
        }
    )
    rule = config.reference_only[0]
    assert rule.keep_fields == ["$.consent_id", "$.call.id"]
    assert rule.approver_view_value() == "redacted"
    assert (
        reference_rule_for(config, tool_name="get_patient_record", server_name="EHR")
        is rule
    )
    assert reference_rule_for(config, tool_name="other", server_name="ehr") is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"scope": {}}, "must set scope.tools"),
        ({"keep_fields": ["consent_id"]}, "not supported"),
        ({"keep_fields": ["$..deep"]}, "not supported"),
        ({"approver_view": "raw"}, "approver_view"),
    ],
)
def test_invalid_reference_rules_rejected(overrides: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _config(**overrides)


def test_keep_fields_subset_extraction() -> None:
    args = {"consent_id": "c", "items": [{"id": 1}, {"id": 2}], "a": [{"b": "x"}]}
    assert extract_keep_fields(
        args, ["$.consent_id", "$.items[*].id", "$.a[0].b", "$.nope"]
    ) == {
        "$.consent_id": "c",
        "$.items[*].id": [1, 2],
        "$.a[0].b": "x",
    }


# ---------------------------------------------------------------------------
# Fingerprints and salts
# ---------------------------------------------------------------------------


def test_same_args_same_hmac_and_accounts_differ(salts) -> None:
    account_a, account_b = uuid.uuid4(), uuid.uuid4()
    rule = _config().reference_only[0]
    first = build_reference_record(
        account_id=account_a, rule=rule, tool_name="t", arguments=ARGS
    )
    second = build_reference_record(
        account_id=account_a,
        rule=rule,
        tool_name="t",
        arguments=dict(reversed(ARGS.items())),
    )
    other = build_reference_record(
        account_id=account_b, rule=rule, tool_name="t", arguments=ARGS
    )
    assert first["args_hmac"] == second["args_hmac"]
    assert first["salt_id"] == second["salt_id"]
    assert other["args_hmac"] != first["args_hmac"]
    assert len(first["args_hmac"]) == 64
    assert first["fingerprint_algo"] == "scrypt"
    assert first["kept"] == {"$.consent_id": "consent-9", "$.call.id": "c-1"}
    assert first["arg_keys"] == ["patient_id", "consent_id", "call", "note"]
    assert first["args_bytes"] > 0 and first["result_bytes"] == 0
    assert is_reference_record(first) and first[REFERENCE_MARKER] is True
    blob = json.dumps(first)
    assert EMAIL not in blob and "P-77" not in blob


def test_hmac_rows_written_before_scrypt_still_verify(salts) -> None:
    account = uuid.uuid4()
    rule = _config().reference_only[0]
    record = build_reference_record(
        account_id=account, rule=rule, tool_name="t", arguments=ARGS
    )
    entry = reference._cached_salts(account, None)[-1]
    legacy = reference._legacy_hmac_hex(reference._secret(entry), ARGS)
    assert legacy != record["args_hmac"]
    assert verify_hmac(account, ARGS, legacy) == (True, record["salt_id"])


def test_salts_are_stored_encrypted_and_never_exported(salts) -> None:
    account = uuid.uuid4()
    build_reference_record(
        account_id=account,
        rule=_config().reference_only[0],
        tool_name="t",
        arguments=ARGS,
    )
    entries = salts[str(account)].meta_data[reference.SALTS_META_KEY]
    assert entries[0]["encrypted"] != reference.decrypt_value(entries[0]["encrypted"])
    assert reference.salt_ids(account) == [entries[0]["salt_id"]]


def test_hash_check_verifies_without_storing(salts) -> None:
    account = uuid.uuid4()
    record = build_reference_record(
        account_id=account,
        rule=_config().reference_only[0],
        tool_name="t",
        arguments=ARGS,
    )
    assert verify_hmac(account, ARGS, record["args_hmac"]) == (True, record["salt_id"])
    assert verify_hmac(account, {**ARGS, "note": "x"}, record["args_hmac"]) == (
        False,
        None,
    )
    assert verify_hmac(uuid.uuid4(), ARGS, record["args_hmac"]) == (False, None)


def test_salt_rotation_keeps_old_rows_verifying(salts) -> None:
    account = uuid.uuid4()
    rule = _config().reference_only[0]
    old = build_reference_record(
        account_id=account, rule=rule, tool_name="t", arguments=ARGS
    )
    new_salt = rotate_salt(MagicMock(), account)
    new = build_reference_record(
        account_id=account, rule=rule, tool_name="t", arguments=ARGS
    )
    assert new["salt_id"] == new_salt != old["salt_id"]
    assert new["args_hmac"] != old["args_hmac"]
    assert verify_hmac(account, ARGS, old["args_hmac"], salt_id=old["salt_id"]) == (
        True,
        old["salt_id"],
    )
    assert verify_hmac(account, ARGS, old["args_hmac"]) == (True, old["salt_id"])
    assert verify_hmac(account, ARGS, old["args_hmac"], salt_id=new_salt) == (
        False,
        None,
    )
    assert reference.salt_ids(account) == [old["salt_id"], new_salt]


def test_hash_check_endpoint_is_account_scoped(salts) -> None:
    account = MagicMock()
    account.id = uuid.uuid4()
    other = MagicMock()
    other.id = uuid.uuid4()
    record = build_reference_record(
        account_id=account.id,
        rule=_config().reference_only[0],
        tool_name="t",
        arguments=ARGS,
    )
    user = MagicMock()
    ok = policies.sensitive_data_hash_check(
        policies.SensitiveDataHashCheckRequest(
            payload=ARGS, args_hmac=record["args_hmac"]
        ),
        account=account,
        current_user=user,
        db=MagicMock(),
    )
    assert ok.match is True and ok.salt_id == record["salt_id"]
    wrong = policies.sensitive_data_hash_check(
        policies.SensitiveDataHashCheckRequest(
            payload={"x": 1}, args_hmac=record["args_hmac"]
        ),
        account=account,
        current_user=user,
        db=MagicMock(),
    )
    assert wrong.match is False
    foreign = policies.sensitive_data_hash_check(
        policies.SensitiveDataHashCheckRequest(
            payload=ARGS, args_hmac=record["args_hmac"]
        ),
        account=other,
        current_user=user,
        db=MagicMock(),
    )
    assert foreign.match is False


def test_hash_check_endpoint_requires_manage_policies() -> None:
    import inspect

    source = inspect.getsource(policies)
    index = source.index("def sensitive_data_hash_check(")
    decorator_block = source[max(0, index - 400) : index]
    assert '@require_permission("manage_policies")' in decorator_block


# ---------------------------------------------------------------------------
# Storage: in-scope calls hold references only
# ---------------------------------------------------------------------------


def test_storage_hook_substitutes_a_reference_record(reference_policy) -> None:
    account = uuid.uuid4()
    stored = apply_storage_redaction(
        account,
        redact_dict(ARGS),
        scope=StorageScope(
            target="tool.args", tool_name="get_patient_record", server_name="ehr"
        ),
    )
    assert is_reference_record(stored)
    assert stored["kept"] == {"$.consent_id": "consent-9", "$.call.id": "c-1"}
    assert EMAIL not in json.dumps(stored) and "P-77" not in json.dumps(stored)
    result_ref = apply_storage_redaction(
        account,
        RECORD_TEXT,
        scope=StorageScope(
            target="tool.result", tool_name="get_patient_record", server_name="ehr"
        ),
    )
    assert result_ref.startswith("[reference-only:patient-tools] get_patient_record")
    assert EMAIL not in result_ref and "Jane" not in result_ref


def test_out_of_scope_call_on_the_same_server_is_stored_as_before(
    reference_policy,
) -> None:
    stored = apply_storage_redaction(
        uuid.uuid4(),
        ARGS,
        scope=StorageScope(
            target="tool.args", tool_name="list_appointments", server_name="ehr"
        ),
    )
    assert stored == ARGS


def test_kept_fields_pass_through_redact_rules(mocker, salts) -> None:
    storage.invalidate_cache()
    config = SensitiveDataConfig.model_validate(
        {
            "rules": [
                {"id": "r", "on": ["tool.args"], "types": ["email"], "action": "redact"}
            ],
            "reference_only": [
                {"id": "ref", "scope": {"tools": ["t"]}, "keep_fields": ["$.note"]}
            ],
        }
    )
    mocker.patch.object(storage, "resolve_config", return_value=config)
    stored = apply_storage_redaction(
        uuid.uuid4(),
        {"note": EMAIL},
        scope=StorageScope(target="tool.args", tool_name="t"),
    )
    assert stored["kept"] == {"$.note": "[REDACTED:email]"}
    storage.invalidate_cache()


@pytest.fixture
def user_context():
    return UserContext(
        user_id=str(uuid.uuid4()),
        account_id=str(uuid.uuid4()),
        username="tester",
        has_tracker=True,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
        runtime_session_id=str(uuid.uuid4()),
        managed_agent_id="agent-1",
    )


@pytest.fixture
def proxied(monkeypatch, user_context):
    from preloop.models.crud import crud_runtime_session_activity

    mcp = DynamicFastMCP("test-mcp")
    mcp.set_user_context_provider(lambda: user_context)
    client = MagicMock()
    client.call_tool = AsyncMock(return_value=[MagicMock(text=RECORD_TEXT)])
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp.get_db", lambda: iter([MagicMock()])
    )
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp.kill_switch_service.tools_halted",
        lambda db, account_id: False,
    )
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp.crud_tool_configuration.get_multi_by_account",
        lambda *args, **kwargs: [],
    )
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=MagicMock())
    session.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "preloop.models.db.session.get_async_db_session", lambda: session
    )
    monkeypatch.setattr(
        "preloop.services.policy_evaluator.evaluate_policy_async",
        AsyncMock(return_value=("allow", None, None)),
    )
    monkeypatch.setattr(
        mcp,
        "list_tools",
        AsyncMock(
            return_value=[
                Tool(name="get_patient_record", description="Read", parameters={})
            ]
        ),
    )
    monkeypatch.setattr(
        "preloop.services.approval_helper.require_approval",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp._resolve_proxied_tool_server",
        MagicMock(
            return_value=MagicMock(
                name="ehr",
                url="http://example.test",
                auth_type="none",
                auth_config={},
                transport="http",
            )
        ),
    )
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp.get_mcp_client_pool",
        lambda: MagicMock(get_client=AsyncMock(return_value=client)),
    )
    monkeypatch.setattr(mcp, "_halt_dispatch_denial", AsyncMock(return_value=None))
    audit_service = MagicMock()
    plugin_manager = MagicMock()
    plugin_manager.get_service = lambda name: (
        audit_service if name == "audit_service" else None
    )
    monkeypatch.setattr(
        "preloop.plugins.base.get_plugin_manager", lambda: plugin_manager
    )
    activity = MagicMock()
    monkeypatch.setattr(crud_runtime_session_activity, "log_tool_call", activity)
    monkeypatch.setattr(
        "preloop.services.account_realtime.emit_account_event", lambda *a, **k: None
    )
    wrapper = mcp._create_proxied_tool_wrapper(
        tool_name="get_patient_record",
        account_id=user_context.account_id,
        description="Read",
        input_schema={
            "properties": {
                "patient_id": {"type": "string"},
                "consent_id": {"type": "string"},
                "note": {"type": "string"},
            }
        },
    )
    internal = f"account_{user_context.account_id.replace('-', '_')}_get_patient_record"
    mcp.tool()(wrapper)
    mcp._registered_proxied_tools.add(internal)
    mcp._proxied_tool_servers["get_patient_record"] = "server-1"
    mcp._proxied_tool_server_names["get_patient_record"] = "ehr"
    return mcp, client, audit_service, activity


@pytest.mark.asyncio
async def test_in_scope_proxied_call_leaves_no_payload_in_any_store(
    proxied, monkeypatch, reference_policy, mocker
) -> None:
    mcp, client, audit_service, activity = proxied
    decision_audit = MagicMock()
    monkeypatch.setattr(policy_evaluator, "_get_audit_service", lambda: decision_audit)

    async def _allow_and_record(**kwargs):
        """The fixture stubs the evaluator. Record a real decision row anyway."""
        subject = kwargs.get("subject_context") or {}
        policy_evaluator._log_policy_decision_async(
            account_id=kwargs["account_id"],
            tool_name=kwargs["tool_name"],
            action="allow",
            rule_description="No tool configuration found",
            tool_args=kwargs.get("tool_args"),
            server_name=kwargs.get("server_name"),
            managed_agent_id=subject.get("managed_agent_id"),
        )
        return ("allow", None, None)

    monkeypatch.setattr(
        "preloop.services.policy_evaluator.evaluate_policy_async",
        _allow_and_record,
    )
    monkeypatch.setattr(
        policy_evaluator, "_get_db_factory", lambda: lambda: MagicMock()
    )
    monkeypatch.setattr(storage, "has_cached_config", lambda account_id: True)
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp._load_sensitive_data_policy",
        lambda account_id: (None, None),
    )
    args = {"patient_id": "P-77", "consent_id": "consent-9", "note": EMAIL}
    client.call_tool.side_effect = RuntimeError(f"upstream said {RECORD_TEXT}")
    result = await mcp.call_tool("get_patient_record", args)
    assert result.is_error  # transport failure: the summary store receives text
    stores = {
        "audit_tool_row": json.dumps(
            audit_service.log_tool_call_async.call_args.kwargs["tool_args"]
        ),
        "activity_summary": activity.call_args.kwargs["summary"] or "",
        "decision_rows": json.dumps(
            [
                c.kwargs.get("tool_args")
                for c in decision_audit.log_policy_decision_async.call_args_list
            ],
            default=str,
        ),
    }
    assert decision_audit.log_policy_decision_async.call_args_list, (
        "no policy-decision row was written"
    )
    assert any(
        is_reference_record(c.kwargs.get("tool_args"))
        for c in decision_audit.log_policy_decision_async.call_args_list
    )
    for name, blob in stores.items():
        assert EMAIL not in blob and "P-77" not in blob and "Jane" not in blob, name
    audit_row = audit_service.log_tool_call_async.call_args.kwargs["tool_args"]
    assert is_reference_record(audit_row)
    assert audit_row["kept"] == {"$.consent_id": "consent-9"}
    assert audit_row["args_hmac"] and audit_row["salt_id"]
    assert stores["activity_summary"].startswith("[reference-only:patient-tools]")


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_original_until_decided_keeps_raw_args_sealed_until_decision(
    mocker, salts
) -> None:
    from preloop.services import approval_service as module

    storage.invalidate_cache()
    config = _config(
        approver_view="original_until_decided", scope={"tools": ["get_patient_record"]}
    )
    mocker.patch.object(storage, "resolve_config", return_value=config)
    service = module.ApprovalService.__new__(module.ApprovalService)
    service.db = AsyncMock()
    service.db.add = MagicMock()
    mocker.patch(
        "preloop.services.approval_attribution.resolve_managed_agent_name",
        AsyncMock(return_value=None),
    )
    mocker.patch.object(service, "_record_event", AsyncMock())
    mocker.patch.object(service, "_broadcast_approval_update", AsyncMock())
    lifecycle = mocker.patch.object(module, "_log_approval_lifecycle_async")
    request = await service.create_approval_request(
        account_id=str(uuid.uuid4()),
        tool_configuration_id=uuid.uuid4(),
        approval_workflow_id=uuid.uuid4(),
        tool_name="get_patient_record",
        tool_args=ARGS,
        timeout_seconds=60,
    )
    stored = request.tool_args
    assert is_reference_record(stored)
    assert SEALED_ARGS_KEY in stored
    assert unseal_original(stored[SEALED_ARGS_KEY]) == ARGS
    # Notification payloads mask the sealed copy and never held the raw args.
    for payload in (
        redact_dict(stored),
        lifecycle.call_args.kwargs["extra_details"]["tool_args"],
    ):
        blob = json.dumps(payload, default=str)
        assert EMAIL not in blob and "P-77" not in blob
        assert payload[SEALED_ARGS_KEY] == "***REDACTED***"
    # Approver API returns the raw args while pending, 404 after.
    from fastapi import HTTPException

    from preloop.api.endpoints import approval_requests as api

    request.status = "pending"
    mocker.patch.object(api.crud_approval_request, "get", return_value=request)
    user = MagicMock()
    user.account_id = request.account_id
    shown = api.get_approval_original_args(
        request.id, current_user=user, db=MagicMock()
    )
    assert shown["tool_args"] == ARGS
    # Decision: the sealed copy is removed in the same update.
    mocker.patch.object(
        service, "get_approval_request", AsyncMock(return_value=request)
    )
    mocker.patch.object(service, "_release_parked_executions", AsyncMock())
    from preloop.models.schemas.approval_request import ApprovalRequestUpdate

    decided = await service.update_approval_request(
        request.id, ApprovalRequestUpdate(status="approved")
    )
    assert SEALED_ARGS_KEY not in decided.tool_args
    assert is_reference_record(decided.tool_args)
    with pytest.raises(HTTPException) as exc_info:
        api.get_approval_original_args(request.id, current_user=user, db=MagicMock())
    assert exc_info.value.status_code == 404
    storage.invalidate_cache()


def test_strip_and_seal_helpers() -> None:
    sealed = seal_original(ARGS)
    assert sealed != json.dumps(ARGS) and unseal_original(sealed) == ARGS
    assert unseal_original(None) is None
    assert strip_sealed_original({"a": 1, SEALED_ARGS_KEY: sealed}) == ({"a": 1}, True)
    assert strip_sealed_original({"a": 1}) == ({"a": 1}, False)


def test_replay_fails_when_the_seal_cannot_be_read() -> None:
    with pytest.raises(RuntimeError, match="could not be read"):
        tool_args_for_replay(
            {REFERENCE_MARKER: True, SEALED_ARGS_KEY: "not-a-real-seal"}
        )
    with pytest.raises(RuntimeError, match="never stored"):
        tool_args_for_replay(
            {REFERENCE_MARKER: True, "tool_name": "get_patient_record"}
        )
    assert tool_args_for_replay({SEALED_ARGS_KEY: seal_original({})}) == {}


def _decision_stored(mocker, config, **scope):
    audit = MagicMock()
    mocker.patch.object(policy_evaluator, "_get_audit_service", return_value=audit)
    mocker.patch.object(
        policy_evaluator, "_get_db_factory", return_value=lambda: MagicMock()
    )
    mocker.patch.object(storage, "has_cached_config", return_value=True)
    mocker.patch.object(storage, "resolve_config", return_value=config)
    policy_evaluator._log_policy_decision_async(
        account_id=uuid.uuid4(),
        tool_name="get_patient_record",
        action="allow",
        tool_args=dict(ARGS),
        **scope,
    )
    assert audit.log_policy_decision_async.called
    return audit.log_policy_decision_async.call_args.kwargs["tool_args"]


@pytest.mark.asyncio
async def test_servers_only_and_agents_only_rules_store_references(
    mocker, salts
) -> None:
    """A rule that names only servers or only agents must not store raw args."""
    from preloop.services import approval_service as approval_module

    storage.invalidate_cache()
    agent_id = uuid.uuid4()
    servers_only = SensitiveDataConfig.model_validate(
        {
            "reference_only": [
                {
                    "id": "ehr-only",
                    "scope": {"servers": ["ehr"]},
                    "keep_fields": ["$.consent_id"],
                }
            ]
        }
    )
    agents_only = SensitiveDataConfig.model_validate(
        {
            "reference_only": [
                {
                    "id": "agent-only",
                    "scope": {"agents": [str(agent_id)]},
                    "keep_fields": ["$.consent_id"],
                }
            ]
        }
    )
    for stored in (
        _decision_stored(mocker, servers_only, server_name="ehr"),
        _decision_stored(mocker, agents_only, managed_agent_id=str(agent_id)),
    ):
        assert is_reference_record(stored)
        assert EMAIL not in json.dumps(stored) and "P-77" not in json.dumps(stored)

    service = approval_module.ApprovalService.__new__(approval_module.ApprovalService)
    mocker.patch.object(storage, "resolve_config", return_value=servers_only)
    approved = await service._storage_redacted_tool_args(
        uuid.uuid4(),
        tool_name="get_patient_record",
        tool_args=dict(ARGS),
        managed_agent_id=None,
        server_name="ehr",
    )
    assert is_reference_record(approved)
    assert EMAIL not in json.dumps(approved)

    mocker.patch.object(storage, "resolve_config", return_value=agents_only)
    approved = await service._storage_redacted_tool_args(
        uuid.uuid4(),
        tool_name="get_patient_record",
        tool_args=dict(ARGS),
        managed_agent_id=agent_id,
        server_name=None,
    )
    assert is_reference_record(approved)
    assert EMAIL not in json.dumps(approved)


@pytest.mark.asyncio
async def test_evaluator_context_supplies_scope_for_reference_rules(
    mocker, salts
) -> None:
    """A decision row inherits server and agent from the evaluator context.

    ``evaluate_policy_async`` sets ``_policy_storage_scope`` and the logger
    reads it. Calling ``_log_policy_decision_async`` with those fields
    directly would still pass if that contextvar were removed, and a
    servers-only or agents-only rule would store the raw arguments.
    """
    agent_id = uuid.uuid4()
    account_id = uuid.uuid4()
    servers_only = SensitiveDataConfig.model_validate(
        {
            "reference_only": [
                {
                    "id": "ehr-only",
                    "scope": {"servers": ["ehr"]},
                    "keep_fields": ["$.consent_id"],
                }
            ]
        }
    )
    agents_only = SensitiveDataConfig.model_validate(
        {
            "reference_only": [
                {
                    "id": "agent-only",
                    "scope": {"agents": [str(agent_id)]},
                    "keep_fields": ["$.consent_id"],
                }
            ]
        }
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_result.scalars.return_value.first.return_value = None
    db = MagicMock()
    db.execute = AsyncMock(return_value=mock_result)
    audit = MagicMock()
    mocker.patch.object(policy_evaluator, "_get_audit_service", return_value=audit)
    cases = (
        (servers_only, "ehr", None, "ehr-only"),
        (agents_only, None, str(agent_id), "agent-only"),
    )
    storage.invalidate_cache()
    try:
        for config, server_name, managed_agent_id, rule_id in cases:
            storage.prime_cache(account_id, config)
            audit.log_policy_decision_async.reset_mock()
            decision = await policy_evaluator.evaluate_policy_async(
                db=db,
                tool_name="get_patient_record",
                tool_args=dict(ARGS),
                account_id=account_id,
                subject_context={"managed_agent_id": managed_agent_id},
                server_name=server_name,
            )
            assert decision.action == "allow"
            stored = audit.log_policy_decision_async.call_args.kwargs["tool_args"]
            assert is_reference_record(stored)
            assert stored["rule_id"] == rule_id
            blob = json.dumps(stored)
            assert EMAIL not in blob and "P-77" not in blob
            assert stored["kept"] == {"$.consent_id": "consent-9"}
    finally:
        storage.invalidate_cache()


# ---------------------------------------------------------------------------
# Audit chain and export
# ---------------------------------------------------------------------------


def test_chain_verifies_with_reference_records_and_export_lists_salt_ids(
    reference_policy, db_session, test_user, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "audit_chain_enabled", True, raising=False)
    account_id = test_user.account_id
    base = datetime.now(UTC) - timedelta(minutes=20)
    for index in range(2):
        details = {
            "tool_name": "get_patient_record",
            "tool_args": apply_storage_redaction(
                account_id,
                redact_dict(ARGS),
                scope=StorageScope(
                    target="tool.args",
                    tool_name="get_patient_record",
                    server_name="ehr",
                ),
            ),
        }
        db_session.add(
            AuditLog(
                account_id=account_id,
                action="tool_call",
                resource_type="tool",
                resource_id="get_patient_record",
                status="executed",
                details=details,
                timestamp=base + timedelta(seconds=index),
            )
        )
    db_session.flush()
    audit_chain.seal_account(db_session, account_id=account_id, lag=timedelta(0))
    assert audit_chain.verify_chain(db_session, account_id=account_id)["status"] == "ok"
    segment = audit_chain.chain_segment(db_session, account_id=account_id)
    salt_id = segment["entries"][0]["payload"]["details"]["tool_args"]["salt_id"]
    assert salt_id in segment["reference_salt_ids"]
    # FastAPI drops fields the response model does not declare.
    from preloop.schemas.audit_chain import ChainSegmentRead

    exported = ChainSegmentRead.model_validate(segment).model_dump()
    assert salt_id in exported["reference_salt_ids"]
    blob = json.dumps(segment)
    assert EMAIL not in blob and "P-77" not in blob
    entries = reference.crud_account.get(db_session, id=account_id).meta_data[
        reference.SALTS_META_KEY
    ]
    assert all(entry["encrypted"] not in blob for entry in entries)
    previous = segment["genesis_hash"]
    for entry in segment["entries"]:
        assert entry["prev_hash"] == previous
        assert audit_chain.hash_row(entry["payload"]) == entry["row_hash"]
        previous = entry["row_hash"]


def test_session_search_indexes_tool_name_and_reference_only(
    reference_policy, db_session, test_user, monkeypatch
) -> None:
    from preloop.models.crud import crud_runtime_session
    from preloop.services.session_search_index import index_tool_call

    monkeypatch.setattr(settings, "model_gateway_capture_content", True, raising=False)
    occurred = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="custom",
        session_source_id="reference-session",
        session_reference="reference-session",
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Agent",
        started_at=occurred,
        last_activity_at=occurred,
    )
    stored = index_tool_call(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        source_id="call-1",
        server_name="ehr",
        tool_name="get_patient_record",
        status="succeeded",
        summary=RECORD_TEXT,
        occurred_at=occurred,
    )
    assert stored, "indexing is disabled in this environment"
    content = "\n".join(chunk.content for chunk in stored)
    assert "tool_name: get_patient_record" in content
    assert "[reference-only:patient-tools]" in content
    assert EMAIL not in content and "Jane" not in content


def test_result_text_of_reference_record_has_no_payload() -> None:
    record = {REFERENCE_MARKER: True, "args_hmac": "abc"}
    assert "abc" in result_text(record)


# ---------------------------------------------------------------------------
# $result keep_fields, result_hmac and rule_matched on the tool_call row (#1368)
# ---------------------------------------------------------------------------

RESULT_RULE = {
    "keep_fields": ["$.consent_id", "$result.consent_id", "$result.grant.scope"]
}
RESULT_JSON = {
    "consent_id": "cons-42",
    "owner": "did:example:owner1",
    "grant": {"scope": "daily"},
}


def _result_rule():
    return _config(**RESULT_RULE).reference_only[0]


def test_result_paths_validate_and_old_paths_still_load() -> None:
    rule = _result_rule()
    assert rule.keep_fields == [
        "$.consent_id",
        "$result.consent_id",
        "$result.grant.scope",
    ]
    # $.result.x stays an argument path named "result".
    old = _config(keep_fields=["$.result.consent_id"]).reference_only[0]
    assert reference.split_keep_fields(old.keep_fields) == (
        ["$.result.consent_id"],
        [],
    )
    for bad in ["$results.x", "$result", "result.x", "$result..x"]:
        with pytest.raises(ValidationError):
            _config(keep_fields=[bad])


def test_argument_keep_fields_ignore_result_paths() -> None:
    kept = reference.extract_keep_fields(
        {"consent_id": "a", "result": {"consent_id": "b"}},
        ["$.consent_id", "$.result.consent_id", "$result.consent_id"],
    )
    assert kept == {"$.consent_id": "a", "$.result.consent_id": "b"}


def test_result_keep_from_structured_content() -> None:
    from fastmcp.tools.tool import ToolResult
    from mcp.types import TextContent

    result = ToolResult(
        content=[TextContent(type="text", text="not json")],
        structured_content=RESULT_JSON,
    )
    kept = reference.extract_result_keep_fields(result, _result_rule().keep_fields)
    assert kept == {"$result.consent_id": "cons-42", "$result.grant.scope": "daily"}
    # MCP wire form (dict, camelCase) gives the same answer.
    wire = {"content": [], "structuredContent": RESULT_JSON}
    assert reference.extract_result_keep_fields(wire, ["$result.consent_id"]) == {
        "$result.consent_id": "cons-42"
    }


def test_result_keep_from_first_json_text_block() -> None:
    from mcp.types import TextContent

    result = MagicMock(spec=["content"])
    result.content = [
        TextContent(type="text", text=json.dumps(RESULT_JSON)),
        TextContent(type="text", text=json.dumps({"consent_id": "other"})),
    ]
    assert reference.extract_result_keep_fields(
        result, ["$result.consent_id", "$result.grant.scope", "$result.missing"]
    ) == {"$result.consent_id": "cons-42", "$result.grant.scope": "daily"}
    plain = MagicMock(spec=["content"])
    plain.content = [TextContent(type="text", text="no json here")]
    assert reference.extract_result_keep_fields(plain, ["$result.consent_id"]) == {}


def test_absent_result_leaves_result_fields_empty(salts) -> None:
    record = reference.build_reference_record(
        account_id=uuid.uuid4(),
        rule=_result_rule(),
        tool_name="get_patient_record",
        arguments=ARGS,
    )
    assert record["kept"] == {"$.consent_id": "consent-9"}
    assert record["kept_result"] == {}
    assert record["result_hmac"] is None


def test_attach_result_sets_salted_result_hmac(reference_policy, salts) -> None:
    from fastmcp.tools.tool import ToolResult

    config = _config(**RESULT_RULE)
    scope = StorageScope(
        target="tool.args", tool_name="get_patient_record", server_name="ehr"
    )
    result = ToolResult(content=[], structured_content=RESULT_JSON)
    rows = []
    for account in (uuid.uuid4(), uuid.uuid4()):
        stored = apply_storage_redaction(account, ARGS, scope=scope, config=config)
        rows.append(
            storage.attach_result_to_reference(
                account, stored, result=result, scope=scope, config=config
            )
        )
    first, second = rows
    assert first["kept_result"] == {
        "$result.consent_id": "cons-42",
        "$result.grant.scope": "daily",
    }
    assert first["result_hmac"] and second["result_hmac"]
    # Salted per account: same result, different fingerprint.
    assert first["result_hmac"] != second["result_hmac"]
    # Not an unsalted hash of the payload.
    payload = reference.result_fingerprint_payload(result)
    import hashlib as _hashlib

    plain = _hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    assert first["result_hmac"] != plain
    assert "did:example:owner1" not in json.dumps(first)
    # Non-reference rows pass through untouched.
    assert storage.attach_result_to_reference(
        uuid.uuid4(), {"a": 1}, result=result, scope=scope, config=config
    ) == {"a": 1}


def test_kept_result_values_pass_through_redact_rules(mocker, salts) -> None:
    config = SensitiveDataConfig.model_validate(
        {
            "rules": [
                {
                    "id": "r",
                    "on": ["tool.result"],
                    "types": ["email"],
                    "action": "redact",
                }
            ],
            "reference_only": [
                {
                    "id": "ref",
                    "scope": {"tools": ["t"]},
                    "keep_fields": ["$result.contact"],
                }
            ],
        }
    )
    scope = StorageScope(target="tool.args", tool_name="t")
    stored = apply_storage_redaction(uuid.uuid4(), {"x": 1}, scope=scope, config=config)
    out = storage.attach_result_to_reference(
        uuid.uuid4(),
        stored,
        result={"structuredContent": {"contact": EMAIL}},
        scope=scope,
        config=config,
    )
    assert out["kept_result"] == {"$result.contact": "[REDACTED:email]"}


async def _call_with_decision(proxied, monkeypatch, mocker, decision):
    mcp, client, audit_service, _ = proxied
    config = _config(**RESULT_RULE)
    storage.invalidate_cache()
    mocker.patch.object(storage, "resolve_config", return_value=config)
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp.apply_storage_redaction_config",
        lambda account_id: config,
    )
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp._load_sensitive_data_policy",
        lambda account_id: (None, None),
    )
    monkeypatch.setattr(
        "preloop.services.policy_evaluator.evaluate_policy_async",
        AsyncMock(return_value=decision),
    )
    client.call_tool.return_value = [MagicMock(text=json.dumps(RESULT_JSON))]
    await mcp.call_tool(
        "get_patient_record", {"patient_id": "P-77", "consent_id": "consent-9"}
    )
    storage.invalidate_cache()
    return audit_service.log_tool_call_async.call_args.kwargs


@pytest.mark.asyncio
async def test_tool_call_row_has_kept_result_and_rule_matched_on_allow(
    proxied, monkeypatch, mocker, salts
) -> None:
    kwargs = await _call_with_decision(
        proxied,
        monkeypatch,
        mocker,
        policy_evaluator.PolicyDecision("allow", None, "allow owner reads"),
    )
    row = kwargs["tool_args"]
    assert is_reference_record(row)
    assert row["rule_id"] == "patient-tools"
    assert row["kept"] == {"$.consent_id": "consent-9"}
    assert row["kept_result"]["$result.consent_id"] == "cons-42"
    assert row["result_hmac"] and row["salt_id"]
    assert "did:example:owner1" not in json.dumps(row)
    assert kwargs["rule_matched"] == "allow owner reads"
    assert kwargs["result"] != RESULT_JSON  # status string, not the result


@pytest.mark.asyncio
async def test_rule_matched_for_require_approval_then_executed(
    proxied, monkeypatch, mocker, salts
) -> None:
    kwargs = await _call_with_decision(
        proxied,
        monkeypatch,
        mocker,
        ("require_approval", uuid.uuid4(), "approve owner writes"),
    )
    assert kwargs["rule_matched"] == "approve owner writes"
    assert kwargs["tool_args"]["kept_result"] == {
        "$result.consent_id": "cons-42",
        "$result.grant.scope": "daily",
    }


def test_result_keep_falls_back_to_text_when_structured_lacks_the_path() -> None:
    """FastMCP wraps a JSON string return as {"result": "<string>"}."""
    from fastmcp.tools.tool import ToolResult
    from mcp.types import TextContent

    text = json.dumps(RESULT_JSON)
    result = ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content={"result": text},
    )
    assert reference.extract_result_keep_fields(result, ["$result.consent_id"]) == {
        "$result.consent_id": "cons-42"
    }
    assert reference.extract_result_keep_fields(None, ["$result.consent_id"]) == {}


# ---------------------------------------------------------------------------
# Caller-supplied reference markers are data, not a server-built record
# ---------------------------------------------------------------------------


def test_forged_marker_out_of_scope_is_still_redacted(mocker, salts) -> None:
    storage.invalidate_cache()
    config = SensitiveDataConfig.model_validate(
        {
            "rules": [
                {"id": "r", "on": ["tool.args"], "types": ["email"], "action": "redact"}
            ]
        }
    )
    mocker.patch.object(storage, "resolve_config", return_value=config)
    forged = {REFERENCE_MARKER: True, "note": EMAIL}
    stored = apply_storage_redaction(
        uuid.uuid4(), forged, scope=StorageScope(target="tool.args", tool_name="t")
    )
    assert EMAIL not in json.dumps(stored)
    storage.invalidate_cache()


def test_forged_marker_in_scope_becomes_a_real_record(reference_policy) -> None:
    forged = {**ARGS, REFERENCE_MARKER: True, "schema": reference.RECORD_SCHEMA}
    stored = apply_storage_redaction(
        uuid.uuid4(),
        forged,
        scope=StorageScope(
            target="tool.args", tool_name="get_patient_record", server_name="ehr"
        ),
    )
    assert reference.is_built_reference_record(stored)
    assert stored["args_hmac"]
    assert EMAIL not in json.dumps(stored) and "P-77" not in json.dumps(stored)


def test_attach_result_ignores_a_forged_record(reference_policy, salts) -> None:
    from fastmcp.tools.tool import ToolResult

    config = _config(**RESULT_RULE)
    scope = StorageScope(
        target="tool.args", tool_name="get_patient_record", server_name="ehr"
    )
    forged = {REFERENCE_MARKER: True, "rule_id": "patient-tools"}
    result = ToolResult(content=[], structured_content=RESULT_JSON)
    out = storage.attach_result_to_reference(
        uuid.uuid4(), forged, result=result, scope=scope, config=config
    )
    assert out is forged and "kept_result" not in out


def test_result_keep_skips_leading_non_json_text_block() -> None:
    from mcp.types import TextContent

    result = MagicMock(spec=["content"])
    result.content = [
        TextContent(type="text", text="Here is the record:"),
        TextContent(type="text", text=json.dumps(RESULT_JSON)),
    ]
    assert reference.extract_result_keep_fields(result, ["$result.consent_id"]) == {
        "$result.consent_id": "cons-42"
    }


@pytest.mark.parametrize(
    ("result", "path", "expected"),
    [
        ({"items": [{"id": "a"}, {"id": "b"}]}, "$result.items[*].id", ["a", "b"]),
        ({"items": [{"id": "a"}, {"id": "b"}]}, "$result.items[1].id", "b"),
        ([{"id": "a"}, {"id": "b"}], "$result[0].id", "a"),
        ({"consent_id": "plain"}, "$result.consent_id", "plain"),
    ],
)
def test_result_keep_list_paths_and_plain_results(result, path, expected) -> None:
    kept = reference.extract_result_keep_fields(result, [path])
    argument_kept = reference.extract_keep_fields(result, ["$" + path[7:]])
    assert kept == {path: expected}
    # Same walk as the argument path, rooted at the result.
    assert list(argument_kept.values()) == [expected]


def test_result_keep_from_plain_string_result() -> None:
    text = json.dumps(RESULT_JSON)
    assert reference.extract_result_keep_fields(text, ["$result.grant.scope"]) == {
        "$result.grant.scope": "daily"
    }
    assert reference.extract_result_keep_fields("not json", ["$result.x"]) == {}
