"""Tests for the security audit preset pack (SBOM verify / exploit check /
release audit).

Validates the shipped preset YAMLs against the Observe/Eval pattern
invariants (read-only toolset, mandatory result.json contract, versioned
schema ids) and sanity-checks the synthetic SBOM fixtures used to
document the input path.
"""

import json
from pathlib import Path

import pytest
import yaml

PRESETS_DIR = Path(__file__).resolve().parents[1] / "presets"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "sbom"

DISCLAIMER = "Machine-generated evidence for conformity assessment support. Not a"

PRESET_FILES = {
    "SBOM Verify": "004-sbom-verify.yaml",
    "SBOM Exploit Check": "005-sbom-exploit-check.yaml",
    "Release Security Audit": "006-release-security-audit.yaml",
}

SCHEMA_IDS = {
    "SBOM Verify": "preloop.cra.sbomaudit/v1",
    "SBOM Exploit Check": "preloop.cra.vulnscan/v1",
    "Release Security Audit": "preloop.cra.releaseaudit/v1",
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


class TestSecurityAuditPresetInvariants:
    """Observe/Eval-pattern invariants shared by all three presets."""

    def test_name_matches_file(self, preset):
        name, data = preset
        assert data["name"] == name

    def test_read_only_toolset(self, preset):
        """Write tools stay off. Scanners run in the sandbox, not via MCP.

        SOLE exception: the Release Security Audit carries the built-in
        ask_user question channel — and nothing else — so a human can put
        gate waivers on the record when the payload asks for interactive
        waiver collection. ask_user is not a write tool: it routes a
        question through the platform approval workflow, which captures
        the approver identity the waiver register records.
        """
        name, data = preset
        assert data["allowed_mcp_servers"] == []
        if name == "Release Security Audit":
            assert data["allowed_mcp_tools"] == [{"name": "ask_user"}]
        else:
            assert data["allowed_mcp_tools"] == []
        assert "repo-audit" not in json.dumps(data)

    def test_no_git_clone_or_baked_trigger(self, preset):
        """Presets ship trigger-agnostic, like 003-observe-eval."""
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
        # Payload templating is the input path.
        assert "{{trigger_event.payload}}" in prompt

    def test_prompt_carries_disclaimer(self, preset):
        """Every artifact must carry the evidence-not-assessment line."""
        _, data = preset
        assert DISCLAIMER in data["prompt_template"]

    def test_prompt_forbids_writes(self, preset):
        _, data = preset
        assert "NO write tools" in _norm(data["prompt_template"])

    def test_prompt_separates_facts_from_judgment(self, preset):
        _, data = preset
        prompt = data["prompt_template"]
        assert '"checks"' in prompt
        assert '"assessments"' in prompt

    def test_verify_not_generate(self, preset):
        """The pack verifies SBOMs; generation is explicitly out of scope."""
        name, data = preset
        prompt = data["prompt_template"]
        assert "never" in prompt and "invent" in prompt
        if name != "SBOM Exploit Check":
            assert "GENERATE" in prompt

    def test_one_page_verdict_cover(self, preset):
        """Every 004-006 human report opens with the three-box cover."""
        name, data = preset
        prompt = data["prompt_template"]
        report_file = {
            "SBOM Verify": "audit-report.md",
            "SBOM Exploit Check": "vuln-report.md",
            "Release Security Audit": "audit-report.md",
        }[name]
        assert report_file in prompt
        assert "one-page cover" in prompt
        assert "Verdict sentence first" in prompt
        for box in (
            'BOX 1 — "What we checked"',
            'BOX 2 — "What we did NOT check"',
            'BOX 3 — "What you should do next week"',
        ):
            assert box in prompt, f"missing cover box: {box}"


class TestSbomVerifyPreset:
    def test_deterministic_check_catalogue(self):
        prompt = _load_preset(PRESET_FILES["SBOM Verify"])["prompt_template"]
        for marker in [
            "FORMAT VALIDITY",
            "MINIMUM ELEMENTS",
            "COVERAGE QUALITY",
            "BUILD CROSS-CHECK",
            "LICENSE FLAGS",
        ]:
            assert marker in prompt
        # Missing build evidence must be reported as skipped, not guessed.
        assert "skipped: no build evidence delivered" in prompt

    def test_cover_adapted_to_sbom_checks(self):
        """BOX 1 lists the five deterministic checks; honesty rail holds."""
        prompt = _load_preset(PRESET_FILES["SBOM Verify"])["prompt_template"]
        norm = _norm(prompt)
        assert "MUST OPEN" in prompt
        assert "the same value written to result.json" in norm
        assert "format validity, minimum elements, coverage quality" in norm
        assert "vulnerability matching" in norm
        assert "HONESTY RAIL" in prompt
        assert "may only summarize" in norm
        assert "Strictly one page" in norm


class TestSbomExploitCheckPreset:
    def test_vuln_sources_and_honest_limits(self):
        prompt = _load_preset(PRESET_FILES["SBOM Exploit Check"])["prompt_template"]
        assert "api.osv.dev/v1/querybatch" in prompt
        assert "known_exploited_vulnerabilities.json" in prompt
        # NVD rate limits stated honestly; NVD is fallback only.
        assert "FALLBACK ONLY" in prompt
        assert "5 requests per 30 seconds" in prompt
        assert "art14_candidates" in prompt
        assert "kev_snapshot_date" in prompt
        # Never claim absence for unmatchable components.
        assert "unmatchable" in prompt

    def test_completion_status_contract(self):
        """The vulnscan schema has no top-level verdict, so a required
        top-level "status" field is its flow completion signal: "success"
        when the scan completed, "error" when it could not."""
        prompt = _load_preset(PRESET_FILES["SBOM Exploit Check"])["prompt_template"]
        norm = _norm(prompt)
        assert '"status": "success" | "error"' in norm
        assert '"status" is REQUIRED — it is the flow completion signal' in norm
        # Completion is about the scan finishing, not the gate outcome.
        assert "regardless of the gate outcome or findings" in norm

    def test_cover_box2_includes_unscreened_count(self):
        """BOX 2 must name unscreened components by count from the matrix."""
        prompt = _load_preset(PRESET_FILES["SBOM Exploit Check"])["prompt_template"]
        norm = _norm(prompt)
        assert "MUST OPEN" in prompt
        assert "gate.passed written to result.json" in norm
        assert "unscreened components by count" in norm
        assert "source_matrix.screened_by_no_source" in prompt
        assert "unmatchable components" in norm
        assert "HONESTY RAIL" in prompt
        assert "may only summarize" in norm
        assert "Strictly one page" in norm


class TestReleaseSecurityAuditPreset:
    def test_combines_both_audits_plus_drift(self):
        prompt = _load_preset(PRESET_FILES["Release Security Audit"])["prompt_template"]
        assert '"sbom_audit"' in prompt
        assert '"vuln_scan"' in prompt
        assert '"drift"' in prompt
        # Drift only against a delivered baseline, never a guessed one.
        assert "never guess a baseline" in _norm(prompt)
        assert "api.osv.dev/v1/querybatch" in prompt
        assert "known_exploited_vulnerabilities.json" in prompt

    def test_designed_for_schedules(self):
        data = _load_preset(PRESET_FILES["Release Security Audit"])
        assert "schedule" in data["description"].lower()


class TestReleaseAuditEvidenceStorage:
    """Multi-repo product mode: hybrid evidence storage (per-repo stubs +
    product-level compliance repo), cross-linked by commit SHA."""

    @pytest.fixture()
    def prompt(self):
        return _load_preset(PRESET_FILES["Release Security Audit"])["prompt_template"]

    def test_hybrid_storage_phase_present(self, prompt):
        assert "EVIDENCE STORAGE (MULTI-REPO PRODUCT MODE)" in prompt
        # Per-repo stub next to the code, full pack in the compliance repo.
        assert ".preloop/evidence/" in prompt
        assert "products/<product>/audits/" in prompt

    def test_versioned_storage_schemas(self, prompt):
        assert "preloop.cra.repostub/v1" in prompt
        assert "preloop.cra.evidencepack/v1" in prompt
        # result.json gains an additive, nullable section — same schema id.
        assert '"evidence_storage"' in prompt

    def test_compliance_repo_named_by_flow_config(self, prompt):
        """The flow config names the compliance repo (clone_path
        convention); the payload may override the path."""
        norm = _norm(prompt)
        assert 'clone_path "compliance"' in norm
        assert "compliance_repo_path" in prompt

    def test_sha_cross_reference_is_explicit(self, prompt):
        norm = _norm(prompt)
        assert "git rev-parse HEAD" in prompt
        assert "the manifest SHAs and the stub SHAs must agree" in norm

    def test_skipped_without_checkouts(self, prompt):
        """No attached repos => artifact-only behavior, honestly recorded."""
        norm = _norm(prompt)
        assert "skipped — no repositories attached" in norm
        assert "that is not a failure" in norm

    def test_commit_discipline(self, prompt):
        norm = _norm(prompt)
        # Agent commits locally; the platform pushes / opens PRs.
        assert "NEVER run git push" in norm
        assert "git add ONLY the evidence files" in norm
        assert "never amend or rebase existing history" in norm
        # Commit failure is recorded, never papered over.
        assert "committed: false" in norm

    def test_report_phase_is_its_own_heading(self, prompt):
        """PHASE 4B must not swallow the mandatory result.json write."""
        assert "PHASE 4B: EVIDENCE STORAGE (MULTI-REPO PRODUCT MODE)" in prompt
        assert "PHASE 5: REPORT (MANDATORY)" in prompt
        assert prompt.index("PHASE 4B:") < prompt.index("PHASE 5: REPORT")
        assert prompt.index("PHASE 5: REPORT") < prompt.index(
            "As your FINAL action, write /workspace/result.json"
        )

    def test_repos_are_not_scanned(self, prompt):
        """SBOM stays the vuln inventory; gap register is hygiene/config."""
        norm = _norm(prompt)
        assert "NOT use repository source as the vulnerability inventory" in norm
        assert "FILE-PRESENCE, CONFIG, and SECRET-HYGIENE" in _norm(prompt)
        assert "repo-audit" not in prompt

    def test_scanners_run_in_the_sandbox(self, prompt):
        """gitleaks/zizmor are installed and run in the execution sandbox;
        no server-side MCP scanner wrappers."""
        norm = _norm(prompt)
        assert "Install and run gitleaks and zizmor inside this execution sandbox" in (
            norm
        )
        assert "never on the platform control plane" in norm
        assert "gitleaks 8.24.3" in prompt
        assert "zizmor 1.16.0" in prompt
        # The wrapper tool names must be gone.
        assert "gitleaks_scan" not in prompt
        assert "zizmor_scan" not in prompt
        # zizmor scope + honest degradation.
        assert "not applicable" in norm
        assert "unavailable: reason" in prompt

    def test_gap_register_schema(self, prompt):
        """PHASE 3.5 keeps the freeze schema: statuses, floor, no product
        nouns (generic config names like MQTT_PASS are allowed)."""
        assert "PHASE 3.5: CRA GAP REGISTER" in prompt
        assert '"gap_register"' in prompt
        assert "not_checkable" in prompt
        assert "secrets_findings_count" in prompt
        assert "gitleaks count of 0 does NOT make this item met" in _norm(prompt)
        assert "freeze floor" in prompt
        lowered = prompt.lower()
        for noun in (
            "kettlecompanion",
            "tasmota",
            "user_config_override",
            "my_user_config",
        ):
            assert noun not in lowered

    def test_pickaxe_keyword_sweeps_restored(self, prompt):
        """The battle-tested history sweep: keyword families + --grep."""
        norm = _norm(prompt)
        assert "-S" in prompt
        for term in (
            "MQTT_PASS",
            "MQTT_PASSWORD",
            "MQTT_USER",
            "PASSWORD",
            "PASSWD",
            "SECRET",
            "TOKEN",
            "API_KEY",
            "API_TOKEN",
            "STA_PASS",
            "WEB_PASSWORD",
            "PRIVATE_KEY",
        ):
            assert term in prompt, f"missing pickaxe term {term}"
        assert "--grep='should not be public'" in prompt
        assert "--grep='Remove MQTT'" in prompt
        assert "git log --all --diff-filter=D --summary" in norm

    def test_forbidden_to_dismiss_rule(self, prompt):
        """Classify EVERY pickaxe hit; the documented failure mode is
        dismissing hits as keyword changes only."""
        norm = _norm(prompt)
        assert "KNOWN FAILURE MODE (forbidden)" in norm
        assert '"keyword changes only"' in norm
        assert "MUST classify each pickaxe commit" in norm
        assert "Unclassified pickaxe hits mean this item is gap or partial" in norm
        assert "SHA+PATH REGISTER" in prompt
        assert "one SHA+path row" in norm

    def test_secret_values_never_dumped(self, prompt):
        norm = _norm(prompt)
        assert "FORBIDDEN: git log -p, git show, git diff" in norm
        assert "NEVER print, echo, quote, or copy a secret VALUE" in norm

    def test_db_resolvable_and_negative_control(self, prompt):
        """Coverage honesty: db_resolvable metric + mandatory negative
        control with a method_blind flag."""
        norm = _norm(prompt)
        assert "db_resolvable" in prompt
        assert "pkg:generic and pkg:github" in norm
        assert "NEGATIVE CONTROL (mandatory" in norm
        assert "method_blind" in prompt
        assert "negative_control" in prompt
        assert "tj-actions/changed-files" in prompt
        assert "GHSA-mrrh-fwg8-r2c3" in prompt

    def test_three_box_cover_page(self, prompt):
        """audit-report.md opens with the non-agentic reader cover."""
        for box in (
            'BOX 1 — "What we checked"',
            'BOX 2 — "What we did NOT check"',
            'BOX 3 — "What you should do next week"',
        ):
            assert box in prompt, f"missing cover box: {box}"
        assert "Verdict sentence first" in prompt
        assert "the same value written to result.json" in _norm(prompt)
        assert "DRIFT LINE" in prompt
        assert "NTIA RECONCILIATION LINE" in prompt

    def test_hygiene_and_citation_rules(self, prompt):
        """Junk-at-HEAD, leftover CI, key filenames, default-credential
        citations, support-window semantics."""
        norm = _norm(prompt)
        assert "git ls-files at HEAD" in norm
        assert "*.yml.off" in prompt
        assert "Do not say a file was later deleted unless HEAD lacks the path" in norm
        assert "ca.key" in prompt and "id_rsa" in prompt
        assert "asserted key filename" in norm
        assert "changelog or README admits a default AP with no password" in norm
        assert "STA_PASS1" in prompt
        assert "OTA_URL" in prompt
        assert "empty WEB_PASSWORD" in norm
        assert "If no support-window statement exists, status is gap" in norm

    def test_junk_paths_listed_verbatim(self, prompt):
        """Literal junk paths, not category summaries or counts."""
        norm = _norm(prompt)
        assert "List each junk path VERBATIM as its own quoted string" in norm
        assert "exactly as git ls-files prints it (including spaces)" in norm
        assert (
            'a category summary or a count ("three fragments") without the '
            "literal paths is a miss" in norm
        )

    def test_auto_upgrade_rule_and_webserver_define_cited(self, prompt):
        """The auto-upgrade rule line and BOTH webserver defines must be
        cited by name and file:line, never summarized."""
        norm = _norm(prompt)
        assert "cite its file:line and quote only the rule/command names" in norm
        assert "never embedded credentials" in norm
        assert (
            '"HTTP OTA URLs" alone without the auto-upgrade rule line is a miss' in norm
        )
        assert "cite BOTH sides by name and file:line" in norm
        assert (
            "the define that enables the server AND the empty password define" in norm
        )
        assert "citing only one is a partial" in norm

    def test_stable_gap_register_item_ids(self, prompt):
        """The fixed id vocabulary keeps previous-run floor diffs clean."""
        norm = _norm(prompt)
        assert "STABLE ITEM IDS" in prompt
        assert "Use these exact item ids verbatim in gap_register.items[].id" in norm
        id_list = (
            "cvd_policy, security_contact, support_window, article14_runbooks, "
            "update_and_signed_ota, secrets_hygiene, "
            "default_credentials_provisioning, repo_hygiene, key_management, "
            "ci_secret_scanning, ci_sbom_job, debug_leakage"
        )
        assert id_list in norm, "stable id list incomplete or reordered"
        assert len(id_list.split(", ")) == 12
        assert "Do not rename or merge ids between runs" in norm
        assert "renamed ids break previous-run floor comparison" in norm

    def test_count_equals_rows_and_floor(self, prompt):
        norm = _norm(prompt)
        assert (
            "MUST equal the number of SHA+path rows listed in gap-register.md" in norm
        )
        assert "You do not self-grade the floor" in norm

    def test_sbom_fail_cannot_be_upgraded(self, prompt):
        assert "sbom_audit.verdict is fail, result.verdict MUST be fail" in _norm(
            prompt
        )

    def test_stubs_stay_small(self, prompt):
        norm = _norm(prompt)
        assert "target < 2 KB" in norm
        assert "artifact bloat" in norm


class TestPerSourceScreeningMatrix:
    """The screening coverage statement is a component x source matrix:
    every source carries its own negative control, heuristic layers are
    labeled and gate-inert, and the cover page derives from the matrix."""

    @pytest.fixture(params=["SBOM Exploit Check", "Release Security Audit"])
    def prompt(self, request):
        return _load_preset(PRESET_FILES[request.param])["prompt_template"]

    def test_matrix_block_present_with_all_sources(self, prompt):
        norm = _norm(prompt)
        assert "PER-SOURCE SCREENING MATRIX" in norm
        for source in ("osv_purl", "osv_git", "nvd_cpe", "osv_distro"):
            assert source in prompt, f"missing source: {source}"
        assert "source_matrix" in prompt
        assert "screened_by_no_source" in prompt
        assert "evidence/source-matrix.json" in prompt

    def test_git_range_source_screens_vendored_code(self, prompt):
        """OSV commit queries via the vcs_url in enriched purls — the win
        for vendored C code the purl path is blind to."""
        norm = _norm(prompt)
        assert "vcs_url" in prompt
        assert "git ls-remote <vcs_url> '<tag>^{}'" in norm
        assert '{"commit": "<40 hex sha>"}' in norm
        assert "record it, never guess a commit" in norm

    def test_query_form_guidance(self, prompt):
        """Malformed queries return empty sets that look clean: the purl
        source pins the query form and demands a same-class control for
        all-empty ecosystem classes (staging round W1 regression)."""
        norm = _norm(prompt)
        assert "QUERY FORM MATTERS" in norm
        assert '{"package": {"purl": "<purl minus ?qualifiers>"}}' in norm
        assert "strip qualifiers such as vcs_url first" in norm
        assert "never combine a purl with a name+ecosystem object" in norm
        assert "group:artifact with a COLON, never a slash" in norm
        assert (
            "pkg:maven/org.apache.logging.log4j/log4j-core@2.14.1 must "
            "return CVE-2021-44228" in norm
        )
        # Staging W2-r5 regression: the agent ran the control, saw the
        # advisories, and never counted them as inventory findings.
        assert "Control results are not quarantined" in norm
        assert "a control never launders a real finding out of the screen" in norm

    def test_one_negative_control_per_source(self, prompt):
        norm = _norm(prompt)
        # osv_git control: a curl 8.3.0 release commit and its known CVE.
        assert "6fa1d817e5b1a00d7d0c8168091877476b499317" in prompt
        assert "CVE-2023-38545" in prompt
        # nvd_cpe control.
        assert "cpe:2.3:a:haxx:curl:7.50.0" in prompt
        # osv_distro control.
        assert '{"package": {"name": "curl"}, "version": "7.50.0"}' in norm
        assert "must return distro-prefixed entries" in norm

    def test_heuristic_layers_are_labeled_and_gate_inert(self, prompt):
        norm = _norm(prompt)
        assert "LABELED HEURISTIC" in norm
        assert "never presented as a database match" in norm
        assert "do NOT enter the severity gate" in norm
        assert "a heuristic can neither fail nor clear a release on its own" in norm

    def test_findings_carry_source_and_match_kind(self, prompt):
        assert '"match_kind": "database" | "heuristic"' in _norm(prompt)
        assert '"osv_purl" | "osv_git" | "nvd_cpe" | "osv_distro"' in _norm(prompt)

    def test_absence_claims_are_per_source(self, prompt):
        norm = _norm(prompt)
        assert '"not screenable by any method"' in norm
        assert 'never "zero vulnerabilities"' in norm


class TestReleaseAuditWaivers:
    """Waivers: the governed alternative to verdict upgrades. Human-authored
    inputs, deterministic application, verbatim echo, fail-closed defaults."""

    @pytest.fixture
    def prompt(self):
        return _load_preset(PRESET_FILES["Release Security Audit"])["prompt_template"]

    def test_waiver_file_input_declared(self, prompt):
        norm = _norm(prompt)
        assert "Optional WAIVER FILE: human-authored gate acceptances" in norm
        assert '"id": "<finding id or gate-family id>"' in norm
        # All four fields are required; an incomplete entry waives nothing.
        assert "An entry missing id, reason, author, or date is INVALID" in norm

    def test_no_model_authored_waivers(self, prompt):
        norm = _norm(prompt)
        assert "NO MODEL-AUTHORED WAIVERS, EVER" in norm
        assert "You transcribe and apply" in norm or (
            "you only transcribe and apply what humans put on the record" in norm
        )

    def test_unwaived_failure_keeps_gate_failed(self, prompt):
        norm = _norm(prompt)
        assert "gate.passed is true only when every gate failure is waived" in norm
        assert "An unwaived failure keeps the gate failed" in norm
        assert "waivers never upgrade the SBOM-audit verdict" in norm
        assert (
            'A run with any applied waiver can never end better than "pass_with_findings"'
            in norm
        )

    def test_any_failure_is_waivable_and_aliases_match(self, prompt):
        """Staging W2 regression: the agent must not invent an
        'unwaivable' class, and a CVE-id waiver covers its GHSA alias."""
        norm = _norm(prompt)
        assert "Waivability is not severity-dependent" in norm
        assert "KEV-listed findings included" in norm
        assert 'You never decide that a failure is "unwaivable"' in norm
        assert (
            "Match waiver ids against the finding id AND its recorded aliases" in norm
        )
        assert (
            "a CVE id waives the same advisory surfaced under a GHSA/OSV alias" in norm
        )

    def test_waivers_echoed_verbatim_and_cover_listed(self, prompt):
        norm = _norm(prompt)
        assert "echoed VERBATIM" in norm
        assert "WAIVERS (mandatory cover section" in norm
        assert '"No waivers were delivered or applied."' in norm
        assert "evidence/waivers.json" in prompt
        assert "never silently dropped" in norm

    def test_gate_schema_carries_waiver_outcome(self, prompt):
        for field in (
            '"passed_before_waivers"',
            '"waivers_applied"',
            '"unwaived_failures"',
            '"waivers_invalid"',
            '"waivers_unmatched"',
        ):
            assert field in prompt, f"missing gate field: {field}"

    def test_interactive_collection_is_batched_and_fail_closed(self, prompt):
        norm = _norm(prompt)
        assert 'waiver_collection: "interactive"' in norm
        assert "make EXACTLY ONE ask_user call for them all, batched" in norm
        assert "Never one call per finding, never a second round" in norm
        # Tool routing (staging round W2 regression): the namespaced tool
        # name routes; a routing failure fails closed like a timeout.
        assert (
            "Call the tool by the exact namespaced name your tool catalog "
            "lists for the preloop MCP server" in norm
        )
        assert (
            "a routing failure is not an answer — it fails closed like a "
            "timeout" in norm
        )
        assert "TIMEOUT / no answer / declined = FAIL CLOSED" in norm
        assert "Never re-ask, never assume acceptance" in norm
        # The approval record is the identity capture.
        assert (
            "The approval id is required and always comes from the tool "
            "result, never from you" in norm
        )
        assert (
            "an interactive answer with no platform-reported approval id "
            "waives nothing" in norm
        )

    def test_ask_user_is_the_sole_allowlist_exception(self, prompt):
        norm = _norm(prompt)
        assert "SOLE exception to the otherwise-empty allowlist" in norm
        assert "not a write tool" in norm
        assert "captures the approver's identity" in norm
        assert "Non-interactive runs (the default) NEVER call ask_user" in norm

    def test_default_stays_deterministic_and_unattended(self, prompt):
        norm = _norm(prompt)
        assert (
            "file-only, no questions asked, so CI runs stay deterministic "
            "and unattended" in norm
        )


class TestEvidenceStorageFixtures:
    """Synthetic per-repo stub + product manifest documenting the hybrid
    storage cross-reference."""

    FIXTURES = Path(__file__).resolve().parent / "fixtures" / "evidence"

    def test_repo_stub_shape(self):
        stub = json.loads((self.FIXTURES / "repo-stub.json").read_text())
        assert stub["schema"] == "preloop.cra.repostub/v1"
        assert stub["result_schema"] == "preloop.cra.releaseaudit/v1"
        assert len(stub["repo"]["commit"]) == 40
        int(stub["repo"]["commit"], 16)  # hex SHA
        assert stub["disclaimer"].startswith(DISCLAIMER)
        # The stub points at the product-level pack.
        assert stub["product"]["compliance_repo"]
        assert stub["product"]["evidence_path"].startswith("products/")

    def test_repo_stub_is_small_and_summary_only(self):
        raw = (self.FIXTURES / "repo-stub.json").read_text()
        assert len(raw.encode()) < 2048
        assert "findings" not in json.loads(raw)  # no detail in code repos

    def test_manifest_cross_references_stub_by_sha(self):
        stub = json.loads((self.FIXTURES / "repo-stub.json").read_text())
        manifest = json.loads((self.FIXTURES / "product-manifest.json").read_text())
        assert manifest["schema"] == "preloop.cra.evidencepack/v1"
        assert manifest["disclaimer"].startswith(DISCLAIMER)
        by_remote = {r["remote"]: r for r in manifest["repos"]}
        entry = by_remote[stub["repo"]["remote"]]
        # The spine of the audit trail: manifest SHA == stub SHA.
        assert entry["commit"] == stub["repo"]["commit"]
        assert entry["stub_path"].startswith(".preloop/evidence/")
        for repo in manifest["repos"]:
            assert len(repo["commit"]) == 40
            int(repo["commit"], 16)

    def test_fixtures_are_synthetic(self):
        for name in ("repo-stub.json", "product-manifest.json"):
            text = (self.FIXTURES / name).read_text()
            assert "synthetic fixture" in text
            assert "example" in text  # example.com-style identities only


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
            # Ordering: after the existing 001-003 presets.
            assert names.index("SBOM Verify") > names.index("Observe / Eval")
        finally:
            load_flow_presets.cache_clear()


class TestSbomFixtures:
    """The synthetic SPDX + CycloneDX samples used as documented inputs."""

    def test_spdx_fixture_minimum_elements(self):
        doc = json.loads((FIXTURES_DIR / "sample.spdx.json").read_text())
        assert doc["spdxVersion"] == "SPDX-2.3"
        assert doc["creationInfo"]["created"]
        assert doc["creationInfo"]["creators"]
        packages = doc["packages"]
        assert len(packages) == 3
        for pkg in packages:
            assert pkg["name"]
            assert pkg["versionInfo"]
        # Relationships reference existing elements (referential integrity).
        ids = {doc["SPDXID"]} | {pkg["SPDXID"] for pkg in packages}
        for rel in doc["relationships"]:
            assert rel["spdxElementId"] in ids
            assert rel["relatedSpdxElement"] in ids
        # The fixture deliberately contains quality gaps for the audit to
        # find: one package with NOASSERTION license and no purl.
        gaps = [
            p
            for p in packages
            if p.get("licenseConcluded") == "NOASSERTION" and not p.get("externalRefs")
        ]
        assert len(gaps) == 1

    def test_cyclonedx_fixture_shape(self):
        doc = json.loads((FIXTURES_DIR / "sample.cdx.json").read_text())
        assert doc["bomFormat"] == "CycloneDX"
        assert doc["specVersion"] == "1.5"
        assert doc["metadata"]["timestamp"]
        components = doc["components"]
        assert len(components) == 2
        # One fully-identified component, one deliberate quality gap
        # (no version, no purl) for the audit to find.
        assert components[0]["purl"] == "pkg:generic/openssl@3.0.13"
        assert "version" not in components[1]
        assert "purl" not in components[1]
        # Dependency refs resolve to declared bom-refs.
        declared = {c["bom-ref"] for c in components}
        declared.add(doc["metadata"]["component"]["bom-ref"])
        for dep in doc["dependencies"]:
            assert dep["ref"] in declared
            for ref in dep["dependsOn"]:
                assert ref in declared

    def test_fixtures_are_synthetic(self):
        """Confidentiality: fixtures carry no real vendor/customer identity."""
        for name in ("sample.spdx.json", "sample.cdx.json"):
            text = (FIXTURES_DIR / name).read_text()
            assert "synthetic fixture" in text
