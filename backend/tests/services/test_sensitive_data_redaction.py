"""Redact action and storage-time redaction on every write path (#1123)."""

from __future__ import annotations

import inspect
import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.tools import Tool
from mcp.types import TextContent
from pydantic import ValidationError

from preloop.config import settings
from preloop.models.crud import crud_runtime_session, crud_runtime_session_activity
from preloop.models.crud.flow_execution_log import (
    crud_flow_execution_log,
    storable_log_message,
    storable_log_metadata,
)
from preloop.models.models.audit_log import AuditLog
from preloop.schemas.browser_step import BrowserStepIn
from preloop.services import audit_chain, policy_evaluator
from preloop.services.dynamic_fastmcp import DynamicFastMCP
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.gateway_usage_search import GatewayUsageSearchService
from preloop.services.model_content_policy import (
    enforce_request_policy,
    evaluate_model_io,
    is_notify_only,
    redact_request_upstream,
)
from preloop.services.policy.schema import (
    ModelIORule,
    SensitiveDataConfig,
    ToolCondition,
    ToolDefinition,
)
from preloop.services.sensitive_data import storage
from preloop.services.sensitive_data.detectors import DetectorConfig
from preloop.services.sensitive_data.policy_store import detector_config_from
from preloop.services.sensitive_data.redact import (
    redact_structure,
    redact_text,
    redaction_token,
)
from preloop.services.sensitive_data.storage import (
    StorageScope,
    apply_storage_redaction,
    redact_for_storage,
)
from preloop.services.sensitive_data.tool_policy import (
    compile_model_io_rules,
    evaluate_tool_target,
    redact_tool_result,
    result_text,
)
from preloop.services.session_search_index import index_transcript_message
from preloop.utils.redaction import redact_dict
from preloop.utils.secret_scrubbing import scrub_secrets

EMAIL = "alice@example.com"
IBAN = "DE89 3704 0044 0532 0130 00"
SAMPLE = f"mail {EMAIL} and iban {IBAN}"
R_EMAIL = redaction_token("email")
R_IBAN = redaction_token("iban")


def _config(*rules: dict) -> SensitiveDataConfig:
    return SensitiveDataConfig.model_validate({"rules": list(rules)})


def _redact_rule(**overrides) -> dict:
    base = {
        "id": "redact-pii",
        "on": ["tool.args", "tool.result", "model.request", "model.response"],
        "types": ["email", "iban"],
        "action": "redact",
    }
    base.update(overrides)
    return base


@pytest.fixture
def redact_policy(mocker):
    """Every account resolves to one redact rule for email and IBAN."""
    storage.invalidate_cache()
    config = _config(_redact_rule())
    mocker.patch.object(storage, "resolve_config", return_value=config)
    yield config
    storage.invalidate_cache()


@pytest.fixture
def no_policy(mocker):
    storage.invalidate_cache()
    mocker.patch.object(storage, "resolve_config", return_value=SensitiveDataConfig())
    yield
    storage.invalidate_cache()


def _assert_redacted(blob: str) -> None:
    assert R_EMAIL in blob and R_IBAN in blob, blob
    assert EMAIL not in blob and "3704" not in blob, blob


# ---------------------------------------------------------------------------
# redact_text / redact_structure
# ---------------------------------------------------------------------------


def test_redact_text_replaces_each_match_with_a_typed_token() -> None:
    text, counts = redact_text(SAMPLE)
    assert text == f"mail {R_EMAIL} and iban {R_IBAN}"
    assert counts == {"email": 1, "iban": 1}
    assert redact_text("") == ("", {})
    assert redact_text(None) == (None, {})


def test_redact_structure_rewrites_leaves_and_keeps_keys() -> None:
    value = {"note": SAMPLE, EMAIL: {"list": [IBAN, 3, None]}, "n": 7}
    redacted, counts = redact_structure(value)
    assert redacted["note"] == f"mail {R_EMAIL} and iban {R_IBAN}"
    assert EMAIL in redacted  # keys are untouched
    assert redacted[EMAIL]["list"] == [R_IBAN, 3, None]
    assert counts == {"email": 1, "iban": 2}
    assert value["note"] == SAMPLE  # input not mutated


