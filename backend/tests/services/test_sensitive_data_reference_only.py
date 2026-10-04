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
    assert first["kept"] == {"$.consent_id": "consent-9", "$.call.id": "c-1"}
    assert first["arg_keys"] == ["patient_id", "consent_id", "call", "note"]
    assert first["args_bytes"] > 0 and first["result_bytes"] == 0
    assert is_reference_record(first) and first[REFERENCE_MARKER] is True
    blob = json.dumps(first)
    assert EMAIL not in blob and "P-77" not in blob


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
        "preloop.services.dynamic_fastmcp.crud_mcp_server.get_visible",
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
        server_id="server-1",
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
    decision_rows = mocker.patch.object(policy_evaluator, "_log_policy_decision_async")
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
            [c.kwargs.get("tool_args") for c in decision_rows.call_args_list],
            default=str,
        ),
    }
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
