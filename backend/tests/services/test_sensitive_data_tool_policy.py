"""Sensitive-data rules on MCP tool arguments and results (#1122)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.tools import Tool
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from pydantic import ValidationError

from preloop.services import policy_evaluator
from preloop.services.dynamic_fastmcp import (
    DynamicFastMCP,
    _rule_context_var,
    _rule_workflow_id_var,
)
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.policy import PolicyDocument
from preloop.services.policy.schema import SensitiveDataConfig
from preloop.services.sensitive_data import tool_policy
from preloop.services.sensitive_data.detectors import DetectorConfig
from preloop.services.sensitive_data.policy_store import detector_config_from
from preloop.services.sensitive_data.tool_policy import (
    SOURCE_SENSITIVE_DATA_RULE,
    compile_model_io_rules,
    evaluate_tool_target,
    flatten_string_leaves,
    result_text,
)

IBAN = "DE89 3704 0044 0532 0130 00"
CARD = "4111 1111 1111 1111"


def _config(*rules: dict, detectors: dict | None = None) -> SensitiveDataConfig:
    return SensitiveDataConfig.model_validate(
        {"detectors": detectors, "rules": list(rules)}
    )


def _rule(**overrides) -> dict:
    base = {
        "id": "block-cards",
        "on": ["tool.args"],
        "types": ["credit_card", "iban"],
        "action": "deny",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestRuleSchema:
    def test_rule_yaml_from_the_issue_loads(self) -> None:
        doc = PolicyDocument.model_validate(
            {
                "version": "1.0",
                "metadata": {"name": "t"},
                "approval_workflows": [{"name": "humans", "timeout_seconds": 60}],
                "sensitive_data": {
                    "rules": [
                        {
                            "id": "block-cards-in-tools",
                            "on": [
                                "tool.args",
                                "tool.result",
                                "model.request",
                                "model.response",
                            ],
                            "scope": {"agents": [], "tools": [], "servers": []},
                            "types": ["credit_card", "iban"],
                            "action": "deny",
                        },
                        {
                            "id": "ask",
                            "on": ["tool.result"],
                            "action": "require_approval",
                            "approval_workflow": "humans",
                        },
                    ]
                },
            }
        )
        rules = doc.sensitive_data.rules
        assert rules[0].has_tool_target() and rules[0].has_model_target()
        assert rules[0].scope.is_empty()
        assert doc.sensitive_data.types_for_rule(rules[1])[:3] == [
            "email",
            "phone",
            "credit_card",
        ]

    @pytest.mark.parametrize(
        ("rule", "message"),
        [
            (
                {"id": "r", "on": ["tool.args"], "action": "allow"},
                "do not support action 'allow'",
            ),
            (
                {"id": "r", "on": ["tool.args"], "action": "deny", "types": ["nope"]},
                "unknown types",
            ),
            ({"id": "r", "on": ["nowhere"], "action": "deny"}, "tool.args"),
            ({"id": " ", "on": ["tool.args"], "action": "deny"}, "cannot be empty"),
        ],
    )
    def test_invalid_rules_are_rejected(self, rule: dict, message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            _config(rule)

    def test_duplicate_rule_ids_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Duplicate sensitive_data rule id"):
            _config(_rule(), _rule())

    def test_unknown_workflow_reference_rejected_by_the_account_check(
        self, mocker
    ) -> None:
        """Cross-references resolve against the account (main's #1240 shape)."""
        from preloop.services.policy.loader import PolicyApplier

        mocker.patch(
            "preloop.models.crud.crud_mcp_server.get_active_by_account", return_value=[]
        )
        names = mocker.patch(
            "preloop.models.crud.crud_approval_workflow.get_names_by_account",
            return_value=set(),
        )
        mocker.patch(
            "preloop.services.model_content_policy.load_model_io_rules", return_value=[]
        )
        policy = PolicyDocument.model_validate(
            {
                "version": "1.0",
                "metadata": {"name": "t"},
                "sensitive_data": {
                    "rules": [
                        _rule(action="require_approval", approval_workflow="ghost")
                    ]
                },
            }
        )
        errors = PolicyApplier(MagicMock(), uuid.uuid4())._validate_references(policy)
        assert any("block-cards" in e and "ghost" in e for e in errors)
        names.assert_called_once()
        # A workflow the account already has resolves the reference.
        names.return_value = {"ghost"}
        assert (
            PolicyApplier(MagicMock(), uuid.uuid4())._validate_references(policy) == []
        )

    def test_rule_types_default_to_detector_block_types(self) -> None:
        config = _config(_rule(types=None), detectors={"types": ["email"]})
        assert config.types_for_rule(config.rules[0]) == ["email"]

    def test_scope_matching(self) -> None:
        config = _config(
            _rule(
                scope={"tools": ["get_patient"], "servers": ["EHR"], "agents": ["a1"]}
            )
        )
        scope = config.rules[0].scope
        assert scope.matches(
            tool_name="get_patient", server_name="ehr", managed_agent_id="a1"
        )
        assert not scope.matches(
            tool_name="other", server_name="ehr", managed_agent_id="a1"
        )
        assert not scope.matches(
            tool_name="get_patient", server_name="crm", managed_agent_id="a1"
        )
        assert not scope.matches(
            tool_name="get_patient", server_name="ehr", managed_agent_id=None
        )


# ---------------------------------------------------------------------------
# Evaluator unit tests
# ---------------------------------------------------------------------------


def test_flatten_keeps_key_paths_and_skips_markers() -> None:
    leaves = flatten_string_leaves(
        {
            "note": "a",
            "customer": {"emails": ["x", "y"]},
            "_preloop_origin": "z",
            "n": 3,
            "ok": True,
            "none": None,
        }
    )
    assert leaves == [
        ("note", "a"),
        ("customer.emails[0]", "x"),
        ("customer.emails[1]", "y"),
        ("n", "3"),
    ]


def test_numeric_card_argument_is_scanned() -> None:
    outcome = evaluate_tool_target(
        config=_config(_rule()),
        detector_config=None,
        target="tool.args",
        payload={"card": 4111111111111111},
        tool_name="pay",
        server_name="billing",
        managed_agent_id=None,
    )
    assert outcome.action == "deny"
    assert outcome.summary.paths == ["card"]


def test_result_text_covers_strings_tool_results_and_blocks() -> None:
    assert result_text("plain") == "plain"
    tool_result = ToolResult(
        content=[
            TextContent(type="text", text="one"),
            TextContent(type="text", text="two"),
        ]
    )
    assert result_text(tool_result) == "one\ntwo"
    assert result_text([MagicMock(text="a")]) == "a"
    assert result_text(None) == ""


def test_deny_rule_reports_types_paths_and_hash_only(mocker) -> None:
    audit = mocker.patch.object(policy_evaluator, "_log_policy_decision_async")
    account_id = uuid.uuid4()
    outcome = evaluate_tool_target(
        config=_config(_rule()),
        detector_config=None,
        target="tool.args",
        payload={"note": f"card {CARD}", "other": "clean"},
        tool_name="pay",
        server_name="billing",
        managed_agent_id=None,
        account_id=account_id,
        user_id=None,
        correlation_id="corr",
    )
    assert outcome.action == "deny"
    assert outcome.rule_id == "block-cards"
    assert outcome.bindings() == {
        "pii": {
            "found": True,
            "types_found": ["credit_card"],
            "count": 1,
            "paths": ["note"],
        }
    }
    audit.assert_called_once()
    kwargs = audit.call_args.kwargs
    assert kwargs["action"] == "deny"
    assert kwargs["condition_matched"] == "block-cards"
    assert kwargs["extra_details"]["types_found"] == ["credit_card"]
    assert kwargs["extra_details"]["source"] == SOURCE_SENSITIVE_DATA_RULE
    assert "4111" not in repr(kwargs)


def test_no_rule_in_scope_runs_no_detector(mocker) -> None:
    detect = mocker.patch.object(tool_policy, "detect")
    outcome = evaluate_tool_target(
        config=_config(_rule(scope={"tools": ["other"]})),
        detector_config=None,
        target="tool.args",
        payload={"note": CARD},
        tool_name="pay",
        server_name="billing",
        managed_agent_id=None,
    )
    assert outcome.action == "allow" and outcome.scan is None
    detect.assert_not_called()


def test_notify_rule_records_a_notice_and_does_not_block(mocker) -> None:
    audit = mocker.patch.object(policy_evaluator, "_log_policy_decision_async")
    schedule = mocker.patch.object(tool_policy, "schedule_policy_notice")
    outcome = evaluate_tool_target(
        config=_config(_rule(id="watch", action="notify")),
        detector_config=None,
        target="tool.args",
        payload={"note": f"IBAN {IBAN}"},
        tool_name="pay",
        server_name="billing",
        managed_agent_id=None,
        account_id=uuid.uuid4(),
    )
    assert outcome.action == "notify"
    assert audit.call_args.kwargs["action"] == "notify"
    notice = schedule.call_args.args[0]
    assert notice.rule_id == "watch"
    assert "iban" in notice.excerpt and "3704" not in notice.excerpt


def test_first_blocking_rule_wins_after_notify(mocker) -> None:
    mocker.patch.object(policy_evaluator, "_log_policy_decision_async")
    mocker.patch.object(tool_policy, "schedule_policy_notice")
    outcome = evaluate_tool_target(
        config=_config(
            _rule(id="watch", action="notify"),
            _rule(id="ask", action="require_approval"),
        ),
        detector_config=None,
        target="tool.args",
        payload={"note": CARD},
        tool_name="pay",
        server_name="billing",
        managed_agent_id=None,
        account_id=uuid.uuid4(),
    )
    assert outcome.action == "require_approval"
    assert outcome.rule_id == "ask"
    assert len(outcome.notices) == 1
    # The notify hit is still recorded even though a later rule blocked.
    assert tool_policy.schedule_policy_notice.call_count == 1
    assert tool_policy.schedule_policy_notice.call_args.args[0].rule_id == "watch"
    context = outcome.rule_context()
    assert context["source"] == SOURCE_SENSITIVE_DATA_RULE
    assert context["detector_summary"]["pii.types_found"] == ["credit_card"]


def test_rule_only_sees_its_own_types() -> None:
    outcome = evaluate_tool_target(
        config=_config(
            _rule(id="emails", types=["email"]),
            _rule(id="cards", types=["credit_card"]),
        ),
        detector_config=None,
        target="tool.args",
        payload={"note": CARD},
        tool_name="pay",
        server_name="billing",
        managed_agent_id=None,
    )
    assert outcome.rule_id == "cards"
    assert outcome.summary.types_found == ["credit_card"]


def test_custom_pattern_from_detectors_block_applies_to_tool_rules() -> None:
    config = _config(
        _rule(id="badges", on=["tool.result"], types=["employee_id"]),
        detectors={"custom_patterns": [{"name": "employee_id", "regex": r"EMP-\d{6}"}]},
    )
    outcome = evaluate_tool_target(
        config=config,
        detector_config=detector_config_from(config),
        target="tool.result",
        payload="badge EMP-123456",
        tool_name="lookup",
        server_name="hr",
        managed_agent_id=None,
    )
    assert outcome.action == "deny"
    assert outcome.summary.types_found == ["employee_id"]


@pytest.mark.parametrize(("mode", "expected"), [("deny", "deny"), ("allow", "allow")])
def test_detector_timeout_follows_rule_fail_mode(
    mocker, mode: str, expected: str
) -> None:
    mocker.patch.object(policy_evaluator, "_log_policy_decision_async")
    mocker.patch.object(
        tool_policy,
        "scan_with_timeout",
        return_value=tool_policy.ScanSummary(timed_out=True, text_sha256="x"),
    )
    outcome = evaluate_tool_target(
        config=_config(_rule(on_detector_timeout=mode)),
        detector_config=None,
        target="tool.args",
        payload={"note": CARD},
        tool_name="pay",
        server_name="billing",
        managed_agent_id=None,
        account_id=uuid.uuid4(),
    )
    assert outcome.action == expected
    if expected == "deny":
        assert outcome.detector_summary()["detector_timeout"] is True


def test_scan_with_timeout_returns_timed_out_summary(mocker) -> None:
    import time

    def slow(*_args, **_kwargs):
        time.sleep(0.5)
        return tool_policy.ScanSummary()

    mocker.patch.object(tool_policy, "_scan_leaves", side_effect=slow)
    summary = tool_policy.scan_with_timeout(
        [("a", "text")], DetectorConfig(), timeout_ms=50
    )
    assert summary.timed_out is True


# ---------------------------------------------------------------------------
# Model targets compile to model I/O rules
# ---------------------------------------------------------------------------


def test_model_targets_compile_to_model_io_rules() -> None:
    config = _config(
        _rule(
            id="cards",
            on=["tool.args", "model.request", "model.response"],
            action="require_approval",
            approval_workflow="humans",
        ),
        _rule(id="tools-only"),
    )
    compiled = compile_model_io_rules(config)
    assert [rule.id for rule in compiled] == [
        "sensitive-data:cards:model.request",
        "sensitive-data:cards:model.response",
    ]
    first = compiled[0]
    assert first.detectors.pii.types == ["credit_card", "iban"]
    assert first.conditions[0].expression == "pii.found == true"
    assert first.conditions[0].action == "require_approval"
    assert first.approval_workflow == "humans"


def test_compiled_rules_honour_agent_scope() -> None:
    config = _config(_rule(id="cards", on=["model.request"], scope={"agents": ["a1"]}))
    assert compile_model_io_rules(config, managed_agent_id="a1")
    assert compile_model_io_rules(config, managed_agent_id="a2") == []
    assert compile_model_io_rules(config) == []


def test_compiled_rules_ignore_tool_and_server_scope() -> None:
    config = _config(
        _rule(
            id="cards",
            on=["model.request"],
            scope={"agents": ["a1"], "tools": ["search"], "servers": ["crm"]},
        )
    )
    assert [r.id for r in compile_model_io_rules(config, managed_agent_id="a1")] == [
        "sensitive-data:cards:model.request"
    ]


def test_compiled_rules_evaluate_on_the_model_path(mocker) -> None:
    from preloop.services.model_content_policy import _load_gateway_policy_rules

    mocker.patch(
        "preloop.services.model_content_policy.load_gateway_policy_blocks",
        return_value=([], _config(_rule(id="cards", on=["model.request"]))),
    )
    gateway = MagicMock()
    gateway.auth_context.account_id = "acc"
    rules = _load_gateway_policy_rules(gateway, ai_model=None, provider="openai")
    assert [rule.id for rule in rules] == ["sensitive-data:cards:model.request"]
    from preloop.services.model_content_policy import evaluate_model_io

    decision = evaluate_model_io(
        rules=rules, target="model.request", text=f"pay {CARD}"
    )
    assert decision.action == "deny"
    assert decision.rule_id == "sensitive-data:cards:model.request"


# ---------------------------------------------------------------------------
# pii.* bindings for tool access-rule conditions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "condition_type", "expected"),
    [
        ("pii.found == true", "simple", True),
        ("pii.count >= 2", "simple", False),
        ("pii.types_found.contains('iban')", "simple", True),
        ("args.note.contains('x')", "simple", True),
        ("pii.found && 'iban' in pii.types_found", "cel", True),
        ("size(pii.paths) == 1 && args.note == 'x'", "cel", True),
    ],
)
def test_tool_conditions_can_read_pii_bindings(
    expression, condition_type, expected
) -> None:
    context = {
        policy_evaluator.EXTRA_BINDINGS_KEY: {
            "pii": {
                "found": True,
                "types_found": ["iban"],
                "count": 1,
                "paths": ["note"],
            }
        }
    }
    assert (
        policy_evaluator._evaluate_rule_condition(
            expression, condition_type, {"note": "x"}, context
        )
        is expected
    )