def test_redact_respects_type_selection() -> None:
    text, counts = redact_text(SAMPLE, DetectorConfig(types=("email",)))
    assert R_EMAIL in text and IBAN in text
    assert counts == {"email": 1}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_redact_action_accepted_for_sensitive_and_model_rules_only() -> None:
    assert _config(_redact_rule()).has_redact_rules()
    ModelIORule(
        id="r",
        target="model.request",
        conditions=[ToolCondition(expression="pii.found == true", action="redact")],
        redact_upstream=True,
    )
    with pytest.raises(ValidationError, match="do not support action 'redact'"):
        ToolDefinition(
            name="t", conditions=[{"expression": "true", "action": "redact"}]
        )
    with pytest.raises(
        ValidationError, match="redact_upstream requires action 'redact'"
    ):
        _config(_redact_rule(action="deny", redact_upstream=True))


# ---------------------------------------------------------------------------
# Storage hook
# ---------------------------------------------------------------------------


def test_storage_hook_applies_in_scope_redact_rules(redact_policy) -> None:
    redacted, counts, rules = redact_for_storage(
        uuid.uuid4(), {"note": SAMPLE}, scope=StorageScope(target="tool.args")
    )
    _assert_redacted(json.dumps(redacted))
    assert counts == {"email": 1, "iban": 1}
    assert [rule.id for rule in rules] == ["redact-pii"]


def test_storage_hook_honours_target_and_scope(mocker) -> None:
    storage.invalidate_cache()
    config = _config(_redact_rule(on=["tool.result"], scope={"tools": ["lookup"]}))
    mocker.patch.object(storage, "resolve_config", return_value=config)
    account = uuid.uuid4()
    untouched = apply_storage_redaction(
        account, SAMPLE, scope=StorageScope(target="tool.args", tool_name="lookup")
    )
    other_tool = apply_storage_redaction(
        account, SAMPLE, scope=StorageScope(target="tool.result", tool_name="other")
    )
    in_scope = apply_storage_redaction(
        account, SAMPLE, scope=StorageScope(target="tool.result", tool_name="lookup")
    )
    any_target = apply_storage_redaction(
        account, SAMPLE, scope=StorageScope(tool_name="lookup")
    )
    assert untouched == SAMPLE and other_tool == SAMPLE
    _assert_redacted(in_scope)
    _assert_redacted(any_target)


def test_no_redact_rules_leaves_output_byte_identical(no_policy) -> None:
    payload = {"note": SAMPLE, "nested": [IBAN]}
    assert apply_storage_redaction(uuid.uuid4(), payload) is payload
    assert apply_storage_redaction(uuid.uuid4(), SAMPLE) == SAMPLE
    assert apply_storage_redaction(None, SAMPLE) == SAMPLE


def test_storage_hook_caches_per_account_and_invalidates_on_write(mocker) -> None:
    storage.invalidate_cache()
    loader = mocker.patch.object(
        storage, "_load_config", return_value=_config(_redact_rule())
    )
    account = uuid.uuid4()
    apply_storage_redaction(account, SAMPLE)
    apply_storage_redaction(account, SAMPLE)
    assert loader.call_count == 1
    storage.invalidate_cache(account)
    apply_storage_redaction(account, SAMPLE)
    assert loader.call_count == 2
    loader.side_effect = RuntimeError("db down")
    storage.invalidate_cache()
    assert apply_storage_redaction(account, SAMPLE) == SAMPLE  # degrades, never raises


def test_policy_store_write_invalidates_storage_cache(mocker) -> None:
    from preloop.models.crud import crud_account
    from preloop.services.sensitive_data.policy_store import (
        replace_sensitive_data_config,
    )

    account = MagicMock()
    account.meta_data = {}
    mocker.patch.object(crud_account, "get", return_value=account)
    invalidate = mocker.patch.object(storage, "invalidate_cache")
    replace_sensitive_data_config(MagicMock(), "acc", _config(_redact_rule()))
    invalidate.assert_called_once_with("acc")


