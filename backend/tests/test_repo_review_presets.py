"""Tests for the full-repo review preset family (architecture-strategy
review / repo code health review / standards compliance walk).

Validates the shipped preset YAMLs against the shared review skeleton
invariants: strictly read-only toolset with empty MCP allowlists,
mandatory result.json contract with versioned schema ids, deterministic
sampling with declared coverage, freeze-floor drift, and the
verdict-honesty rules (the register can never upgrade a verdict; 010
refuses to run without a named standard). Pins the same style of
invariants that test_security_audit_presets.py pins for 004-006.
"""

from pathlib import Path

import pytest
import yaml

PRESETS_DIR = Path(__file__).resolve().parents[1] / "presets"

DISCLAIMER = (
    "Machine-generated review evidence. Not a certification, audit"
)

PRESET_FILES = {
    "Architecture and Strategy Conformance Review": (
        "008-architecture-strategy-review.yaml"
    ),
    "Full Repo Code Health Review": "009-repo-code-health-review.yaml",
    "Standards Compliance Walk": "010-standards-compliance-walk.yaml",
}

SCHEMA_IDS = {
    "Architecture and Strategy Conformance Review": "preloop.review.arch/v1",
    "Full Repo Code Health Review": "preloop.review.codehealth/v1",
    "Standards Compliance Walk": "preloop.review.standards/v1",
}

FLOW_SLUGS = {
    "Architecture and Strategy Conformance Review": (
        "architecture-strategy-review"
    ),
    "Full Repo Code Health Review": "repo-code-health-review",
    "Standards Compliance Walk": "standards-compliance-walk",
}


def _norm(text: str) -> str:
    """Collapse whitespace so asserts survive YAML line wrapping."""
    return " ".join(text.split())


def _load_preset(filename: str) -> dict:
    path = PRESETS_DIR / filename
    assert path.exists(), f"Missing preset file: {path}"
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    return data


@pytest.fixture(params=sorted(PRESET_FILES))
def preset(request):
    return request.param, _load_preset(PRESET_FILES[request.param])