def test_pii_simple_condition_without_bindings_falls_back_to_args() -> None:
    assert (
        policy_evaluator._evaluate_rule_condition("pii.found == true", "simple", {}, {})
        is False
    )


# ---------------------------------------------------------------------------
# call_tool integration (proxied tool, mocked upstream)
# ---------------------------------------------------------------------------


@pytest.fixture
def user_context():
    return UserContext(
        user_id=str(uuid.uuid4()),
        account_id=str(uuid.uuid4()),
        username="tester",
        has_tracker=True,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
        managed_agent_id="agent-1",
    )


@pytest.fixture
def proxied(monkeypatch, user_context):
    """A DynamicFastMCP with one proxied tool whose upstream client is a mock."""
    mcp = DynamicFastMCP("test-mcp")
    mcp.set_user_context_provider(lambda: user_context)
    client = MagicMock()
    client.call_tool = AsyncMock(return_value=[MagicMock(text="ok")])
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
    evaluate = AsyncMock(return_value=("allow", None, None))
    monkeypatch.setattr(
        "preloop.services.policy_evaluator.evaluate_policy_async", evaluate
    )
    monkeypatch.setattr(
        mcp,
        "list_tools",
        AsyncMock(
            return_value=[Tool(name="save_note", description="Save", parameters={})]
        ),
    )
    require_approval = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(
        "preloop.services.approval_helper.require_approval", require_approval
    )
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp._resolve_proxied_tool_server",
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
    monkeypatch.setattr(mcp, "_persist_tool_call_activity", MagicMock())
    wrapper = mcp._create_proxied_tool_wrapper(
        tool_name="save_note",
        account_id=user_context.account_id,
        description="Save",
        input_schema={"properties": {"note": {"type": "string"}}},
    )
    internal = f"account_{user_context.account_id.replace('-', '_')}_save_note"
    mcp.tool()(wrapper)
    mcp._registered_proxied_tools.add(internal)
    mcp._proxied_tool_servers["save_note"] = "server-1"
    mcp._proxied_tool_server_names["save_note"] = "crm"
    return mcp, client, evaluate, require_approval


