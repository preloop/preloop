"""Tests for gitleaks/zizmor wrappers and checkout helpers."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from preloop.security.checkout import CheckoutError, validate_repository_url
from preloop.security.git_guard import ForbiddenGitError, validate_git_argv
from preloop.security.gitleaks import gitleaks_scan, run_gitleaks
from preloop.security.pins import (
    RECOMMENDED_GITLEAKS_VERSION,
    RECOMMENDED_ZIZMOR_VERSION,
)
from preloop.security.zizmor import run_zizmor
from preloop.tools.builtin_defs import GITLEAKS_SCAN_TOOL, ZIZMOR_SCAN_TOOL

from tests.security.conftest import SYNTHETIC_PASSWORD


class TestBuiltinDefs:
    def test_scanner_tools_exist_and_default_disabled(self):
        assert GITLEAKS_SCAN_TOOL["name"] == "gitleaks_scan"
        assert ZIZMOR_SCAN_TOOL["name"] == "zizmor_scan"
        assert GITLEAKS_SCAN_TOOL["default_enabled"] is False
        assert ZIZMOR_SCAN_TOOL["default_enabled"] is False


class TestInitializeMcpRegistersWrappers:
    def test_initialize_mcp_registers_scanner_tools(self):
        import asyncio

        from preloop.services.initialize_mcp import initialize_mcp_with_tools

        mcp = initialize_mcp_with_tools()
        tools = asyncio.run(mcp._list_tools())
        names = {getattr(tool, "name", None) for tool in tools}
        assert "gitleaks_scan" in names
        assert "zizmor_scan" in names


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


class TestCheckoutUrl:
    def test_rejects_empty_and_bad_scheme(self):
        try:
            validate_repository_url("")
            raise AssertionError("expected CheckoutError")
        except CheckoutError:
            pass
        try:
            validate_repository_url("git@example.com:org/repo.git")
            raise AssertionError("expected CheckoutError")
        except CheckoutError:
            pass

    def test_accepts_https(self):
        assert (
            validate_repository_url("https://git.example.com/example.git")
            == "https://git.example.com/example.git"
        )


class TestGitleaksWrapper:
    def test_missing_binary_is_not_empty_met(self, tmp_path: Path):
        with patch("preloop.security.gitleaks.shutil.which", return_value=None):
            result = run_gitleaks(tmp_path)
        assert result["error"] == "scanner_not_installed"
        assert result["recommended_version"] == RECOMMENDED_GITLEAKS_VERSION
        assert "met" not in json.dumps(result).lower()

    def test_redacts_secret_values(self, tmp_path: Path):
        raw = json.dumps(
            [
                {
                    "RuleID": "generic-api-key",
                    "File": "config/service.env",
                    "Commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "StartLine": 1,
                    "Secret": SYNTHETIC_PASSWORD,
                    "Match": f"password={SYNTHETIC_PASSWORD}",
                    "Author": "Fixture Bot",
                    "Email": "fixture@example.com",
                    "Message": "add service password",
                }
            ]
        )

        def _run(argv, **kwargs):
            if argv[1] == "version":
                return SimpleNamespace(returncode=0, stdout="8.24.3", stderr="")
            report = Path(argv[argv.index("-r") + 1])
            report.write_text(raw)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("preloop.security.gitleaks.shutil.which", return_value="gitleaks"),
            patch("preloop.security.gitleaks.subprocess.run", side_effect=_run),
        ):
            result = run_gitleaks(tmp_path)

        blob = json.dumps(result)
        assert SYNTHETIC_PASSWORD not in blob
        assert "Secret" not in blob
        assert result["finding_count"] == 1
        finding = result["findings"][0]
        assert finding["commit"].startswith("aaa")
        assert finding["file"] == "config/service.env"
        assert finding["rule"] == "generic-api-key"
        assert finding["line"] == 1
        assert "met" not in blob.lower()

    def test_zero_findings_is_a_count_not_met(self, tmp_path: Path):
        def _run(argv, **kwargs):
            if argv[1] == "version":
                return SimpleNamespace(returncode=0, stdout="8.24.3", stderr="")
            report = Path(argv[argv.index("-r") + 1])
            report.write_text("[]")
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")

        with (
            patch("preloop.security.gitleaks.shutil.which", return_value="gitleaks"),
            patch("preloop.security.gitleaks.subprocess.run", side_effect=_run),
        ):
            result = run_gitleaks(tmp_path)
        assert result["finding_count"] == 0
        assert result["findings"] == []
        assert "met" not in json.dumps(result).lower()

    def test_scan_surfaces_checkout_errors(self):
        result = gitleaks_scan("")
        assert result["error"] == "checkout_failed"


class TestZizmorWrapper:
    def test_no_workflows_is_not_applicable(self, tmp_path: Path):
        result = run_zizmor(tmp_path)
        assert result["applicable"] is False
        assert result["note"] == "no_github_workflows"
        assert result["findings"] == []

    def test_missing_binary_when_workflows_exist(self, tmp_path: Path):
        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: ci\n")
        with patch("preloop.security.zizmor.shutil.which", return_value=None):
            result = run_zizmor(tmp_path)
        assert result["error"] == "scanner_not_installed"
        assert result["recommended_version"] == RECOMMENDED_ZIZMOR_VERSION

    def test_parses_findings_without_secret_values(self, tmp_path: Path):
        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: ci\n")
        raw = json.dumps(
            [
                {
                    "ident": "unpinned-uses",
                    "determinations": {"severity": "High"},
                    "locations": [
                        {
                            "symbolic": {"file": ".github/workflows/ci.yml"},
                            "concrete": {"line_span": {"start": 4}},
                        }
                    ],
                }
            ]
        )

        with (
            patch("preloop.security.zizmor.shutil.which", return_value="zizmor"),
            patch(
                "preloop.security.zizmor.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stdout=raw, stderr=""),
            ),
        ):
            result = run_zizmor(tmp_path)

        blob = json.dumps(result)
        assert SYNTHETIC_PASSWORD not in blob
        assert result["applicable"] is True
        assert result["finding_count"] == 1
        assert result["findings"][0]["rule"] == "unpinned-uses"
        assert result["findings"][0]["file"] == ".github/workflows/ci.yml"
        assert result["findings"][0]["line"] == 4