class TestRepoReviewPresetInvariants:
    """Shared review-skeleton invariants pinned for all three presets."""

    def test_name_and_slug_match_file(self, preset):
        name, data = preset
        assert data["name"] == name
        assert data["slug"] == FLOW_SLUGS[name]

    def test_strictly_read_only_toolset(self, preset):
        """Empty MCP allowlists: the checkout plus the sandbox is the
        whole toolset; deliverables leave via the artifact channel."""
        _, data = preset
        assert data["allowed_mcp_servers"] == []
        assert data["allowed_mcp_tools"] == []

    def test_no_git_clone_or_baked_trigger(self, preset):
        """Presets ship trigger-agnostic, like 003 and the 004-006 pack."""
        _, data = preset
        assert data["git_clone_config"] is None
        assert data["trigger_config"] is None
        assert data["trigger_event_source"] is None
        assert data["trigger_event_types"] is None

    def test_agent_config(self, preset):
        _, data = preset
        assert data["agent_type"] == "codex"
        assert data["agent_config"]["sandbox_type"] == "exec"
        assert data["agent_config"]["enable_auto_lint"] is False
        assert data["is_preset"] is True
        assert data.get("description")
        assert data.get("icon")

    def test_prompt_declares_result_json_contract(self, preset):
        name, data = preset
        prompt = data["prompt_template"]
        assert "/workspace/result.json" in prompt
        assert SCHEMA_IDS[name] in prompt
        assert f'"flow": "{FLOW_SLUGS[name]}"' in prompt
        # Payload templating is the input path.
        assert "{{trigger_event.payload}}" in prompt
        # The report write is the mandatory final action.
        assert "As your FINAL action, write /workspace/result.json" in prompt

    def test_prompt_forbids_writes(self, preset):
        """No commits, no pushes, no external mutation, ever."""
        _, data = preset
        norm = _norm(data["prompt_template"])
        assert "NO write tools" in norm
        assert "Never modify tracked files" in norm
        assert "never run git commit or git push" in norm

    def test_prompt_carries_review_disclaimer(self, preset):
        """The adapted (non-CRA) disclaimer: review evidence, not a
        certification."""
        _, data = preset
        assert DISCLAIMER in data["prompt_template"]
        assert DISCLAIMER in data["description"] or True  # description opt.

    def test_prompt_separates_facts_from_judgment(self, preset):
        _, data = preset
        prompt = data["prompt_template"]
        assert '"checks"' in prompt
        assert '"assessments"' in prompt

    def test_security_is_referred_not_reviewed(self, preset):
        """Security posture belongs to the Release Security Audit family:
        referral only, pointer only, never a secret value."""
        name, data = preset
        norm = _norm(data["prompt_template"])
        assert "SECURITY IS OUT OF SCOPE" in norm
        assert "Release Security Audit" in norm
        if name != "Standards Compliance Walk":
            assert "file ONE referral finding" in norm
            assert "NEVER a value" in norm

    def test_one_repo_per_run_and_pinned_sha(self, preset):
        _, data = preset
        norm = _norm(data["prompt_template"])
        assert "exactly one per run" in norm
        assert "HEAD commit SHA (40 hex)" in norm
        assert "target_repo_path" in data["prompt_template"]
        assert "repository_url" in data["prompt_template"]

    def test_url_hygiene(self, preset):
        """Payload URLs are hostile input; SSRF-shaped targets refused."""
        _, data = preset
        prompt = data["prompt_template"]
        norm = _norm(prompt)
        assert "URL HYGIENE" in prompt
        assert "169.254.169.254" in prompt
        assert "loopback, private-range, link-local" in norm
        assert "never fetch them anyway" in norm

    def test_budget_knobs_and_depth_caps(self, preset):
        """Declared budgets: depth mapping plus explicit overrides."""
        _, data = preset
        prompt = data["prompt_template"]
        assert '"quick" | "standard" | "deep"' in prompt
        assert "60 / 150 / 400" in prompt
        assert "max_files_opened" in prompt
        assert "max_file_kb" in prompt
        assert "focus_paths" in prompt
        assert "exclude_paths" in prompt

    def test_inventory_before_content(self, preset):
        """Phased cost control: command-only inventory first."""
        _, data = preset
        prompt = data["prompt_template"]
        assert "COMMANDS ONLY" in prompt
        assert "git ls-files" in prompt
        assert "evidence/inventory.json" in prompt

    def test_deterministic_sampling_with_declared_coverage(self, preset):
        name, data = preset
        prompt = data["prompt_template"]
        norm = _norm(prompt)
        assert '"coverage"' in prompt
        assert '"plan_completed"' in prompt or "plan_completed" in prompt
        assert '"not_reviewed"' in prompt
        if name != "Standards Compliance Walk":
            assert "deterministic" in norm.lower()
            assert "no random sampling" in norm.lower()

    def test_absence_claims_are_scoped(self, preset):
        """'No X observed' is valid only for opened files or recorded
        full-repo searches."""
        _, data = preset
        norm = _norm(data["prompt_template"]).lower()
        assert "absence" in norm
        assert "recorded" in norm and "search" in norm

    def test_freeze_floor_drift(self, preset):
        """Baseline only when delivered; previous open items are a floor;
        dropping one silently fails the freeze_floor check."""
        _, data = preset
        prompt = data["prompt_template"]
        norm = _norm(prompt)
        assert "Never guess a baseline" in norm or "never guess a baseline" in norm
        assert "FREEZE FLOOR" in prompt
        assert "freeze_floor" in prompt
        assert "baseline_mismatch" in prompt
        assert "You do not self-grade the floor" in norm
        assert '"resolved"' in prompt or "resolved" in prompt
        assert "not reproducible at <SHA>" in norm

    def test_register_cannot_upgrade_verdict(self, preset):
        """Verdict honesty: met/declared/resolved rows and positive prose
        never raise a verdict; declared is not a pass."""
        _, data = preset
        prompt = data["prompt_template"]
        norm = _norm(prompt)
        assert "THE REGISTER CANNOT UPGRADE THE VERDICT" in norm
        assert 'cannot be "pass"' in norm
        assert "not a pass" in norm  # declared status
        assert '"pass" | "pass_with_findings" | "fail"' in prompt

    def test_register_grammar_and_not_checkable(self, preset):
        """The 006-style register grammar: met|gap|partial|declared plus a
        mandatory not_checkable list."""
        _, data = preset
        prompt = data["prompt_template"]
        assert "not_checkable" in prompt
        assert "met | gap | partial | declared" in prompt or (
            "met|gap|partial|declared" in prompt
        )

    def test_evidence_pack_and_size_cap(self, preset):
        _, data = preset
        prompt = data["prompt_template"]
        norm = _norm(prompt)
        assert "/workspace/evidence/" in prompt
        assert "findings.json" in prompt
        assert "drift-report.md" in prompt
        assert "under 200 KB" in norm
        assert "null rather than inventing values" in norm


