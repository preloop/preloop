"""The ``notify`` action on model I/O rules (#959).

A notify match must never block, park or buffer a call. It records a hit,
writes an audit row with action ``notify``, and leaves later deny and
require_approval rules to behave as they always have.
"""

from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from preloop.services import model_content_policy as mcp
from preloop.services.model_content_policy import (
    ModelIODecision,
    _apply_decision,
    evaluate_model_io,
    is_notify_only,
    wrap_stream_for_response_policy,
)
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.policy.schema import (
    ConditionAction,
    ModelIORule,
    ToolCondition,
    ToolDefinition,
)

ACCOUNT = uuid4()
USER = uuid4()


def _rule(**kwargs) -> ModelIORule:
    defaults = {
        "id": "notify-secret",
        "target": "model.request",
        "conditions": [
            ToolCondition(
                expression="request.text.contains('project-x')", action="notify"
            )
        ],
    }
    defaults.update(kwargs)
    return ModelIORule.model_validate(defaults)


@pytest.fixture
def captured():
    """Capture scheduled notices and audit rows instead of writing them."""
    notices = []
    audits = []
    with (
        patch.object(mcp, "schedule_policy_notice", side_effect=notices.append),
        patch.object(
            mcp,
            "_log_policy_decision_async",
            side_effect=lambda **kw: audits.append(kw),
        ),
    ):
        yield SimpleNamespace(notices=notices, audits=audits)


def _evaluate(rules, text, target="model.request"):
    return evaluate_model_io(
        rules=rules,
        target=target,
        text=text,
        account_id=ACCOUNT,
        user_id=USER,
    )


def test_notify_action_is_a_condition_action() -> None:
    assert ConditionAction("notify") is ConditionAction.NOTIFY


def test_notify_rule_validates_without_workflow() -> None:
    rule = _rule()
    assert rule.approval_workflow is None
    assert rule.conditions[0].action == "notify"


def test_tool_condition_rejects_notify() -> None:
    with pytest.raises(ValidationError, match="do not support action 'notify'"):
        ToolDefinition.model_validate(
            {
                "name": "bash",
                "source": "builtin",
                "conditions": [{"expression": "true", "action": "notify"}],
            }
        )


def test_notify_match_proceeds_and_records_one_hit(captured) -> None:
    text = "Please summarise project-x for the board."
    decision = _evaluate([_rule()], text)

    assert decision.action == "notify"
    assert decision.rule_id == "notify-secret"
    assert len(captured.notices) == 1
    notice = captured.notices[0]
    assert notice.account_id == ACCOUNT
    assert notice.user_id == USER
    assert notice.target == "model.request"
    assert notice.rule_id == "notify-secret"
    assert notice.text_sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert "project-x" in (notice.excerpt or "")


def test_notify_writes_audit_row_with_rule_and_hash(captured) -> None:
    text = "project-x"
    _evaluate([_rule()], text)

    assert len(captured.audits) == 1
    row = captured.audits[0]
    assert row["action"] == "notify"
    assert row["condition_matched"] == "notify-secret"
    assert row["tool_args"] == {"text_sha256": hashlib.sha256(b"project-x").hexdigest()}
    assert row["extra_details"]["rule_id"] == "notify-secret"
    assert "project-x" not in repr(row)


def test_repeat_calls_record_repeat_hits(captured) -> None:
    for _ in range(3):
        _evaluate([_rule()], "project-x again")
    assert len(captured.notices) == 3


def test_no_match_records_nothing(captured) -> None:
    decision = _evaluate([_rule()], "unrelated text")
    assert decision.action == "allow"
    assert captured.notices == []
    assert captured.audits == []


def test_one_hit_per_rule_even_when_several_conditions_match(captured) -> None:
    rule = _rule(
        conditions=[
            ToolCondition(
                expression="request.text.contains('project')", action="notify"
            ),
            ToolCondition(expression="request.text.contains('-x')", action="notify"),
        ]
    )
    _evaluate([rule], "project-x")
    assert len(captured.notices) == 1


def test_later_deny_still_denies_and_notice_is_kept(captured) -> None:
    rules = [
        _rule(),
        _rule(
            id="deny-x",
            conditions=[
                ToolCondition(expression="request.text.contains('x')", action="deny")
            ],
        ),
    ]
    decision = _evaluate(rules, "project-x")

    assert decision.action == "deny"
    assert decision.rule_id == "deny-x"
    assert [n.rule_id for n in decision.notices] == ["notify-secret"]
    assert sorted(a["action"] for a in captured.audits) == ["deny", "notify"]


def test_later_require_approval_still_requires_approval(captured) -> None:
    rules = [
        _rule(),
        _rule(
            id="hold-x",
            approval_workflow="Default",
            conditions=[
                ToolCondition(
                    expression="request.text.contains('x')",
                    action="require_approval",
                )
            ],
        ),
    ]
    decision = _evaluate(rules, "project-x")
    assert decision.action == "require_approval"
    assert decision.rule_id == "hold-x"
    assert len(captured.notices) == 1


def test_earlier_deny_is_unchanged_and_later_notify_does_not_fire(captured) -> None:
    rules = [
        _rule(
            id="deny-x",
            conditions=[
                ToolCondition(expression="request.text.contains('x')", action="deny")
            ],
        ),
        _rule(),
    ]
    decision = _evaluate(rules, "project-x")
    assert decision.action == "deny"
    assert captured.notices == []


def test_several_notify_rules_each_record_a_hit(captured) -> None:
    rules = [_rule(), _rule(id="notify-board")]
    decision = _evaluate(rules, "project-x")
    assert decision.action == "notify"
    assert [n.rule_id for n in captured.notices] == ["notify-secret", "notify-board"]