def _install_policy(monkeypatch, config: SensitiveDataConfig | None):
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp._load_sensitive_data_policy",
        lambda account_id: (
            (config, detector_config_from(config)) if config else (None, None)
        ),
    )
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp._resolve_approval_workflow_id",
        lambda account_id, name: "wf-1",
    )


@pytest.mark.asyncio
async def test_deny_rule_refuses_before_upstream_is_called(
    proxied, monkeypatch, mocker
) -> None:
    mcp, client, _evaluate, _approval = proxied
    audit = mocker.patch.object(policy_evaluator, "_log_policy_decision_async")
    _install_policy(monkeypatch, _config(_rule()))
    result = await mcp.call_tool("save_note", {"note": f"card {CARD}"})
    assert result.is_error
    text = result.content[0].text
    assert "block-cards" in text and "credit_card" in text and "4111" not in text
    client.call_tool.assert_not_called()
    assert audit.call_args.kwargs["extra_details"]["types_found"] == ["credit_card"]


@pytest.mark.asyncio
async def test_result_rule_replaces_upstream_result_with_refusal(
    proxied, monkeypatch, mocker
) -> None:
    mcp, client, _evaluate, _approval = proxied
    audit = mocker.patch.object(policy_evaluator, "_log_policy_decision_async")
    client.call_tool.return_value = [MagicMock(text=f"account IBAN {IBAN}")]
    _install_policy(
        monkeypatch, _config(_rule(id="no-ibans", on=["tool.result"], types=["iban"]))
    )
    result = await mcp.call_tool("save_note", {"note": "clean"})
    assert result.is_error
    assert "no-ibans" in result.content[0].text
    assert "3704" not in result.content[0].text
    client.call_tool.assert_awaited_once()
    row = audit.call_args.kwargs
    assert row["extra_details"]["types_found"] == ["iban"]
    assert row["extra_details"]["target"] == "tool.result"
    assert "3704" not in repr(row)
    assert mcp._persist_tool_call_activity.call_args.kwargs["status"] == "refused"