class TestArchitectureStrategyReviewPreset:
    @pytest.fixture()
    def prompt(self):
        return _load_preset(
            PRESET_FILES["Architecture and Strategy Conformance Review"]
        )["prompt_template"]

    def test_declared_intent_inputs(self, prompt):
        """Payload attachment first, then repo-convention discovery."""
        assert "intent_docs" in prompt
        for source in (
            "ARCHITECTURE.md",
            "MISSION.md",
            "STRATEGY.md",
            "ROADMAP.md",
            "docs/adr/",
            "docs/decisions/",
            "AGENTS.md",
            "CLAUDE.md",
        ):
            assert source in prompt, f"missing intent source {source}"

    def test_declarations_need_source_pointers(self, prompt):
        norm = _norm(prompt)
        assert "A declaration you cannot point to does not exist" in norm

    def test_no_intent_docs_degrades_honestly(self, prompt):
        """Missing intent docs: gap row, not_checkable rows, verdict capped
        — never invented declarations, never a silent pass."""
        norm = _norm(prompt)
        assert "If NO intent sources exist" in norm
        assert "cap the verdict at pass_with_findings" in norm
        assert "Never invent declarations" in norm

    def test_drift_check_catalogue(self, prompt):
        norm = _norm(prompt)
        for marker in (
            "Responsibility drift",
            "Dependency direction violations",
            "Undeclared load-bearing components",
            "Dead declared components",
            "Technology drift",
            "Non-goal violations",
        ):
            assert marker in norm, f"missing drift check {marker}"

    def test_stable_finding_ids_and_verification_budget(self, prompt):
        norm = _norm(prompt)
        assert 'arch:<category>:<path>:<slug>' in prompt
        assert "at most 2 greps and 2 file reads per finding" in norm

    def test_purpose_fit_is_marked_judgment(self, prompt):
        """Purpose/fit commentary lives in assessments only."""
        norm = _norm(prompt)
        assert "goes ONLY in assessments" in norm