def test_notify_condition_error_does_not_block(captured) -> None:
    rule = _rule(
        conditions=[
            ToolCondition(
                expression="request.text.nope(",
                condition_type="cel",
                action="notify",
            )
        ]
    )
    decision = _evaluate([rule], "project-x")
    assert decision.action == "allow"
    assert captured.notices == []


def test_notify_only_rule_detector_timeout_does_not_block(captured) -> None:
    rule = _rule(
        detectors={"pii": True},
        detector_timeout_ms=50,
        on_detector_timeout="deny",
    )

    def slow(*_args, **_kwargs):
        time.sleep(0.3)
        raise AssertionError("unreachable")

    with patch.object(mcp, "_run_detectors", side_effect=slow):
        decision = _evaluate([rule], "project-x")
    assert decision.action == "allow"
    assert captured.notices == []


def test_notice_needs_an_account(captured) -> None:
    """Without an account there is nowhere to record a hit; the call proceeds."""
    decision = evaluate_model_io(
        rules=[_rule()], target="model.request", text="project-x"
    )
    assert decision.action == "allow"
    assert captured.notices == []


def test_scheduling_failure_does_not_fail_evaluation() -> None:
    with (
        patch(
            "preloop.services.db_executor.submit_off_loop",
            side_effect=RuntimeError("pool gone"),
        ),
        patch.object(mcp, "_log_policy_decision_async"),
    ):
        decision = _evaluate([_rule()], "project-x")
    assert decision.action == "notify"


def test_apply_decision_notify_proceeds_without_hold() -> None:
    gateway = SimpleNamespace(db=MagicMock())
    with patch.object(mcp, "hold_for_model_io_approval") as hold:
        _apply_decision(
            gateway=gateway,
            decision=ModelIODecision(action="notify", rule_id="notify-secret"),
            target="model.request",
            provider="openai",
            before_provider=True,
        )
    hold.assert_not_called()


def test_apply_decision_deny_still_raises() -> None:
    with pytest.raises(ModelGatewayAPIError):
        _apply_decision(
            gateway=SimpleNamespace(db=MagicMock()),
            decision=ModelIODecision(action="deny", rule_id="deny-x"),
            target="model.request",
            provider="openai",
            before_provider=True,
        )


def test_is_notify_only() -> None:
    assert is_notify_only(_rule())
    mixed = _rule(
        conditions=[
            ToolCondition(expression="true", action="notify"),
            ToolCondition(expression="true", action="deny"),
        ]
    )
    assert not is_notify_only(mixed)


def _gateway():
    return SimpleNamespace(
        db=MagicMock(),
        auth_context=SimpleNamespace(
            account_id=ACCOUNT,
            user=SimpleNamespace(id=USER),
        ),
        _openai_stream_error_event=lambda exc, _err: f"data: {exc.message}\n\n",
        _sse_done=lambda: "data: [DONE]\n\n",
        _client_session_id=None,
        _resolved_runtime_session_id=None,
    )


def test_notify_only_stream_is_not_buffered(captured) -> None:
    rule = _rule(
        target="model.response",
        conditions=[
            ToolCondition(
                expression="response.text.contains('project-x')", action="notify"
            )
        ],
    )
    pulled = []

    def upstream():
        for chunk in ("Talking about ", "project-x", " today"):
            pulled.append(chunk)
            yield f'data: {{"choices":[{{"delta":{{"content":"{chunk}"}}}}]}}\n\n'
        yield "data: [DONE]\n\n"

    with patch.object(mcp, "load_model_io_rules", return_value=[rule]):
        stream = wrap_stream_for_response_policy(
            upstream(),
            gateway=_gateway(),
            payload={},
            ai_model=None,
            provider="openai",
        )
        first = next(stream)
        # The first event reached the client before the rest was pulled.
        assert pulled == ["Talking about "]
        assert "Talking about" in first
        rest = list(stream)

    assert len(rest) == 3
    assert len(captured.notices) == 1
    assert captured.notices[0].account_id == ACCOUNT
    assert captured.notices[0].target == "model.response"
    assert "project-x" in (captured.notices[0].excerpt or "")


def test_notify_only_stream_closed_early_still_evaluates(captured) -> None:
    rule = _rule(
        target="model.response",
        conditions=[
            ToolCondition(
                expression="response.text.contains('project-x')", action="notify"
            )
        ],
    )
    events = [
        'data: {"choices":[{"delta":{"content":"project-x"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":" more"}}]}\n\n',
    ]
    with patch.object(mcp, "load_model_io_rules", return_value=[rule]):
        stream = wrap_stream_for_response_policy(
            iter(events),
            gateway=_gateway(),
            payload={},
            ai_model=None,
            provider="openai",
        )
        next(stream)
        stream.close()
    assert len(captured.notices) == 1


def test_mixed_response_rules_still_buffer_and_deny(captured) -> None:
    rules = [
        _rule(
            target="model.response",
            conditions=[
                ToolCondition(
                    expression="response.text.contains('project-x')", action="notify"
                )
            ],
        ),
        _rule(
            id="deny-out",
            target="model.response",
            conditions=[
                ToolCondition(
                    expression="response.text.contains('project-x')", action="deny"
                )
            ],
        ),
    ]
    events = [
        'data: {"choices":[{"delta":{"content":"project-x"}}]}\n\n',
        "data: [DONE]\n\n",
    ]
    with patch.object(mcp, "load_model_io_rules", return_value=rules):
        out = list(
            wrap_stream_for_response_policy(
                iter(events),
                gateway=_gateway(),
                payload={},
                ai_model=None,
                provider="openai",
            )
        )
    assert all("project-x" not in event for event in out)
    assert len(captured.notices) == 1
