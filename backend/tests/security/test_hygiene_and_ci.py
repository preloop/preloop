"""Tests for repo_hygiene_walk and ci_workflow_audit."""

from __future__ import annotations

import json

from preloop.security.ci_workflow import ci_workflow_audit
from preloop.security.hygiene import repo_hygiene_walk

from tests.security.conftest import JUNK_FILENAME, SYNTHETIC_PASSWORD


class TestRepoHygieneWalk:
    def test_junk_name_and_disabled_ci(self, synthetic_history_repo):
        result = repo_hygiene_walk(str(synthetic_history_repo["repo"]))
        blob = json.dumps(result)
        assert SYNTHETIC_PASSWORD not in blob
        kinds = {row["kind"] for row in result["rows"]}
        assert "junk_name" in kinds
        assert "disabled_ci" in kinds
        junk = [r for r in result["rows"] if r["kind"] == "junk_name"]
        assert any(JUNK_FILENAME in r["path"] for r in junk)

    def test_readme_ca_key_pointer_stays_on_disk(self, synthetic_history_repo):
        readme = (synthetic_history_repo["repo"] / "README.md").read_text()
        assert "ca.key" in readme
        assert "example.invalid" in readme


class TestCiWorkflowAudit:
    def test_flags_mutable_tag_and_pull_request_target(self, synthetic_history_repo):
        result = ci_workflow_audit(str(synthetic_history_repo["repo"]))
        kinds = {row["kind"] for row in result["rows"]}
        assert "mutable_uses_tag" in kinds
        assert "pull_request_target" in kinds
        assert "missing_permissions" in kinds