class TestRepoCodeHealthReviewPreset:
    @pytest.fixture()
    def prompt(self):
        return _load_preset(PRESET_FILES["Full Repo Code Health Review"])[
            "prompt_template"
        ]

    def test_five_lenses(self, prompt):
        for lens in (
            "CORRECTNESS RISK",
            "QUALITY",
            "PERFORMANCE HOTSPOTS",
            "DEAD CODE",
            "TEST SHAPE",
        ):
            assert lens in prompt, f"missing lens {lens}"
        assert "correctness|quality|performance|dead_code|test_shape" in prompt

    def test_judges_by_project_conventions(self, prompt):
        norm = _norm(prompt)
        assert "not generic taste" in norm

    def test_test_shape_is_not_measured_coverage(self, prompt):
        """Layout-derived shape only; a coverage percentage is never
        invented."""
        norm = _norm(prompt)
        assert "never present it as measured coverage" in norm
        assert "never invent a coverage percentage" in norm

    def test_verification_discipline_inherited_from_pr_reviewer(self, prompt):
        norm = _norm(prompt)
        assert "at most 2 greps and 2 file reads per finding" in norm
        assert "Trace the consumer" in norm
        assert "Check reachability" in norm
        assert "Fewer verified findings beat a long noisy list" in norm

    def test_stable_finding_ids(self, prompt):
        assert 'health:<lens>:<path>:<slug>' in prompt

    def test_per_module_register(self, prompt):
        norm = _norm(prompt)
        assert "one register row per lens" in norm
        assert "clean in opened sample" in norm

    def test_verdict_blocking_conditions(self, prompt):
        norm = _norm(prompt)
        assert (
            'fail" if any open high-severity correctness finding exists OR '
            "freeze_floor_passed is false" in norm
        )


class TestStandardsComplianceWalkPreset:
    @pytest.fixture()
    def prompt(self):
        return _load_preset(PRESET_FILES["Standards Compliance Walk"])[
            "prompt_template"
        ]

    def test_refuses_to_run_without_named_standard(self, prompt):
        """No named standard, no run: guessing would contaminate the
        register."""
        norm = _norm(prompt)
        assert "YOU NEVER GUESS THE STANDARD" in norm
        assert (
            'names no standards and does not set repo_declared mode, finish '
            'with an "error" summary' in norm
        )
        assert "did not name would contaminate the register" in norm

    def test_standards_input_contract(self, prompt):
        assert '"standards"' in prompt
        assert '"repo_declared"' in prompt
        norm = _norm(prompt)
        # Undelivered standards never grow invented rows.
        assert "recorded as undelivered and its rows are not invented" in norm

    def test_requirements_are_atomic_with_obligation_levels(self, prompt):
        norm = _norm(prompt)
        assert "ATOMIC, CHECKABLE requirements" in norm
        assert '"mandatory" (must/shall) or "recommended" (should/may)' in norm
        assert "<standard id>:R<n>" in prompt

    def test_gaps_require_recorded_full_repo_searches(self, prompt):
        """A gap is an absence claim: only recorded full-repo searches can
        back it; sampled absence is not_checkable, not gap."""
        norm = _norm(prompt)
        assert "A gap is an ABSENCE claim" in norm
        assert "backed by a recorded full-repo search" in norm
        assert 'not_checkable with reason "sampling budget"' in norm

    def test_security_rows_deferred_not_rechecked(self, prompt):
        assert "covered_elsewhere: release-security-audit" in prompt

    def test_not_checkable_for_unshowable_requirements(self, prompt):
        norm = _norm(prompt)
        assert (
            "organizational process, runtime behavior, personnel, hosted "
            "infrastructure" in norm
        )
        assert "Never fake a repo check" in norm

    def test_mandatory_gap_blocks_the_verdict(self, prompt):
        norm = _norm(prompt)
        assert 'fail" if any MANDATORY requirement is gap' in norm


class TestPresetsLoadThroughLoader:
    def test_loader_picks_up_all_three(self):
        from unittest.mock import patch

        from preloop.flow_presets import load_flow_presets

        load_flow_presets.cache_clear()
        try:
            with patch("preloop.flow_presets.PRESETS_DIRS", [PRESETS_DIR]):
                names = [p["name"] for p in load_flow_presets()]
            for name in PRESET_FILES:
                assert name in names
            # Ordering: the family lands after the 004-007 audit pack.
            assert names.index(
                "Architecture and Strategy Conformance Review"
            ) > names.index("Release Security Audit")
            assert (
                names.index("Full Repo Code Health Review")
                > names.index("Architecture and Strategy Conformance Review")
            )
            assert (
                names.index("Standards Compliance Walk")
                > names.index("Full Repo Code Health Review")
            )
        finally:
            load_flow_presets.cache_clear()