def test_credential_scrub_and_pii_redaction_compose(redact_policy) -> None:
    text = f"key sk-ant-abcdefghijklmnopqrstuvwxyz0123 for {EMAIL}"
    stored = apply_storage_redaction(uuid.uuid4(), scrub_secrets(text))
    assert "abcdefghijklmnop" not in stored and "[REDACTED]" in stored
    assert EMAIL not in stored and R_EMAIL in stored
    nested = apply_storage_redaction(
        uuid.uuid4(), redact_dict({"api_key": "x", "note": EMAIL})
    )
    assert nested == {"api_key": "***REDACTED***", "note": R_EMAIL}


# ---------------------------------------------------------------------------
# Write paths
# ---------------------------------------------------------------------------


def test_policy_decision_rows_store_redacted_args(mocker) -> None:
    audit = MagicMock()
    mocker.patch.object(policy_evaluator, "_get_audit_service", return_value=audit)
    mocker.patch.object(policy_evaluator, "_get_db_factory", return_value=lambda: None)
    account = uuid.uuid4()
    storage.invalidate_cache()
    storage.prime_cache(account, _config(_redact_rule()))
    policy_evaluator._log_policy_decision_async(
        account_id=account,
        tool_name="save_note",
        action="allow",
        tool_args={"note": SAMPLE},
    )
    row = audit.log_policy_decision_async.call_args.kwargs
    _assert_redacted(json.dumps(row["tool_args"]))
    storage.invalidate_cache()


def test_policy_decision_row_write_moves_off_loop_on_a_cache_miss(mocker) -> None:
    """A policy read must not run on the async evaluator's loop."""
    audit = MagicMock()
    mocker.patch.object(policy_evaluator, "_get_audit_service", return_value=audit)
    mocker.patch.object(policy_evaluator, "_get_db_factory", return_value=lambda: None)
    off_loop = mocker.patch.object(policy_evaluator, "submit_off_loop")
    mocker.patch.object(storage, "resolve_config", return_value=_config(_redact_rule()))
    storage.invalidate_cache()
    policy_evaluator._log_policy_decision_async(
        account_id=uuid.uuid4(),
        tool_name="save_note",
        action="allow",
        tool_args={"note": SAMPLE},
    )
    audit.log_policy_decision_async.assert_not_called()
    off_loop.assert_called_once()
    off_loop.call_args.args[0]()  # run the deferred write
    row = audit.log_policy_decision_async.call_args.kwargs
    _assert_redacted(json.dumps(row["tool_args"]))


def test_flow_execution_logs_store_redacted_messages(redact_policy, mocker) -> None:
    account = uuid.uuid4()
    _assert_redacted(storable_log_message(f"agent said {SAMPLE}", account))
    _assert_redacted(json.dumps(storable_log_metadata({"line": SAMPLE}, account)))
    mocker.patch(
        "preloop.models.crud.flow_execution_log._account_for_execution",
        return_value=str(account),
    )
    db = MagicMock()
    row = crud_flow_execution_log.append_log(
        db, str(uuid.uuid4()), {"type": "log", "message": SAMPLE}, commit=False
    )
    _assert_redacted(row.message)


def test_flow_log_account_lookup_never_caches_a_miss(mocker) -> None:
    from preloop.models.crud import flow_execution_log as module

    module._execution_accounts.clear()
    execution_id = uuid.uuid4()
    db = MagicMock()
    db.execute.return_value.first.return_value = None
    assert module._account_for_execution(db, execution_id) is None
    assert str(execution_id) not in module._execution_accounts
    account = uuid.uuid4()
    db.execute.return_value.first.return_value = (account,)
    assert module._account_for_execution(db, execution_id) == str(account)
    db.execute.side_effect = RuntimeError("down")
    assert module._account_for_execution(db, execution_id) == str(account)  # cached hit


def test_flow_log_without_account_keeps_credential_scrub_only(no_policy) -> None:
    text = f"token glpat-abcdefghijklmnop1234 {EMAIL}"
    stored = storable_log_message(text)
    assert "glpat-[REDACTED]" in stored and EMAIL in stored


