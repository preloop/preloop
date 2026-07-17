"""Unit tests for the e2e rig's pure-python logic.

Covers snapshot_diff (the offboarding-assertion semantics) and the output
redaction shared by lib/riglib.py and research_agent.py. Stdlib + pytest
only; no VM, network, or recording toolchain involved.

Run: pytest scripts/e2e-rig/tests
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

RIG_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RIG_DIR))
sys.path.insert(0, str(RIG_DIR / "lib"))

import research_agent  # noqa: E402
import riglib  # noqa: E402
import snapshot_diff  # noqa: E402


# --- redaction (riglib.redact / research_agent.redact) ----------------------

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJlLXNlZ21lbnQ"
OPAQUE = "a" * 40


@pytest.mark.parametrize("redact", [riglib.redact, research_agent.redact])
class TestRedact:
    def test_masks_bearer_credentials(self, redact):
        out = redact(f"header was 'Authorization: Bearer {JWT}'")
        assert JWT not in out
        assert "***" in out

    def test_masks_bare_jwt(self, redact):
        out = redact(f"unexpected body: {JWT}")
        assert JWT not in out
        assert "unexpected body" in out

    def test_masks_long_opaque_tokens(self, redact):
        out = redact(f"detail: credential {OPAQUE} rejected")
        assert OPAQUE not in out
        assert "rejected" in out

    def test_masks_sensitive_json_values_even_short_ones(self, redact):
        # FastAPI validation errors echo the submitted input verbatim,
        # including short passwords the token heuristics would not catch.
        body = {"detail": [{"input": {"username": "e2e", "password": "hunter2"}}]}
        out = redact(body)
        assert "hunter2" not in out
        assert "username" in out

    def test_accepts_parsed_bodies(self, redact):
        out = redact({"detail": "Not Found"})
        assert "Not Found" in out

    def test_truncates_long_bodies(self, redact):
        out = redact("x " * 1000, limit=100)
        assert len(out) < 200
        assert "truncated" in out

    def test_plain_error_text_survives(self, redact):
        msg = "invalid request: field 'model' is required"
        assert redact(msg) == msg


# --- snapshot_diff ----------------------------------------------------------


def write(root: Path, rel: str, content: str) -> Path:
    path = root / "home" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


class TestAgentFor:
    def test_specific_prefix_wins_over_general(self):
        assert (
            snapshot_diff.agent_for(".claude/claude_desktop_config.json")
            == "Claude Desktop"
        )
        assert snapshot_diff.agent_for(".claude/settings.json") == "Claude Code"
        assert snapshot_diff.agent_for(".claude.json") == "Claude Code"

    def test_unknown_path(self):
        assert snapshot_diff.agent_for(".weird/agent.conf") == "unknown"


class TestCompareFile:
    def test_byte_identical(self, tmp_path):
        a = write(tmp_path / "a", ".codex/config.toml", "x = 1\n")
        b = write(tmp_path / "b", ".codex/config.toml", "x = 1\n")
        result = snapshot_diff.compare_file(".codex/config.toml", a, b)
        assert result["verdict"] == "byte-identical"

    def test_semantic_equal_ignores_volatile_keys(self, tmp_path):
        rel = ".claude/settings.json"
        a = write(
            tmp_path / "a", rel, json.dumps({"model": "opus", "feedbackSurveyState": 1})
        )
        b = write(
            tmp_path / "b", rel, json.dumps({"feedbackSurveyState": 2, "model": "opus"})
        )
        result = snapshot_diff.compare_file(rel, a, b)
        assert result["verdict"] == "semantic-equal"

    def test_claude_json_compared_on_managed_surface_only(self, tmp_path):
        rel = ".claude.json"
        a = write(
            tmp_path / "a",
            rel,
            json.dumps({"mcpServers": {"pl": {}}, "model": "opus", "tips": 1}),
        )
        b = write(
            tmp_path / "b",
            rel,
            json.dumps({"mcpServers": {"pl": {}}, "model": "opus", "tips": 99}),
        )
        result = snapshot_diff.compare_file(rel, a, b)
        assert result["verdict"] == "semantic-equal"

    def test_claude_json_managed_drift_fails(self, tmp_path):
        rel = ".claude.json"
        a = write(tmp_path / "a", rel, json.dumps({"mcpServers": {}, "model": "opus"}))
        b = write(
            tmp_path / "b", rel, json.dumps({"mcpServers": {}, "model": "sonnet"})
        )
        result = snapshot_diff.compare_file(rel, a, b)
        assert result["verdict"] == "FAIL"
        assert "diff" in result

    def test_file_only_on_one_side_fails(self, tmp_path):
        rel = ".cursor/mcp.json"
        a = write(tmp_path / "a", rel, "{}")
        result = snapshot_diff.compare_file(rel, a, None)
        assert result["verdict"] == "FAIL"
        assert "baseline" in result["detail"]

    def test_claude_json_created_empty_on_one_side_passes(self, tmp_path):
        # Claude Code writes ~/.claude.json on any run; if the managed keys
        # are empty the restore is still considered clean.
        rel = ".claude.json"
        b = write(tmp_path / "b", rel, json.dumps({"mcpServers": {}, "tips": 3}))
        result = snapshot_diff.compare_file(rel, None, b)
        assert result["verdict"] == "semantic-equal"

    def test_absent_on_both_sides(self):
        result = snapshot_diff.compare_file(".hermes/config.yaml", None, None)
        assert result["verdict"] == "absent-both"


class TestCompareSnapshots:
    def test_verdicts_grouped_per_agent_and_diffs_written(self, tmp_path):
        baseline, final = tmp_path / "baseline", tmp_path / "final"
        # Claude Code: clean restore.
        write(baseline, ".claude/settings.json", json.dumps({"model": "opus"}))
        write(final, ".claude/settings.json", json.dumps({"model": "opus"}))
        # Codex: broken restore.
        write(baseline, ".codex/config.toml", "marker = true\n")
        write(final, ".codex/config.toml", "marker = false\n")

        diffs = tmp_path / "diffs"
        report = snapshot_diff.compare_snapshots(baseline, final, diffs)

        assert report["verdict"] == "FAIL"
        assert report["agents"]["Claude Code"]["verdict"] == "PASS"
        assert report["agents"]["Codex CLI"]["verdict"] == "FAIL"
        assert (diffs / ".codex__config.toml.diff").exists()

    def test_all_clean_passes(self, tmp_path):
        baseline, final = tmp_path / "baseline", tmp_path / "final"
        write(baseline, ".config/opencode/config.json", "{}")
        write(final, ".config/opencode/config.json", "{}")
        report = snapshot_diff.compare_snapshots(baseline, final, tmp_path / "diffs")
        assert report["verdict"] == "PASS"
