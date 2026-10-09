"""Keep complete documentation policies valid and behaviorally meaningful."""

import re
from pathlib import Path

import pytest
import yaml

from preloop.services.policy import PolicyDocument
from preloop.services.sensitive_data.detectors import detect, types_found
from preloop.services.sensitive_data.policy_store import detector_config_from
from preloop.services.sensitive_data.redact import redact_text

ROOT = Path(__file__).resolve().parents[3]
GUIDE = ROOT / "docs/guide/sensitive-data.md"
EXAMPLES = ROOT / "docs/guide/examples/sensitive-data"


def documented_policies() -> list[dict]:
    """Load every YAML snippet; partial unchecked blocks are not allowed."""
    return [
        yaml.safe_load(text)
        for text in re.findall(r"```yaml\n(.*?)```", GUIDE.read_text(), re.DOTALL)
    ]


def test_every_guide_yaml_snippet_is_a_complete_policy() -> None:
    examples = documented_policies()
    assert len(examples) == 2
    for example in examples:
        policy = PolicyDocument.model_validate(example)
        assert policy.sensitive_data
        assert policy.sensitive_data.rules
        assert policy.sensitive_data.reference_only
    assert examples == [
        yaml.safe_load((EXAMPLES / name).read_text())
        for name in ["patient-records.yaml", "payments.yaml"]
    ]


@pytest.mark.parametrize(
    "name,text,expected",
    [
        (
            "patient-records.yaml",
            "DOB: 1990-02-03; MRN: EX-1042",
            {"date_of_birth", "medical_record_number"},
        ),
        (
            "payments.yaml",
            "4111 1111 1111 1111; DE89 3704 0044 0532 0130 00",
            {"credit_card", "iban"},
        ),
    ],
)
def test_synthetic_samples_match_documented_types(
    name: str, text: str, expected: set[str]
) -> None:
    policy = PolicyDocument.model_validate(
        yaml.safe_load((EXAMPLES / name).read_text())
    )
    config = detector_config_from(policy.sensitive_data)
    assert set(types_found(detect(text, config))) == expected
    redacted, counts = redact_text(text, config)
    assert set(counts) == expected
    for kind in expected:
        assert f"[REDACTED:{kind}]" in redacted


def test_guide_screenshots_are_local_nonempty_assets() -> None:
    images = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", GUIDE.read_text())
    assert len(images) == 2
    for image in images:
        path = GUIDE.parent / image
        assert path.is_file()
        assert path.read_bytes().startswith(b"\x89PNG")


def test_result_hash_check_request_example_uses_the_accepted_field() -> None:
    """Result fingerprints travel in the legacy args_hmac request field."""
    import json
    from preloop.api.endpoints.policies import SensitiveDataHashCheckRequest

    snippets = re.findall(r"```json\n(.*?)```", GUIDE.read_text(), re.DOTALL)
    assert len(snippets) == 1
    request = SensitiveDataHashCheckRequest.model_validate(json.loads(snippets[0]))
    assert request.args_hmac
    assert "result_hmac" not in json.loads(snippets[0])
