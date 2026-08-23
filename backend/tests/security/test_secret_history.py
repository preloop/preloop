"""Tests for secret_history_scan and forbidden git ops."""

from __future__ import annotations

import json

from preloop.security.git_guard import ForbiddenGitError, validate_git_argv
from preloop.security.secret_history import secret_history_scan

from tests.security.conftest import SYNTHETIC_PASSWORD


class TestGitGuard:
    def test_rejects_log_patch(self):
        try:
            validate_git_argv(["git", "log", "-p"])
            raise AssertionError("expected ForbiddenGitError")
        except ForbiddenGitError:
            pass

    def test_rejects_show(self):
        try:
            validate_git_argv(["git", "show", "HEAD"])
            raise AssertionError("expected ForbiddenGitError")
        except ForbiddenGitError:
            pass

    def test_rejects_cat_file_dump(self):
        try:
            validate_git_argv(["git", "cat-file", "-p", "HEAD"])
            raise AssertionError("expected ForbiddenGitError")
        except ForbiddenGitError:
            pass

    def test_allows_pickaxe(self):
        validate_git_argv(["git", "-C", "/tmp/x", "log", "--all", "-S", "password"])


class TestSecretHistoryScan:
    def test_emits_remove_sha_without_value(self, synthetic_history_repo):
        repo = synthetic_history_repo["repo"]
        result = secret_history_scan(str(repo))
        blob = json.dumps(result)
        assert SYNTHETIC_PASSWORD not in blob
        shas = {row["sha"] for row in result["rows"]}
        assert synthetic_history_repo["remove_sha"] in shas
        assert synthetic_history_repo["add_sha"] in shas
        for row in result["rows"]:
            assert row["status"] == "finding"
            assert "sha" in row
            assert "kind" in row
            assert SYNTHETIC_PASSWORD not in json.dumps(row)

    def test_deleted_pem_is_a_row(self, synthetic_history_repo):
        result = secret_history_scan(str(synthetic_history_repo["repo"]))
        pem_rows = [
            r
            for r in result["rows"]
            if r["kind"] == "deleted_filename" and r["path"].endswith(".pem")
        ]
        assert pem_rows
        assert pem_rows[0]["sha"] == synthetic_history_repo["deleted_pem_sha"]

    def test_gitleaks_zero_is_not_met(self, synthetic_history_repo):
        result = secret_history_scan(str(synthetic_history_repo["repo"]))
        assert result["gitleaks"]["note"] == "gitleaks_zero_is_not_met"
        if result["gitleaks"].get("finding_count") == 0:
            assert "gitleaks_zero_is_not_met" in result["notes"]

    def test_defaults_have_no_product_nouns(self):
        from preloop.security.defaults import DEFAULT_GREP_TERMS, DEFAULT_SECRET_TERMS

        joined = " ".join(DEFAULT_SECRET_TERMS + DEFAULT_GREP_TERMS).lower()
        for noun in (
            "mqtt_pass",
            "kettlecompanion",
            "remove mqtt",
            "tasmota",
            "user_config_override",
        ):
            assert noun not in joined