def test_browser_step_metadata_is_stored_redacted(redact_policy, mocker) -> None:
    mocker.patch.object(
        crud_runtime_session_activity, "_find_browser_step", return_value=None
    )
    mocker.patch.object(
        crud_runtime_session_activity, "_touch_runtime_session_and_agent"
    )
    db = MagicMock()
    row, created = crud_runtime_session_activity.log_browser_step(
        db,
        account_id=uuid.uuid4(),
        runtime_session_id=uuid.uuid4(),
        api_key_id=None,
        step=BrowserStepIn(
            source_step_id="s1",
            step_index=0,
            action="type",
            url=f"https://example.test/?contact={EMAIL}",
            target="input",
            reasoning=f"fill iban {IBAN}",
        ),
        commit=False,
    )
    assert created
    blob = json.dumps(row.metadata_) + (row.summary or "")
    _assert_redacted(blob)


def test_gateway_usage_search_documents_are_redacted(
    redact_policy, monkeypatch
) -> None:
    monkeypatch.setattr(
        settings, "model_gateway_auto_index_interactions", True, raising=False
    )
    monkeypatch.setattr(settings, "model_gateway_capture_content", True, raising=False)
    usage = MagicMock()
    usage.id = uuid.uuid4()
    usage.account_id = uuid.uuid4()
    usage.managed_agent_id = None
    usage.status_code = 200
    usage.endpoint = "/v1/chat/completions"
    usage.method = "POST"
    usage.provider_name = "openai"
    usage.model_alias = "gpt"
    usage.runtime_principal_type = None
    usage.runtime_principal_name = None
    usage.meta_data = {}
    service = GatewayUsageSearchService(db=None)
    document = service.build_index_document(
        usage=usage,
        request_payload={"messages": [{"role": "user", "content": SAMPLE}]},
        response_payload={"choices": [{"message": {"content": f"ok {EMAIL}"}}]},
    )
    assert document is not None
    assert EMAIL not in document.searchable_text
    assert R_EMAIL in document.searchable_text


def test_session_search_documents_are_redacted(
    redact_policy, db_session, test_user
) -> None:
    occurred = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="custom",
        session_source_id="redaction-session",
        session_reference="redaction-session",
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Agent",
        started_at=occurred,
        last_activity_at=occurred,
    )
    stored = index_transcript_message(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        source_id="message-pii",
        text=f"api_key=sk-not-a-real-key {SAMPLE}",
        role="user",
        occurred_at=occurred,
    )
    assert len(stored) == 1
    _assert_redacted(stored[0].content)
    assert "sk-not-a-real-key" not in stored[0].content
    assert stored[0].redaction_state == "redacted"


def test_otlp_tool_spans_carry_no_arguments_or_message_text() -> None:
    from preloop.services import otel_export

    params = inspect.signature(otel_export.emit_tool_call).parameters
    assert not {"arguments", "tool_args", "result", "messages"} & set(params)
    attrs = otel_export._safe_attributes(
        {
            "gen_ai.tool.name": "save_note",
            "gen_ai.tool.call.arguments": SAMPLE,
            "gen_ai.prompt": SAMPLE,
            "gen_ai.input.messages": SAMPLE,
            "gen_ai.completion": SAMPLE,
        }
    )
    assert attrs == {"gen_ai.tool.name": "save_note"}


# ---------------------------------------------------------------------------
# Audit chain: redaction happens before the row is written
# ---------------------------------------------------------------------------


def test_chain_hashes_redacted_details_and_verifies(
    redact_policy, db_session, test_user, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "audit_chain_enabled", True, raising=False)
    monkeypatch.setattr(settings, "audit_chain_seal_lag_seconds", 0, raising=False)
    account_id = test_user.account_id
    base = datetime.now(UTC) - timedelta(minutes=30)
    for index in range(3):
        details = {
            "tool_name": "save_note",
            "tool_args": apply_storage_redaction(
                account_id,
                redact_dict({"note": SAMPLE}),
                scope=StorageScope(target="tool.args", tool_name="save_note"),
            ),
        }
        db_session.add(
            AuditLog(
                account_id=account_id,
                action="tool_call",
                resource_type="tool",
                resource_id="save_note",
                status="executed",
                details=details,
                timestamp=base + timedelta(seconds=index),
            )
        )
    db_session.flush()
    audit_chain.seal_account(db_session, account_id=account_id, lag=timedelta(0))
    report = audit_chain.verify_chain(db_session, account_id=account_id)
    assert report["status"] == "ok"
    segment = audit_chain.chain_segment(db_session, account_id=account_id)
    previous = segment["genesis_hash"]
    for entry in segment["entries"]:
        payload = entry["payload"]
        _assert_redacted(json.dumps(payload["details"]))
        assert entry["prev_hash"] == previous
        assert audit_chain.hash_row(payload) == entry["row_hash"]
        previous = entry["row_hash"]


