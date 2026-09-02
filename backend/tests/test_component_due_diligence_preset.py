"""Tests for the Component Due Diligence Record preset.

The preset splits the CRA-style component due-diligence work: the agent
does the legwork (docs, CVE history, maintenance signals, CE declaration
presence), a HUMAN carries the risk decision through the builtin
``request_approval`` tool, and the decision plus evidence land as a dated
record in the product's compliance repo. These tests pin the invariants
of that split and the synthetic record fixture documenting the output.
"""

import json
from pathlib import Path

import pytest
import yaml

PRESETS_DIR = Path(__file__).resolve().parents[1] / "presets"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "evidence"

PRESET_FILE = "007-component-due-diligence.yaml"
SCHEMA_ID = "preloop.cra.duediligence/v1"

DISCLAIMER = "Machine-generated evidence for conformity assessment support. Not a"


def _norm(text: str) -> str:
    """Collapse whitespace so asserts survive YAML line wrapping."""
    return " ".join(text.split())


@pytest.fixture(scope="module")
def preset() -> dict:
    data = yaml.safe_load((PRESETS_DIR / PRESET_FILE).read_text())
    assert isinstance(data, dict)
    return data


class TestPresetInvariants:
    def test_identity(self, preset):
        assert preset["name"] == "Component Due Diligence Record"
        assert preset["is_preset"] is True
        assert preset.get("description")
        assert preset.get("icon")

    def test_trigger_agnostic(self, preset):
        assert preset["git_clone_config"] is None
        assert preset["trigger_config"] is None
        assert preset["trigger_event_source"] is None
        assert preset["trigger_event_types"] is None

    def test_agent_config(self, preset):
        assert preset["agent_type"] == "codex"
        assert preset["agent_config"]["sandbox_type"] == "exec"
        assert preset["agent_config"]["enable_auto_lint"] is False

    def test_only_write_tool_is_request_approval(self, preset):
        """The single external mutation is asking a human for the decision."""
        assert preset["allowed_mcp_servers"] == []
        assert preset["allowed_mcp_tools"] == [{"name": "request_approval"}]

    def test_result_contract(self, preset):
        prompt = preset["prompt_template"]
        assert "/workspace/result.json" in prompt
        assert SCHEMA_ID in prompt
        assert "{{trigger_event.payload}}" in prompt

    def test_completion_status_alongside_verdict(self, preset):
        """The verdict vocabulary (recorded|error) is deliberately NOT a flow
        completion signal; a required top-level "status" field carries the
        completion contract alongside it."""
        norm = _norm(preset["prompt_template"])
        assert '"status": "success" | "error"' in norm
        assert '"verdict": "recorded" | "error"' in norm
        assert '"status" is REQUIRED — it is the flow completion signal' in norm
        assert "NOT a completion signal" in norm

    def test_disclaimer(self, preset):
        assert DISCLAIMER in preset["prompt_template"]


class TestHumanCarriesTheRiskDecision:
    def test_agent_never_decides(self, preset):
        norm = _norm(preset["prompt_template"])
        assert "You NEVER make, imply, or default the risk decision" in norm
        assert "never substitute your own judgment" in norm
        # The dossier summary must not steer the reviewer.
        assert "do NOT recommend an outcome" in norm

    def test_approval_outcome_is_the_decision(self, preset):
        norm = _norm(preset["prompt_template"])
        assert "request_approval" in norm
        assert 'granted -> "accepted", denied -> "rejected"' in norm
        # No decision without a human: unavailable tool => pending + error.
        assert '"pending"' in norm

    def test_reviewer_identity_never_invented(self, preset):
        norm = _norm(preset["prompt_template"])
        assert "never invent a name or timestamp" in norm
        assert 'set "reviewer" to null' in norm
        assert "approval audit trail" in norm

    def test_approval_is_not_certification(self, preset):
        norm = _norm(preset["prompt_template"])
        assert "it is not a certification" in norm


