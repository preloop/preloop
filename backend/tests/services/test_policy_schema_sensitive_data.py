"""Schema and store tests for the ``sensitive_data`` policy block (#1121)."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
import yaml
from pydantic import ValidationError

from preloop.services.policy import (
    PolicyDocument,
    SensitiveDataConfig,
    compute_policy_diff,
    load_policy_from_string,
)
from preloop.services.policy.schema import (
    BUILTIN_SENSITIVE_TYPES,
    ModelIORule,
    PIIDetectorConfig,
    SensitiveDataDetectorsConfig,
)
from preloop.services.sensitive_data.detectors import BUILTIN_TYPE_IDS
from preloop.services.sensitive_data.policy_store import (
    SENSITIVE_DATA_META_KEY,
    detector_config_from,
    load_detector_config,
    load_sensitive_data_config,
    parse_sensitive_data_config,
    replace_sensitive_data_config,
)

BASE = {"version": "1.0", "metadata": {"name": "t"}}


def _doc(**extra) -> PolicyDocument:
    return PolicyDocument.model_validate({**BASE, **extra})


def test_old_yaml_with_email_only_loads_unchanged() -> None:
    policy, result = load_policy_from_string(
        """
version: "1.0"
metadata:
  name: legacy
model_io:
  - id: deny-email
    target: model.request
    detectors:
      pii:
        types: [email]
    conditions:
      - expression: "pii.found == true"
        action: deny