def test_no_post_seal_rewrite_of_chained_rows(
    db_session, test_user, monkeypatch
) -> None:
    """Guard: redaction must happen before the write; a later edit breaks the chain."""
    monkeypatch.setattr(settings, "audit_chain_enabled", True, raising=False)
    account_id = test_user.account_id
    row = AuditLog(
        account_id=account_id,
        action="tool_call",
        resource_type="tool",
        resource_id="save_note",
        status="executed",
        details={"tool_args": {"note": SAMPLE}},
        timestamp=datetime.now(UTC) - timedelta(minutes=5),
    )
    db_session.add(row)
    db_session.flush()
    audit_chain.seal_account(db_session, account_id=account_id, lag=timedelta(0))
    row.details = {"tool_args": {"note": redact_text(SAMPLE)[0]}}
    db_session.flush()
    report = audit_chain.verify_chain(db_session, account_id=account_id)
    assert report["status"] == "broken"
    assert audit_chain.ROW_SCHEMA == "preloop.audit.chain_row/v1"


# ---------------------------------------------------------------------------
# Model I/O redact action
# ---------------------------------------------------------------------------


def _model_rule(**overrides) -> ModelIORule:
    base = {
        "id": "redact-mail",
        "target": "model.request",
        "detectors": {"pii": {"types": ["email", "iban"]}},
        "conditions": [ToolCondition(expression="pii.found == true", action="redact")],
    }
    base.update(overrides)
    return ModelIORule(**base)


def test_model_redact_rule_never_blocks_and_records_counts(mocker) -> None:
    audit = mocker.patch(
        "preloop.services.model_content_policy._log_policy_decision_async"
    )
    decision = evaluate_model_io(
        rules=[_model_rule()],
        target="model.request",
        text=SAMPLE,
        account_id=uuid.uuid4(),
    )
    assert decision.action == "redact"
    assert decision.redactions[0].counts == {"email": 1, "iban": 1}
    assert decision.upstream_redaction_types() == []
    row = audit.call_args.kwargs
    assert row["action"] == "redact"
    assert row["extra_details"]["redaction_counts"] == {"email": 1, "iban": 1}
    assert EMAIL not in repr(row)
    assert is_notify_only(_model_rule())


def test_model_redact_then_deny_still_denies() -> None:
    deny = _model_rule(
        id="deny-iban",
        detectors={"pii": {"types": ["iban"]}},
        conditions=[ToolCondition(expression="pii.found == true", action="deny")],
    )
    decision = evaluate_model_io(
        rules=[_model_rule(), deny], target="model.request", text=SAMPLE
    )
    assert decision.action == "deny"
    assert [hit.rule_id for hit in decision.redactions] == ["redact-mail"]


def test_redact_request_upstream_rewrites_messages_in_place() -> None:
    messages = [
        {"role": "user", "content": SAMPLE},
        {"role": "user", "content": [{"type": "text", "text": f"iban {IBAN}"}]},
    ]
    payload = {"messages": messages, "input": f"mail {EMAIL}"}
    counts = redact_request_upstream(messages, payload, ["email", "iban"], None)
    assert counts == {"email": 2, "iban": 2}
    _assert_redacted(json.dumps(messages))
    assert payload["input"] == f"mail {R_EMAIL}"


@pytest.mark.parametrize("upstream", [False, True])
def test_gateway_request_upstream_follows_redact_upstream(
    mocker, upstream: bool
) -> None:
    rules = [_model_rule(redact_upstream=upstream)]
    mocker.patch(
        "preloop.services.model_content_policy.load_model_io_rules", return_value=rules
    )
    mocker.patch(
        "preloop.services.model_content_policy.load_sensitive_data_config",
        return_value=SensitiveDataConfig(),
    )
    mocker.patch("preloop.services.model_content_policy._log_policy_decision_async")
    gateway = MagicMock()
    gateway.auth_context.account_id = str(uuid.uuid4())
    gateway.auth_context.user.id = None
    messages = [{"role": "user", "content": SAMPLE}]
    enforce_request_policy(
        gateway, payload={}, ai_model=None, messages=messages, provider="openai"
    )
    if upstream:
        _assert_redacted(messages[0]["content"])
    else:
        assert messages[0]["content"] == SAMPLE


