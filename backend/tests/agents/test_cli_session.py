"""Tests for CLI session capture/restore plumbing (preloop.agents.cli_session)."""

import base64
import io
import shlex
import tarfile
from uuid import uuid4

import pytest

from preloop.agents import cli_session
from preloop.agents.cli_session import (
    AGENT_SESSION_MARKER,
    SESSION_PACK_ROOT,
    build_session_archive_decode_shell,
    build_session_pack_shell,
    build_session_restore_shell,
    extract_session_pack,
    parse_agent_session_marker,
    resume_cli_session,
    valid_session_id,
)


class TestParseAgentSessionMarker:
    def test_valid_opencode_marker(self):
        parsed = parse_agent_session_marker(
            f"{AGENT_SESSION_MARKER} opencode ses_ab12cd34ef"
        )
        assert parsed == {
            "agent_type": "opencode",
            "session_id": "ses_ab12cd34ef",
        }

    def test_valid_codex_marker(self):
        sid = str(uuid4())
        parsed = parse_agent_session_marker(f"{AGENT_SESSION_MARKER} codex {sid}")
        assert parsed == {"agent_type": "codex", "session_id": sid}

    def test_marker_inside_longer_line(self):
        line = (
            "some preamble "
            f"{AGENT_SESSION_MARKER} opencode ses_deadbeef "
            "trailing output"
        )
        assert parse_agent_session_marker(line) == {
            "agent_type": "opencode",
            "session_id": "ses_deadbeef",
        }

    def test_missing_marker(self):
        assert parse_agent_session_marker("opencode ses_deadbeef") is None

    def test_missing_fields(self):
        assert parse_agent_session_marker(AGENT_SESSION_MARKER) is None
        assert parse_agent_session_marker(f"{AGENT_SESSION_MARKER} opencode") is None

    def test_invalid_agent_type(self):
        assert (
            parse_agent_session_marker(f"{AGENT_SESSION_MARKER} OpenCode ses_x12345")
            is None
        )
        assert (
            parse_agent_session_marker(f"{AGENT_SESSION_MARKER} 'agent' ses_x12345")
            is None
        )

    def test_invalid_session_id_is_rejected(self):
        # Shell metacharacters never pass validation; trailing junk after the
        # id is tolerated (the log filter may interleave) but only a strict
        # id can be captured.
        for bad in ("ses_a;rm -rf", "ses_$(boom)", "", "x" * 200):
            assert (
                parse_agent_session_marker(f"{AGENT_SESSION_MARKER} opencode {bad}")
                is None
            )

    def test_non_string(self):
        assert parse_agent_session_marker(None) is None
        assert parse_agent_session_marker(123) is None


class TestValidSessionId:
    def test_opencode_ids(self):
        assert valid_session_id("opencode", "ses_ab12cd34")
        assert not valid_session_id("opencode", str(uuid4()))
        assert not valid_session_id("opencode", "ses_")

    def test_codex_ids(self):
        assert valid_session_id("codex", str(uuid4()))
        assert not valid_session_id("codex", "ses_ab12cd34")

    def test_unknown_agent_type(self):
        assert not valid_session_id("gemini", "anything")


class TestResumeCliSession:
    def _context(self, agent_type, session_id):
        return {
            "trigger_event_data": {
                "_resume": {
                    "cli_session": {
                        "agent_type": agent_type,
                        "session_id": session_id,
                    }
                }
            }
        }

    def test_returns_matching_session_id(self):
        context = self._context("opencode", "ses_ab12cd34")
        assert resume_cli_session(context, "opencode") == "ses_ab12cd34"

    def test_agent_type_mismatch_starts_cold(self):
        context = self._context("codex", str(uuid4()))
        assert resume_cli_session(context, "opencode") is None

    def test_malformed_session_id_starts_cold(self):
        context = self._context("opencode", "ses_; rm -rf /")
        assert resume_cli_session(context, "opencode") is None

    def test_no_resume_metadata(self):
        assert resume_cli_session({"trigger_event_data": {}}, "opencode") is None
        assert resume_cli_session({}, "opencode") is None


