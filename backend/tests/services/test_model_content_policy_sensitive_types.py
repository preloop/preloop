"""Model I/O rules over the shared detector library (#1121).

Covers the widened type list, the ``pii.count`` binding and account custom
patterns reaching the evaluator through ``detector_config``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from preloop.services.model_content_policy import (
    _load_gateway_policy_rules,
    enforce_request_policy,
    evaluate_model_io,
)
from preloop.services.policy.schema import (
    ModelIORule,
    SensitiveDataConfig,
    ToolCondition,
)
from preloop.services.sensitive_data.detectors import CustomPattern, DetectorConfig

EMPLOYEE_ID_BLOCK = SensitiveDataConfig.model_validate(
    {"detectors": {"custom_patterns": [{"name": "employee_id", "regex": r"EMP-\d{6}"}]}}
)


def _rule(**kwargs) -> ModelIORule:
    base = {
        "id": "rule",
        "target": "model.request",
        "conditions": [ToolCondition(expression="pii.found == true", action="deny")],
    }
    base.update(kwargs)
    return ModelIORule(**base)


def test_iban_rule_denies_and_reports_count() -> None:
    rule = _rule(detectors={"pii": {"types": ["iban", "email"]}})
    decision = evaluate_model_io(
        rules=[rule],
        target="model.request",
        text="pay DE89 3704 0044 0532 0130 00 and mail alice@example.com",
    )
    assert decision.action == "deny"
    assert decision.detector_summary["pii.types_found"] == ["iban", "email"]
    assert decision.detector_summary["pii.count"] == 2


def test_pii_count_binding_is_usable_in_conditions() -> None:
    rule = _rule(
        detectors={"pii": {"types": ["email"]}},
        conditions=[ToolCondition(expression="pii.count >= 2", action="deny")],
    )
    one = evaluate_model_io(rules=[rule], target="model.request", text="a@example.com")
    two = evaluate_model_io(
        rules=[rule], target="model.request", text="a@example.com b@example.com"
    )
    assert one.action == "allow"
    assert two.action == "deny"


def test_default_pii_types_are_unchanged_for_old_rules() -> None:
    """A rule without an explicit list still scans only email, phone, card."""
    rule = _rule(detectors={"pii": True})
    decision = evaluate_model_io(
        rules=[rule],
        target="model.request",
        text="IBAN DE89 3704 0044 0532 0130 00 only",
    )
    assert decision.action == "allow"


def test_custom_pattern_from_detector_config_reaches_the_rule() -> None:
    rule = _rule(detectors={"pii": {"types": ["employee_id"]}})
    config = DetectorConfig(
        custom_patterns=(CustomPattern("employee_id", r"EMP-\d{6}"),)
    )
    without = evaluate_model_io(rules=[rule], target="model.request", text="EMP-123456")
    with_config = evaluate_model_io(
        rules=[rule], target="model.request", text="EMP-123456", detector_config=config
    )
    assert without.action == "allow"
    assert with_config.action == "deny"
    assert with_config.detector_summary["pii.types_found"] == ["employee_id"]


def _gateway(rules):
    gateway = MagicMock()
    gateway.db = MagicMock()
    gateway.auth_context.account_id = "acc"
    gateway.auth_context.user.id = None
    gateway.release_db_for_wait = MagicMock()
    return gateway


def test_gateway_loads_detector_config_before_releasing_db(mocker) -> None:
    rules = [_rule(detectors={"pii": {"types": ["employee_id"]}})]
    mocker.patch(
        "preloop.services.model_content_policy.load_model_io_rules", return_value=rules
    )
    loader = mocker.patch(
        "preloop.services.model_content_policy.load_sensitive_data_config",
        return_value=EMPLOYEE_ID_BLOCK,
    )
    gateway = _gateway(rules)
    loaded = _load_gateway_policy_rules(gateway, ai_model=None, provider="openai")
    assert loaded == rules
    loader.assert_called_once_with(gateway.db, "acc")
    gateway.release_db_for_wait.assert_called_once()
    config = gateway._sensitive_detector_config
    assert isinstance(config, DetectorConfig)
    assert config.custom_names() == ["employee_id"]


def test_gateway_parks_no_config_when_no_rules(mocker) -> None:
    mocker.patch(
        "preloop.services.model_content_policy.load_model_io_rules", return_value=[]
    )
    mocker.patch(
        "preloop.services.model_content_policy.load_sensitive_data_config",
        return_value=EMPLOYEE_ID_BLOCK,
    )
    gateway = _gateway([])
    assert _load_gateway_policy_rules(gateway, ai_model=None, provider="openai") == []
    assert gateway._sensitive_detector_config is None


def test_enforce_request_policy_uses_custom_pattern(mocker) -> None:
    rules = [_rule(detectors={"pii": {"types": ["employee_id"]}})]
    mocker.patch(
        "preloop.services.model_content_policy.load_model_io_rules", return_value=rules
    )
    mocker.patch(
        "preloop.services.model_content_policy.load_sensitive_data_config",
        return_value=EMPLOYEE_ID_BLOCK,
    )
    gateway = _gateway(rules)
    import pytest

    from preloop.services.model_gateway_errors import ModelGatewayAPIError

    with pytest.raises(ModelGatewayAPIError):
        enforce_request_policy(
            gateway,
            payload={},
            ai_model=None,
            messages=[{"role": "user", "content": "badge EMP-123456"}],
            provider="openai",
        )