@pytest.mark.asyncio
async def test_scope_by_tool_leaves_other_tools_untouched(
    proxied, monkeypatch, mocker
) -> None:
    mcp, client, _evaluate, _approval = proxied
    detect = mocker.spy(tool_policy, "detect")
    _install_policy(monkeypatch, _config(_rule(scope={"tools": ["charge_card"]})))
    result = await mcp.call_tool("save_note", {"note": f"card {CARD}"})
    assert not getattr(result, "is_error", False)
    client.call_tool.assert_awaited_once()
    detect.assert_not_called()


@pytest.mark.asyncio
async def test_scope_by_agent_limits_the_rule(
    proxied, monkeypatch, user_context
) -> None:
    mcp, client, _evaluate, _approval = proxied
    _install_policy(monkeypatch, _config(_rule(scope={"agents": ["agent-1"]})))
    denied = await mcp.call_tool("save_note", {"note": f"card {CARD}"})
    assert denied.is_error
    client.call_tool.assert_not_called()
    user_context.managed_agent_id = "agent-2"
    allowed = await mcp.call_tool("save_note", {"note": f"card {CARD}"})
    assert not getattr(allowed, "is_error", False)
    client.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_scope_by_server_matches_the_proxied_server_name(
    proxied, monkeypatch
) -> None:
    mcp, client, _evaluate, _approval = proxied
    _install_policy(monkeypatch, _config(_rule(scope={"servers": ["crm"]})))
    denied = await mcp.call_tool("save_note", {"note": f"card {CARD}"})
    assert denied.is_error
    client.call_tool.assert_not_called()