class TestHonestLegwork:
    def test_facts_cite_sources(self, preset):
        norm = _norm(preset["prompt_template"])
        assert "Every fact cites its source" in norm
        assert "Do not summarize documents you did not receive" in norm

    def test_cve_history_honesty(self, preset):
        prompt = preset["prompt_template"]
        assert "OSV.dev" in prompt
        assert "KEV" in prompt
        norm = _norm(prompt)
        assert "NEVER claim it has no known vulnerabilities" in norm

    def test_ce_declaration_presence_only(self, preset):
        """Presence is reportable; authenticity/validity is not verifiable."""
        norm = _norm(preset["prompt_template"])
        assert "CANNOT verify its authenticity or legal validity" in norm
        assert "authenticity_verified is always false" in norm

    def test_open_unknowns_required(self, preset):
        norm = _norm(preset["prompt_template"])
        assert "OPEN UNKNOWNS" in norm
        assert 'an honest "not determined" is evidence' in norm

    def test_one_page_verdict_cover(self, preset):
        """dossier.md opens with the same three-box cover as 004-006."""
        prompt = preset["prompt_template"]
        norm = _norm(prompt)
        assert "dossier.md" in prompt
        assert "MUST OPEN" in prompt
        assert "one-page cover" in prompt
        assert "Verdict sentence first" in prompt
        assert "the same value written to result.json" in norm
        for box in (
            'BOX 1 — "What we checked"',
            'BOX 2 — "What we did NOT check"',
            'BOX 3 — "What you should do next week"',
        ):
            assert box in prompt, f"missing cover box: {box}"
        assert "OPEN UNKNOWNS" in prompt
        assert "do NOT recommend accept or reject" in norm
        assert "HONESTY RAIL" in prompt
        assert "may only summarize" in norm
        assert "Strictly one page" in norm


class TestRecordStorage:
    def test_record_lands_in_compliance_repo(self, preset):
        prompt = preset["prompt_template"]
        assert "/workspace/compliance" in prompt
        assert "compliance_repo_path" in prompt
        assert "components/" in prompt
        assert "-due-diligence.json" in prompt

    def test_commit_discipline(self, preset):
        norm = _norm(preset["prompt_template"])
        assert "NEVER run git push" in norm
        assert "git add ONLY these files" in norm
        assert "committed: false" in norm

    def test_storage_skip_is_honest(self, preset):
        norm = _norm(preset["prompt_template"])
        assert "skipped — no compliance repository attached" in norm


class TestLoader:
    def test_loader_picks_up_the_preset(self):
        from unittest.mock import patch

        from preloop.flow_presets import load_flow_presets

        load_flow_presets.cache_clear()
        try:
            with patch("preloop.flow_presets.PRESETS_DIRS", [PRESETS_DIR]):
                names = [p["name"] for p in load_flow_presets()]
            assert "Component Due Diligence Record" in names
            assert names.index("Component Due Diligence Record") > names.index(
                "Release Security Audit"
            )
        finally:
            load_flow_presets.cache_clear()


class TestRecordFixture:
    """Synthetic due-diligence record documenting the output shape."""

    def test_shape_and_schema(self):
        record = json.loads((FIXTURES_DIR / "due-diligence-record.json").read_text())
        assert record["schema"] == SCHEMA_ID
        assert record["flow"] == "component-due-diligence"
        assert record["disclaimer"].startswith(DISCLAIMER)
        assert record["component"]["name"] and record["component"]["version"]

    def test_decision_block_honesty(self):
        record = json.loads((FIXTURES_DIR / "due-diligence-record.json").read_text())
        decision = record["decision"]
        assert decision["outcome"] in {"accepted", "rejected", "pending"}
        assert decision["decided_via"] == "preloop_approval"
        # Reviewer identity lives in the platform audit trail, never here.
        assert decision["reviewer"] is None
        assert "audit trail" in decision["note"]

    def test_ce_declaration_not_authenticated(self):
        record = json.loads((FIXTURES_DIR / "due-diligence-record.json").read_text())
        assert record["evidence"]["ce_declaration"]["authenticity_verified"] is False

    def test_record_path_cross_references_repo(self):
        record = json.loads((FIXTURES_DIR / "due-diligence-record.json").read_text())
        stored = record["record"]
        assert stored["path"].startswith("products/")
        assert "/components/" in stored["path"]
        assert len(stored["repo_commit"]) == 40
        int(stored["repo_commit"], 16)

    def test_fixture_is_synthetic(self):
        text = (FIXTURES_DIR / "due-diligence-record.json").read_text()
        assert "synthetic fixture" in text
        assert "example" in text