def test_compiled_model_rule_carries_redact_upstream_for_requests_only() -> None:
    compiled = compile_model_io_rules(_config(_redact_rule(redact_upstream=True)))
    by_target = {rule.target: rule for rule in compiled}
    assert by_target["model.request"].redact_upstream is True
    assert by_target["model.response"].redact_upstream is False
    assert compiled[0].conditions[0].action == "redact"


def test_redact_upstream_rejected_without_a_rewritable_target() -> None:
    with pytest.raises(ValidationError, match="redacted at rest only"):
        _config(_redact_rule(on=["model.response"], redact_upstream=True))
    with pytest.raises(ValidationError, match="not available for model.response"):
        ModelIORule(
            id="r",
            target="model.response",
            conditions=[ToolCondition(expression="pii.found == true", action="redact")],
            redact_upstream=True,
        )


def test_redact_request_upstream_rewrites_bare_strings_too() -> None:
    messages = [
        SAMPLE,
        {"role": "user", "content": [f"iban {IBAN}", {"type": "text", "text": "x"}]},
    ]
    counts = redact_request_upstream(messages, {}, ["email", "iban"], None)
    assert counts == {"email": 1, "iban": 2}
    assert messages[0] == f"mail {R_EMAIL} and iban {R_IBAN}"
    assert messages[1]["content"][0] == f"iban {R_IBAN}"


def test_redact_tool_result_keeps_non_text_blocks_in_raw_lists() -> None:
    image = MagicMock(spec=[])  # no .text attribute
    rebuilt = redact_tool_result(
        [TextContent(type="text", text=SAMPLE), image], DetectorConfig()
    )
    assert rebuilt[0].text == f"mail {R_EMAIL} and iban {R_IBAN}"
    assert rebuilt[1] is image


# ---------------------------------------------------------------------------
# Tool path: redact action, upstream copies, stored copies
# ---------------------------------------------------------------------------


def test_tool_redact_rule_is_non_blocking_and_audited(mocker) -> None:
    audit = mocker.patch.object(policy_evaluator, "_log_policy_decision_async")
    outcome = evaluate_tool_target(
        config=_config(_redact_rule()),
        detector_config=None,
        target="tool.args",
        payload={"note": SAMPLE},
        tool_name="save_note",
        server_name="crm",
        managed_agent_id=None,
        account_id=uuid.uuid4(),
    )
    assert outcome.action == "redact"
    row = audit.call_args.kwargs
    assert row["action"] == "redact"
    assert row["extra_details"]["redaction_counts"] == {"email": 1, "iban": 1}
    assert EMAIL not in repr(row)


def test_redact_tool_result_keeps_shape() -> None:
    config = DetectorConfig(types=("email", "iban"))
    assert redact_tool_result(SAMPLE, config) == f"mail {R_EMAIL} and iban {R_IBAN}"
    from fastmcp.tools.tool import ToolResult

    result = ToolResult(
        content=[TextContent(type="text", text=SAMPLE)],
        structured_content={"mail": EMAIL},
    )
    redacted = redact_tool_result(result, config)
    assert result_text(redacted) == f"mail {R_EMAIL} and iban {R_IBAN}\n" + json.dumps(
        {"mail": R_EMAIL}
    )


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
    )