@pytest.mark.asyncio
async def test_require_approval_on_args_carries_types_found(
    proxied, monkeypatch, mocker
) -> None:
    mcp, client, _evaluate, approval = proxied
    mocker.patch.object(policy_evaluator, "_log_policy_decision_async")
    captured: dict = {}

    async def capture(**kwargs):
        captured["workflow_id"] = kwargs.get("workflow_id")
        captured["rule_context"] = kwargs.get("rule_context") or _rule_context_var.get(
            None
        )
        captured["workflow_var"] = _rule_workflow_id_var.get(None)
        return True, None

    approval.side_effect = capture
    _install_policy(
        monkeypatch,
        _config(_rule(action="require_approval", approval_workflow="humans")),
    )
    await mcp.call_tool("save_note", {"note": f"card {CARD}"})
    assert captured["workflow_id"] == "wf-1"
    context = captured["rule_context"]
    assert context["source"] == SOURCE_SENSITIVE_DATA_RULE
    assert context["rule_id"] == "block-cards"
    assert context["detector_summary"]["pii.types_found"] == ["credit_card"]
    client.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_require_approval_without_workflow_fails_closed(
    proxied, monkeypatch, mocker
) -> None:
    mcp, client, _evaluate, _approval = proxied
    mocker.patch.object(policy_evaluator, "_log_policy_decision_async")
    _install_policy(monkeypatch, _config(_rule(action="require_approval")))
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp._resolve_approval_workflow_id",
        lambda a, n: None,
    )
    result = await mcp.call_tool("save_note", {"note": f"card {CARD}"})
    assert result.is_error and "no approval workflow" in result.content[0].text
    client.call_tool.assert_not_called()


