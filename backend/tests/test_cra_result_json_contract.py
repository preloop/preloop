"""CRA evidence-pack result.json contract.

Pins the four security-audit presets to the YAML "Required shape" blocks
as a contract: versioned schema ids, completion fields (status and/or
verdict), the honesty sentence, example artifacts that carry every
required key, and a ban on conformity/CE/Article-14-filed claims.

Deterministic: parses shipped YAML and fixtures only. No OSV/CISA, no
live control plane.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

PRESETS_DIR = Path(__file__).resolve().parents[1] / "presets"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "cra"
EVIDENCE_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "evidence"

HONESTY_LINE = (
    "Machine-generated evidence for conformity assessment support. "
    "Not a conformity assessment, certification, or legal advice."
)

# YAML Required shape is the spec. Schema JSON files must not invent keys.
CONTRACTS = (
    {
        "name": "SBOM Verify",
        "file": "004-sbom-verify.yaml",
        "schema_id": "preloop.cra.sbomaudit/v1",
        "example": "result-sbomaudit.json",
        "jsonschema": "schemas/sbomaudit-v1.json",
    },
    {
        "name": "SBOM Exploit Check",
        "file": "005-sbom-exploit-check.yaml",
        "schema_id": "preloop.cra.vulnscan/v1",
        "example": "result-vulnscan.json",
        "jsonschema": "schemas/vulnscan-v1.json",
    },
    {
        "name": "Release Security Audit",
        "file": "006-release-security-audit.yaml",
        "schema_id": "preloop.cra.releaseaudit/v1",
        "example": "result-releaseaudit.json",
        "jsonschema": "schemas/releaseaudit-v1.json",
    },
    {
        "name": "Component Due Diligence Record",
        "file": "007-component-due-diligence.yaml",
        "schema_id": "preloop.cra.duediligence/v1",
        "example": "result-duediligence.json",
        "jsonschema": "schemas/duediligence-v1.json",
    },
)

BANNED_TRUE_KEYS = ("compliant", "ce_mark")
BANNED_PHRASE = "article 14 filed"


def _norm(text: str) -> str:
    """Collapse whitespace so asserts survive YAML line wrapping."""
    return " ".join(text.split())


def _load_preset(filename: str) -> dict:
    path = PRESETS_DIR / filename
    assert path.exists(), f"Missing preset file: {path}"
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    return data


def _required_shape_block(prompt: str, schema_id: str) -> str:
    """Return the JSON-ish Required shape object for ``schema_id``."""
    marker = f"Required shape ({schema_id}):"
    idx = prompt.find(marker)
    assert idx != -1, f"prompt missing {marker!r}"
    brace = prompt.find("{", idx)
    assert brace != -1, f"no object after {marker!r}"
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(prompt[brace:], brace):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return prompt[brace : i + 1]
    raise AssertionError(f"unclosed Required shape for {schema_id}")


def _top_level_keys(block: str) -> list[str]:
    """Extract top-level object keys from a JSON-ish Required shape block."""
    keys: list[str] = []
    depth = 0
    in_str = False
    escape = False
    i = 0
    while i < len(block):
        ch = block[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            j = i + 1
            while j < len(block) and block[j] != '"':
                if block[j] == "\\":
                    j += 2
                    continue
                j += 1
            key = block[i + 1 : j]
            rest = block[j + 1 :].lstrip()
            if depth == 1 and rest.startswith(":"):
                keys.append(key)
            in_str = True
            i = j
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        i += 1
    assert keys, "Required shape had no top-level keys"
    return keys


def _assert_no_banned_claims(payload: object, source: str) -> None:
    """Banned conformity/CE/filing claims must not appear in the pack."""
    if isinstance(payload, dict):
        for key in BANNED_TRUE_KEYS:
            assert payload.get(key) is not True, (
                f"{source}: banned top-level {key!r}: true"
            )
        for value in payload.values():
            _assert_no_banned_claims(value, source)
    elif isinstance(payload, list):
        for item in payload:
            _assert_no_banned_claims(item, source)
    elif isinstance(payload, str):
        assert BANNED_PHRASE not in payload.lower(), (
            f"{source}: banned phrase {BANNED_PHRASE!r}"
        )


@pytest.fixture(params=CONTRACTS, ids=lambda c: c["schema_id"])
def contract(request) -> dict:
    return request.param


class TestPresetPromptContract:
    """Each preset prompt still requires the YAML result.json contract."""

    def test_prompt_requires_schema_status_verdict_and_honesty(self, contract):
        data = _load_preset(contract["file"])
        prompt = data["prompt_template"]
        schema_id = contract["schema_id"]
        norm = _norm(prompt)

        assert "/workspace/result.json" in prompt
        assert schema_id in prompt
        assert HONESTY_LINE in norm

        block = _required_shape_block(prompt, schema_id)
        keys = _top_level_keys(block)
        assert "schema" in keys
        assert f'"schema": "{schema_id}"' in _norm(block)

        # Completion fields: whichever the YAML shape names (vulnscan has
        # status and no verdict; sbomaudit/releaseaudit have verdict and
        # no status; due diligence has both).
        assert "status" in keys or "verdict" in keys
        if "status" in keys:
            assert re.search(r'"status":\s*"success"\s*\|\s*"error"', norm)
        if "verdict" in keys:
            assert '"verdict"' in block

    def test_honesty_line_is_the_disclaimer_field(self, contract):
        prompt = _load_preset(contract["file"])["prompt_template"]
        block = _required_shape_block(prompt, contract["schema_id"])
        assert HONESTY_LINE in _norm(block)


class TestExampleResultJson:
    """Example artifacts carry every YAML-required key and validate."""

    def test_example_has_every_required_key(self, contract):
        prompt = _load_preset(contract["file"])["prompt_template"]
        required = _top_level_keys(_required_shape_block(prompt, contract["schema_id"]))
        example = json.loads((FIXTURES_DIR / contract["example"]).read_text())
        missing = [key for key in required if key not in example]
        assert missing == [], f"{contract['example']} missing required keys: {missing}"
        assert example["schema"] == contract["schema_id"]
        assert example["disclaimer"] == HONESTY_LINE

    def test_example_validates_against_json_schema(self, contract):
        schema = json.loads((FIXTURES_DIR / contract["jsonschema"]).read_text())
        example = json.loads((FIXTURES_DIR / contract["example"]).read_text())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(example)

    def test_json_schema_required_matches_yaml_shape(self, contract):
        """Schema fixtures stay small: required == YAML keys, no extras."""
        prompt = _load_preset(contract["file"])["prompt_template"]
        yaml_keys = _top_level_keys(
            _required_shape_block(prompt, contract["schema_id"])
        )
        schema = json.loads((FIXTURES_DIR / contract["jsonschema"]).read_text())
        assert schema["required"] == yaml_keys
        # Properties may only name keys the YAML names (plus no invented ones).
        extra = set(schema["properties"]) - set(yaml_keys)
        assert extra == set(), f"schema invents fields YAML does not name: {extra}"

    def test_example_is_synthetic(self, contract):
        text = (FIXTURES_DIR / contract["example"]).read_text()
        assert "synthetic fixture" in text
        assert "example" in text.lower()


class TestBannedClaims:
    """The pack must not emit a conformity, CE-mark, or filing claim."""

    def test_preset_yaml_has_no_banned_claims(self, contract):
        text = (PRESETS_DIR / contract["file"]).read_text()
        lowered = text.lower()
        assert '"compliant": true' not in lowered
        assert '"ce_mark": true' not in lowered
        assert BANNED_PHRASE not in lowered
        data = yaml.safe_load(text)
        _assert_no_banned_claims(data, contract["file"])

    def test_example_result_has_no_banned_claims(self, contract):
        example = json.loads((FIXTURES_DIR / contract["example"]).read_text())
        _assert_no_banned_claims(example, contract["example"])
        dumped = json.dumps(example).lower()
        assert '"compliant": true' not in dumped
        assert '"ce_mark": true' not in dumped
        assert BANNED_PHRASE not in dumped

    def test_json_schema_does_not_name_banned_fields(self, contract):
        schema = json.loads((FIXTURES_DIR / contract["jsonschema"]).read_text())
        named = set(schema.get("properties", {})) | set(schema.get("required", []))
        assert "compliant" not in named
        assert "ce_mark" not in named

    def test_existing_evidence_fixtures_have_no_banned_claims(self):
        for path in sorted(EVIDENCE_FIXTURES_DIR.glob("*.json")):
            payload = json.loads(path.read_text())
            _assert_no_banned_claims(payload, path.name)
            dumped = json.dumps(payload).lower()
            assert '"compliant": true' not in dumped
            assert '"ce_mark": true' not in dumped
            assert BANNED_PHRASE not in dumped


class TestDueDiligenceRecordFixtureStatus:
    """The older due-diligence fixture must carry the YAML status key."""

    def test_legacy_fixture_includes_required_status(self):
        record = json.loads(
            (EVIDENCE_FIXTURES_DIR / "due-diligence-record.json").read_text()
        )
        prompt = _load_preset("007-component-due-diligence.yaml")["prompt_template"]
        required = _top_level_keys(
            _required_shape_block(prompt, "preloop.cra.duediligence/v1")
        )
        assert "status" in required
        assert record["status"] in {"success", "error"}
        missing = [key for key in required if key not in record]
        assert missing == [], f"legacy fixture missing required keys: {missing}"
