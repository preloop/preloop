"""Tests for the sensitive-data types and test endpoints (#1121)."""

from __future__ import annotations

import inspect
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from preloop.api.endpoints import policies
from preloop.services.policy import SensitiveDataConfig
from preloop.services.policy.schema import ModelIORule

pytestmark = pytest.mark.asyncio


@pytest.fixture
def account():
    item = MagicMock()
    item.id = uuid.uuid4()
    return item


@pytest.fixture
def user():
    item = MagicMock()
    item.id = uuid.uuid4()
    return item


def test_handlers_declare_current_user_and_db() -> None:
    for handler in (
        policies.list_sensitive_data_types,
        policies.test_sensitive_data_detectors,
    ):
        params = inspect.signature(handler).parameters
        assert "current_user" in params and "db" in params, handler.__name__


def test_types_endpoint_lists_builtins_and_account_custom_entries(
    account, user, mocker
) -> None:
    mocker.patch.object(
        policies,
        "load_sensitive_data_config",
        return_value=SensitiveDataConfig.model_validate(
            {
                "detectors": {
                    "custom_patterns": [{"name": "employee_id", "regex": r"EMP-\d+"}],
                    "keywords": [{"name": "codes", "terms": ["Phoenix"]}],
                }
            }
        ),
    )
    response = policies.list_sensitive_data_types(
        account=account, current_user=user, db=MagicMock()
    )
    ids = [item.id for item in response.types]
    assert ids[:3] == ["email", "phone", "credit_card"]
    assert "national_id" in ids and "employee_id" in ids and "codes" in ids
    national = next(item for item in response.types if item.id == "national_id")
    assert national.locales == ["us", "de", "uk", "fr", "nl"]
    assert national.example and national.description and national.checksum
    assert response.default_types == ["email", "phone", "credit_card"]


def test_test_endpoint_returns_spans_and_never_logs_the_input(
    account, user, mocker, caplog
) -> None:
    mocker.patch.object(
        policies, "load_sensitive_data_config", return_value=SensitiveDataConfig()
    )
    secret_text = "mail alice@example.com, IBAN DE89 3704 0044 0532 0130 00"
    with caplog.at_level("DEBUG"):
        response = policies.test_sensitive_data_detectors(
            policies.SensitiveDataTestRequest(text=secret_text),
            account=account,
            current_user=user,
            db=MagicMock(),
        )
    assert response.types_found == ["email", "iban"]
    assert response.count == 2
    assert response.redacted_preview is None
    first = response.matches[0]
    assert secret_text[first.start : first.end] == "alice@example.com"
    assert "alice@example.com" not in caplog.text


def test_test_endpoint_uses_submitted_config_and_types(account, user, mocker) -> None:
    loader = mocker.patch.object(policies, "load_sensitive_data_config")
    response = policies.test_sensitive_data_detectors(
        policies.SensitiveDataTestRequest(
            text="badge EMP-123456 for alice@example.com",
            types=["employee_id"],
            config={
                "custom_patterns": [{"name": "employee_id", "regex": r"EMP-\d{6}"}]
            },
        ),
        account=account,
        current_user=user,
        db=MagicMock(),
    )
    assert response.types_found == ["employee_id"]
    loader.assert_not_called()


def test_test_endpoint_rejects_malformed_type_names(account, user, mocker) -> None:
    mocker.patch.object(
        policies, "load_sensitive_data_config", return_value=SensitiveDataConfig()
    )
    with pytest.raises(HTTPException) as exc_info:
        policies.test_sensitive_data_detectors(
            policies.SensitiveDataTestRequest(text="x", types=["Not Valid"]),
            account=account,
            current_user=user,
            db=MagicMock(),
        )
    assert exc_info.value.status_code == 422


def test_create_model_io_rule_rejects_unknown_custom_type(
    account, user, mocker
) -> None:
    mocker.patch.object(
        policies, "load_sensitive_data_config", return_value=SensitiveDataConfig()
    )
    upsert = mocker.patch.object(policies, "upsert_model_io_rule")
    rule = ModelIORule.model_validate(
        {
            "id": "r",
            "target": "model.request",
            "detectors": {"pii": {"types": ["employee_id"]}},
            "conditions": [{"expression": "pii.found == true", "action": "deny"}],
        }
    )
    with pytest.raises(HTTPException) as exc_info:
        policies.create_model_io_rule(
            rule, account=account, current_user=user, db=MagicMock()
        )
    assert exc_info.value.status_code == 422
    assert "employee_id" in exc_info.value.detail
    upsert.assert_not_called()


def test_create_model_io_rule_accepts_declared_custom_type(
    account, user, mocker
) -> None:
    mocker.patch.object(
        policies,
        "load_sensitive_data_config",
        return_value=SensitiveDataConfig.model_validate(
            {
                "detectors": {
                    "custom_patterns": [{"name": "employee_id", "regex": r"E\d"}]
                }
            }
        ),
    )
    rule = ModelIORule.model_validate(
        {
            "id": "r",
            "target": "model.request",
            "detectors": {"pii": {"types": ["employee_id", "iban"]}},
            "conditions": [{"expression": "pii.found == true", "action": "deny"}],
        }
    )
    upsert = mocker.patch.object(policies, "upsert_model_io_rule", return_value=rule)
    saved = policies.create_model_io_rule(
        rule, account=account, current_user=user, db=MagicMock()
    )
    assert saved["detectors"]["pii"]["types"] == ["employee_id", "iban"]
    upsert.assert_called_once()