@pytest.mark.asyncio
async def test_result_require_approval_holds_until_decided(
    proxied, monkeypatch, mocker
) -> None:
    mcp, client, _evaluate, approval = proxied
    mocker.patch.object(policy_evaluator, "_log_policy_decision_async")
    client.call_tool.return_value = [MagicMock(text=f"IBAN {IBAN}")]
    _install_policy(
        monkeypatch,
        _config(_rule(id="ask", on=["tool.result"], action="require_approval")),
    )
    holds: list[dict] = []
    decision = {"approved": False}

    async def approve(**kwargs):
        # The proxied wrapper asks require_approval for the call itself;
        # only the result hold carries target=tool.result.
        if kwargs.get("arguments", {}).get("target") != "tool.result":
            return True, None
        holds.append(kwargs)
        return (True, None) if decision["approved"] else (False, "Approval declined")

    approval.side_effect = approve
    withheld = await mcp.call_tool("save_note", {"note": "x"})
    assert withheld.is_error and "declined" in withheld.content[0].text
    assert len(holds) == 1
    kwargs = holds[0]
    assert kwargs["arguments"]["detector_summary"]["pii.types_found"] == ["iban"]
    assert "3704" not in repr(kwargs["arguments"])
    assert kwargs["rule_context"]["rule_id"] == "ask"
    assert kwargs["workflow_id"] == "wf-1"
    decision["approved"] = True
    released = await mcp.call_tool("save_note", {"note": "x"})
    assert IBAN in result_text(released)


