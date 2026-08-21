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
        """No MCP servers/tools: the flows are read-only by construction."""
        _, data = preset
        assert data["allowed_mcp_servers"] == []
        assert data["allowed_mcp_tools"] == []

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

    def test_repos_are_not_scanned(self, prompt):
        """Checkouts feed evidence storage; the SBOM stays the inventory."""
        norm = _norm(prompt)
        assert "you do NOT scan repository source code" in norm

    def test_stubs_stay_small(self, prompt):
        norm = _norm(prompt)
        assert "target < 2 KB" in norm
        assert "artifact bloat" in norm


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