class TestRestoreShell:
    def test_restore_block_relocates_the_pack(self):
        shell = build_session_restore_shell("opencode", '"$HOME/.local/x"')
        assert f"[ -d {shlex.quote(SESSION_PACK_ROOT + '/opencode')} ]" in shell
        assert "PRELOOP_CLI_SESSION_RESTORED=0" in shell
        assert "PRELOOP_CLI_SESSION_RESTORED=1" in shell
        assert 'cd "$_pl_session_dst" && tar xf -' in shell

    def test_pack_block_excludes_credentials(self):
        shell = build_session_pack_shell("opencode", '"$HOME/.local/x"')
        assert "--exclude=auth.json" in shell
        assert "--exclude=log" in shell
        assert "--exclude=logs" in shell
        assert shlex.quote(SESSION_PACK_ROOT + "/opencode") in shell

    def test_pack_block_custom_excludes(self):
        shell = build_session_pack_shell("codex", '"$CODEX_HOME/sessions"', ())
        assert "--exclude=auth.json" not in shell


class TestDecodeShell:
    def test_payload_round_trips(self):
        archive = b"PK-bytes-not-really-but-fine"
        shell = build_session_archive_decode_shell(archive)
        assert "base64 -d | tar xzf -" in shell
        encoded = shell.split("echo '")[1].split("'")[0]
        assert base64.b64decode(encoded) == archive


def _workspace_snapshot_with_pack(files: dict) -> bytes:
    """A workspace snapshot tar.gz carrying a CLI session pack."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


class TestExtractSessionPack:
    def test_extracts_the_pack_subtree(self):
        snapshot = _workspace_snapshot_with_pack(
            {
                "workspace/.git/HEAD": b"ref: refs/heads/main",
                "workspace/README.md": b"hi",
                "workspace/.preloop-agent-session/opencode/storage/a.json": b"{}",
                "workspace/.preloop-agent-session/opencode/storage/b.json": b"[]",
                "workspace/.preloop-agent-session/codex/sessions/rollout.jsonl": b"x",
            }
        )
        pack = extract_session_pack(snapshot)
        assert pack is not None
        with tarfile.open(fileobj=io.BytesIO(pack), mode="r:gz") as tar:
            names = sorted(tar.getnames())
        assert names == [
            "codex/sessions/rollout.jsonl",
            "opencode/storage/a.json",
            "opencode/storage/b.json",
        ]

    def test_snapshot_without_pack_is_none(self):
        snapshot = _workspace_snapshot_with_pack({"workspace/README.md": b"hi"})
        assert extract_session_pack(snapshot) is None

    def test_invalid_snapshot_is_none(self):
        assert extract_session_pack(b"not a tar at all") is None
        assert extract_session_pack(b"") is None

    def test_path_traversal_members_are_dropped(self):
        snapshot = _workspace_snapshot_with_pack(
            {
                "workspace/.preloop-agent-session/opencode/ok.json": b"{}",
                "workspace/.preloop-agent-session/../evil.json": b"nope",
            }
        )
        pack = extract_session_pack(snapshot)
        assert pack is not None
        with tarfile.open(fileobj=io.BytesIO(pack), mode="r:gz") as tar:
            names = sorted(tar.getnames())
        assert names == ["opencode/ok.json"]

    def test_oversized_pack_is_dropped(self, monkeypatch):
        monkeypatch.setattr(cli_session, "MAX_EMBEDDED_SESSION_ARCHIVE_BYTES", 4)
        snapshot = _workspace_snapshot_with_pack(
            {"workspace/.preloop-agent-session/opencode/big.json": b"x" * 64}
        )
        assert extract_session_pack(snapshot) is None


@pytest.mark.parametrize("agent_dir", ["opencode", "codex"])
def test_pack_root_is_shared(agent_dir):
    assert agent_dir in build_session_pack_shell(agent_dir, '"$DATA"')