@pytest.mark.asyncio
async def test_pii_bindings_reach_access_rule_evaluation(
    proxied, monkeypatch, mocker
) -> None:
    mcp, _client, evaluate, _approval = proxied
    mocker.patch.object(policy_evaluator, "_log_policy_decision_async")
    mocker.patch.object(tool_policy, "schedule_policy_notice")
    _install_policy(monkeypatch, _config(_rule(action="notify")))
    await mcp.call_tool("save_note", {"note": f"card {CARD}"})
    bindings = evaluate.call_args.kwargs["extra_bindings"]
    assert bindings["pii"]["found"] is True
    assert bindings["pii"]["types_found"] == ["credit_card"]
    assert bindings["pii"]["paths"] == ["note"]


@pytest.mark.asyncio
async def test_no_rules_means_no_detector_and_no_bindings(
    proxied, monkeypatch, mocker
) -> None:
    mcp, client, evaluate, _approval = proxied
    detect = mocker.spy(tool_policy, "detect")
    _install_policy(monkeypatch, None)
    await mcp.call_tool("save_note", {"note": f"card {CARD}"})
    detect.assert_not_called()
    client.call_tool.assert_awaited_once()
    assert evaluate.call_args.kwargs["extra_bindings"] is None


@pytest.mark.asyncio
async def test_policy_load_failure_fails_closed(proxied, monkeypatch) -> None:
    mcp, client, _evaluate, _approval = proxied

    def boom(account_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp._load_sensitive_data_policy", boom
    )
    result = await mcp.call_tool("save_note", {"note": "x"})
    assert result.is_error
    client.call_tool.assert_not_called()


@pytest.mark.asyncio
async def test_non_dict_stored_block_fails_closed(proxied, monkeypatch, mocker) -> None:
    """A stored list is not an empty policy: the tool must not run unscanned."""
    from preloop.services import dynamic_fastmcp

    account = MagicMock()
    account.meta_data = {"sensitive_data": ["not-a-block"]}
    mocker.patch("preloop.models.crud.crud_account.get", return_value=account)
    monkeypatch.setattr(dynamic_fastmcp, "get_db", lambda: iter([MagicMock()]))
    mcp, client, _evaluate, _approval = proxied
    result = await mcp.call_tool("save_note", {"note": "x"})
    assert result.is_error
    assert "could not be loaded" in result.content[0].text
    client.call_tool.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_stored_block_fails_closed(
    proxied, monkeypatch, mocker
) -> None:
    """A block that cannot be parsed must refuse the call, not run unscanned."""
    from preloop.services import dynamic_fastmcp

    account = MagicMock()
    account.meta_data = {"sensitive_data": {"rules": [{"id": "r", "on": ["nowhere"]}]}}
    mocker.patch("preloop.models.crud.crud_account.get", return_value=account)
    monkeypatch.setattr(dynamic_fastmcp, "get_db", lambda: iter([MagicMock()]))
    mcp, client, _evaluate, _approval = proxied
    result = await mcp.call_tool("save_note", {"note": "x"})
    assert result.is_error
    assert "could not be loaded" in result.content[0].text
    client.call_tool.assert_not_called()


def test_db_error_reading_the_block_fails_closed(mocker) -> None:
    from preloop.services import dynamic_fastmcp
    from preloop.services.sensitive_data.policy_store import SensitiveDataPolicyError

    mocker.patch.object(dynamic_fastmcp, "get_db", lambda: iter([MagicMock()]))
    mocker.patch(
        "preloop.models.crud.crud_account.get", side_effect=RuntimeError("down")
    )
    with pytest.raises(SensitiveDataPolicyError):
        dynamic_fastmcp._load_sensitive_data_policy("acc")


def test_load_sensitive_data_policy_returns_none_without_tool_rules(mocker) -> None:
    from preloop.services import dynamic_fastmcp

    mocker.patch.object(dynamic_fastmcp, "get_db", lambda: iter([MagicMock()]))
    mocker.patch(
        "preloop.services.sensitive_data.policy_store.load_sensitive_data_config",
        return_value=_config(_rule(on=["model.request"])),
    )
    assert dynamic_fastmcp._load_sensitive_data_policy("acc") == (None, None)
    mocker.patch(
        "preloop.services.sensitive_data.policy_store.load_sensitive_data_config",
        return_value=_config(_rule()),
    )
    config, detectors = dynamic_fastmcp._load_sensitive_data_policy("acc")
    assert config.rules[0].id == "block-cards"
    assert isinstance(detectors, DetectorConfig)