@pytest.fixture
def proxied(monkeypatch, user_context):
    mcp = DynamicFastMCP("test-mcp")
    mcp.set_user_context_provider(lambda: user_context)
    client = MagicMock()
    client.call_tool = AsyncMock(return_value=[MagicMock(text=f"stored {SAMPLE}")])
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
            return_value=[Tool(name="save_note", description="Save", parameters={})]
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
                name="crm",
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
    activity = mocker_log = MagicMock()
    monkeypatch.setattr(crud_runtime_session_activity, "log_tool_call", mocker_log)
    monkeypatch.setattr(
        "preloop.services.account_realtime.emit_account_event", lambda *a, **k: None
    )
    wrapper = mcp._create_proxied_tool_wrapper(
        tool_name="save_note",
        server_id="server-1",
        account_id=user_context.account_id,
        description="Save",
        input_schema={"properties": {"note": {"type": "string"}}},
    )
    internal = f"account_{user_context.account_id.replace('-', '_')}_save_note"
    mcp.tool()(wrapper)
    mcp._registered_proxied_tools.add(internal)
    mcp._proxied_tool_servers["save_note"] = "server-1"
    mcp._proxied_tool_server_names["save_note"] = "crm"
    return mcp, client, audit_service, activity


def _install(monkeypatch, config: SensitiveDataConfig):
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp._load_sensitive_data_policy",
        lambda account_id: (config, detector_config_from(config)),
    )


@pytest.mark.asyncio
async def test_tool_call_stores_redacted_copies_and_sends_original_upstream(
    proxied, monkeypatch, redact_policy, mocker
) -> None:
    mcp, client, audit_service, activity = proxied
    mocker.patch.object(policy_evaluator, "_log_policy_decision_async")
    _install(monkeypatch, redact_policy)
    result = await mcp.call_tool("save_note", {"note": SAMPLE})
    # Upstream receives the original (redact_upstream defaults to false).
    assert client.call_tool.await_args.args[1] == {"note": SAMPLE}
    # The agent receives the original result.
    assert EMAIL in result_text(result)
    # The audit tool row holds the redacted arguments.
    row = audit_service.log_tool_call_async.call_args.kwargs
    _assert_redacted(json.dumps(row["tool_args"]))
    # The activity row never holds the values (summary is None on success).
    assert activity.call_args.kwargs["summary"] is None


@pytest.mark.asyncio
async def test_redact_upstream_rewrites_arguments_and_result(
    proxied, monkeypatch, mocker
) -> None:
    mcp, client, _audit, _activity = proxied
    mocker.patch.object(policy_evaluator, "_log_policy_decision_async")
    config = _config(_redact_rule(redact_upstream=True))
    storage.invalidate_cache()
    mocker.patch.object(storage, "resolve_config", return_value=config)
    _install(monkeypatch, config)
    result = await mcp.call_tool("save_note", {"note": SAMPLE})
    _assert_redacted(json.dumps(client.call_tool.await_args.args[1]))
    _assert_redacted(result_text(result))


@pytest.mark.asyncio
async def test_activity_summary_is_redacted_on_failure(
    proxied, monkeypatch, redact_policy, mocker
) -> None:
    mcp, client, _audit, activity = proxied
    mocker.patch.object(policy_evaluator, "_log_policy_decision_async")
    _install(monkeypatch, redact_policy)
    client.call_tool.side_effect = RuntimeError(f"upstream rejected {EMAIL}")
    await mcp.call_tool("save_note", {"note": "x"})
    summary = activity.call_args.kwargs["summary"]
    assert EMAIL not in summary and R_EMAIL in summary


@pytest.mark.asyncio
async def test_approval_row_stores_redacted_args(redact_policy) -> None:
    from preloop.services.approval_service import ApprovalService

    service = ApprovalService.__new__(ApprovalService)
    stored = await service._storage_redacted_tool_args(
        uuid.uuid4(),
        tool_name="save_note",
        tool_args={"note": SAMPLE},
        managed_agent_id=None,
    )
    _assert_redacted(json.dumps(stored))


@pytest.mark.asyncio
async def test_create_approval_request_persists_the_redacted_copy(
    redact_policy, mocker
) -> None:
    """The row and the lifecycle audit payload hold the redacted arguments."""
    from preloop.services import approval_service as module

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
        tool_name="save_note",
        tool_args={"note": SAMPLE},
        timeout_seconds=60,
    )
    _assert_redacted(json.dumps(request.tool_args))
    added = [call.args[0] for call in service.db.add.call_args_list]
    assert request in added
    _assert_redacted(
        json.dumps(lifecycle.call_args.kwargs["extra_details"]["tool_args"])
    )