"""
    )
    assert result.is_valid, result.errors
    assert policy is not None
    assert policy.model_io[0].detectors.pii.types == ["email"]
    assert policy.sensitive_data is None


def test_pii_types_accept_every_builtin_type() -> None:
    assert BUILTIN_SENSITIVE_TYPES == BUILTIN_TYPE_IDS
    config = PIIDetectorConfig(types=list(BUILTIN_TYPE_IDS))
    assert config.types == list(BUILTIN_TYPE_IDS)


def test_pii_types_reject_malformed_names() -> None:
    with pytest.raises(ValidationError, match="Unknown PII types"):
        PIIDetectorConfig(types=["Email"])
    with pytest.raises(ValidationError, match="must not be empty"):
        PIIDetectorConfig(types=[])


def test_model_io_rule_may_use_custom_type_declared_in_sensitive_data() -> None:
    doc = _doc(
        model_io=[
            {
                "id": "r1",
                "target": "model.request",
                "detectors": {"pii": {"types": ["email", "employee_id", "codes"]}},
                "conditions": [{"expression": "pii.found == true", "action": "deny"}],
            }
        ],
        sensitive_data={
            "detectors": {
                "custom_patterns": [{"name": "employee_id", "regex": r"EMP-\d{6}"}],
                "keywords": [{"name": "codes", "terms": ["Phoenix"]}],
            }
        },
    )
    assert doc.sensitive_data.known_types()[-2:] == ["employee_id", "codes"]


def test_model_io_rule_with_undeclared_custom_type_is_rejected() -> None:
    with pytest.raises(
        ValidationError, match="scans unknown PII types \\['employee_id'\\]"
    ):
        _doc(
            model_io=[
                {
                    "id": "r1",
                    "target": "model.request",
                    "detectors": {"pii": {"types": ["employee_id"]}},
                    "conditions": [
                        {"expression": "pii.found == true", "action": "deny"}
                    ],
                }
            ]
        )


def test_nested_quantifier_custom_regex_rejected_with_clear_error() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _doc(
            sensitive_data={
                "detectors": {
                    "custom_patterns": [{"name": "bad", "regex": "(a+)+"}],
                }
            }
        )
    message = str(exc_info.value)
    assert "custom_patterns[bad]" in message
    assert "nested quantifier" in message


def test_valid_custom_regex_round_trips_and_matches() -> None:
    doc = _doc(
        sensitive_data={
            "detectors": {
                "custom_patterns": [
                    {"name": "employee_id", "regex": r"EMP-\d{6}", "flags": ["i"]}
                ]
            }
        }
    )
    from preloop.services.sensitive_data.detectors import detect

    config = detector_config_from(doc.sensitive_data)
    (match,) = detect("badge emp-123456", config.with_types(["employee_id"]))
    assert match.type == "employee_id"


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ({"custom_patterns": [{"name": "email", "regex": "x"}]}, "built-in type"),
        ({"custom_patterns": [{"name": "Bad Name", "regex": "x"}]}, "must match"),
        ({"keywords": [{"name": "k", "terms": []}]}, "at least 1 item"),
        ({"locales": ["xx"]}, "Unknown national_id locales"),
        ({"types": ["nope"]}, "unknown types"),
        (
            {
                "custom_patterns": [{"name": "dup", "regex": "a"}],
                "keywords": [{"name": "dup", "terms": ["b"]}],
            },
            "Duplicate sensitive_data detector name",
        ),
        ({"medical_record_number_pattern": "(a+)+"}, "medical_record_number_pattern"),
    ],
)
def test_detector_block_validation_errors(block: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        SensitiveDataDetectorsConfig.model_validate(block)


def test_locales_are_normalised() -> None:
    config = SensitiveDataDetectorsConfig(locales=["DE", "nl", "de"])
    assert config.locales == ["de", "nl"]


def test_yaml_example_in_schema_docstring_loads() -> None:
    import preloop.services.policy.schema as schema_module

    doc_text = schema_module.__doc__
    example = doc_text.split("Example YAML:", 1)[1]
    example = "\n".join(line[4:] for line in example.splitlines())
    data = yaml.safe_load(example)
    doc = PolicyDocument.model_validate(data)
    assert doc.sensitive_data.detectors.locales == ["de", "nl"]
    assert "employee_id" in doc.model_io[0].detectors.pii.types


def test_sensitive_data_diff_is_reported_as_one_item() -> None:
    current = _doc()
    incoming = _doc(sensitive_data={"detectors": {"locales": ["de"]}})
    diff = compute_policy_diff(current, incoming)
    paths = [(item.path, item.operation) for item in diff.changes]
    assert ("$.sensitive_data", "add") in paths
    reverse = compute_policy_diff(incoming, current)
    assert ("$.sensitive_data", "remove") in [
        (item.path, item.operation) for item in reverse.changes
    ]


class TestPolicyStore:
    def _db_with_account(self, meta: dict | None):
        account = MagicMock()
        account.meta_data = meta
        db = MagicMock()
        from preloop.models.crud import crud_account

        return db, account, crud_account

    def test_parse_rejects_garbage_without_raising(self) -> None:
        assert parse_sensitive_data_config(None) == SensitiveDataConfig()
        assert parse_sensitive_data_config("nope") == SensitiveDataConfig()
        assert parse_sensitive_data_config({"detectors": {"locales": ["xx"]}}) == (
            SensitiveDataConfig()
        )

    def test_load_returns_empty_when_meta_is_not_a_dict(self, mocker) -> None:
        db, account, crud = self._db_with_account(MagicMock())
        mocker.patch.object(crud, "get", return_value=account)
        assert load_sensitive_data_config(db, uuid.uuid4()) == SensitiveDataConfig()
        assert load_detector_config(db, uuid.uuid4()).custom_patterns == ()

    def test_load_never_raises_on_db_error(self, mocker) -> None:
        db, _account, crud = self._db_with_account({})
        mocker.patch.object(crud, "get", side_effect=RuntimeError("down"))
        assert load_sensitive_data_config(db, uuid.uuid4()) == SensitiveDataConfig()

    def test_replace_round_trips_through_meta_data(self, mocker) -> None:
        db, account, crud = self._db_with_account({"other": 1})
        mocker.patch.object(crud, "get", return_value=account)
        config = SensitiveDataConfig.model_validate(
            {"detectors": {"custom_patterns": [{"name": "emp", "regex": r"E\d"}]}}
        )
        replace_sensitive_data_config(db, uuid.uuid4(), config)
        stored = account.meta_data[SENSITIVE_DATA_META_KEY]
        assert stored["detectors"]["custom_patterns"][0]["name"] == "emp"
        assert account.meta_data["other"] == 1
        assert load_sensitive_data_config(db, uuid.uuid4()) == config
        replace_sensitive_data_config(db, uuid.uuid4(), None)
        assert SENSITIVE_DATA_META_KEY not in account.meta_data

    def test_replace_unknown_account_raises(self, mocker) -> None:
        db, _account, crud = self._db_with_account({})
        mocker.patch.object(crud, "get", return_value=None)
        with pytest.raises(ValueError, match="not found"):
            replace_sensitive_data_config(db, uuid.uuid4(), SensitiveDataConfig())


def test_model_io_rule_standalone_accepts_custom_name_shape() -> None:
    """The API validates custom names against the account; the model only checks shape."""
    rule = ModelIORule.model_validate(
        {
            "id": "r",
            "target": "model.request",
            "detectors": {"pii": {"types": ["employee_id"]}},
            "conditions": [{"expression": "pii.found == true", "action": "deny"}],
        }
    )
    assert rule.detectors.pii.types == ["employee_id"]
